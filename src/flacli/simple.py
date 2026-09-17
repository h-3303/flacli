# SPDX-License-Identifier: GPL-3.0-or-later
"""The coarse operations behind the CLI and the simple MCP server.

Each function does one whole step of the workflow, has few parameters with sensible defaults, and returns a compact
dict. Long work (Soulseek matching) runs in a detached worker process so a call returns within an agent's tool
timeout; `status` reports the job and, when the bridge answers, also maps finished transfers and files them.
The fine-grained tools stay in server.py; this module composes them.
"""

import asyncio
import os
import time

from pathlib import Path

from . import __version__, config, jobs, server, wiki
from .bridge import BridgeClient, BridgeError
from .connectors import SERVICES, get_connector
from .db import Database
from .matcher import MatchPrefs
from .worker import prefs_to

LOSSY = ["flac", "mp3", "ogg", "opus", "m4a", "wav", "ape", "wv", "aiff"]
REMOTE_HINTS = {"tidal.com": "tidal", "deezer.com": "deezer", "deezer.page.link": "deezer", "music.youtube.com": "youtube-music",
                "youtube.com": "youtube-music", "youtu.be": "youtube-music"}


def db() -> Database:
    if server.State.db is None:
        server.State.db = Database(config.db_path())
        jobs.reap_stale(server.State.db)

    return server.State.db


def close():
    if server.State.db is not None:
        server.State.db.close()
        server.State.db = None


def _prefs(lossy=False, min_bitrate=None, album_mode="auto", harvest_seconds=10.0, max_tracks=None) -> MatchPrefs:
    prefs = MatchPrefs(album_mode=album_mode, harvest_seconds=max(0.1, float(harvest_seconds)), max_tracks=max_tracks,
                       min_bitrate=min_bitrate)

    if not lossy:
        prefs.allow_formats = ["flac"]
        prefs.prefer_formats = ["flac"]
    else:
        prefs.allow_formats = list(LOSSY)

    prefs.poll_interval_s = min(prefs.poll_interval_s, prefs.harvest_seconds)
    return prefs


def _start_job(kind: str, playlist_id: int, payload: dict, progress: dict) -> dict:
    active = db().active_job(playlist_id)

    if active is not None:
        raise ValueError(f"a job is already running for playlist {playlist_id} (job {active['id']}); "
                         f"wait for it (flacli status {playlist_id}) or cancel it (flacli cancel {playlist_id})")

    job_id = db().create_job(playlist_id, kind, progress)
    pid = jobs.spawn(db(), job_id, kind, playlist_id, payload)
    return {"job_id": job_id, "pid": pid}


# Health #

async def doctor() -> dict:
    result = {"version": __version__, "config_file": str(config.config_path()),
              "config_file_exists": config.config_path().exists(), "settings": {k: v["value"] for k, v in config.effective().items()},
              "data_dir": str(config.data_dir()), "music_dir": str(config.music_dir()),
              "music_dir_exists": config.music_dir().is_dir()}

    try:
        result["library_files"] = db().library_count()
        result["playlists"] = len(db().list_playlists())
    except Exception as error:  # noqa: BLE001 - reported, never fatal here
        result["database_error"] = str(error)

    try:
        status = await server.bridge().status()
        result["nicotine"] = {"reachable": True, "online": status.get("online"), "username": status.get("username"),
                              "version": status.get("nicotine_version"), "protocol": status.get("protocol"),
                              "socket": server.bridge().socket_path}

        if status.get("protocol") == 1:
            result["nicotine"]["warning"] = "bridge plugin is protocol v1: reinstall it from nicotine-plugin/ and re-enable it"
    except BridgeError as error:
        result["nicotine"] = {"reachable": False, "socket": server.bridge().socket_path, "error": str(error),
                              "fix": "start Nicotine+ and enable the 'MCP Bridge' plugin (Preferences > Plugins); "
                                     "install it with install.sh if it is not listed"}

    result["services"] = {s["service"]: s.get("connected", False) for s in (await server.service_status())["services"]}
    result["ok"] = result["nicotine"]["reachable"] and result["music_dir_exists"]
    return result


# Direct requests #

async def get(items: list, playlist: str = server.REQUESTS_PLAYLIST, min_confidence: float = 0.85, lossy: bool = False,
              min_bitrate: int | None = None, harvest_seconds: float = 10.0, download: bool = True) -> dict:
    """Name songs and albums; they are resolved, checked against the library, and the rest is fetched by a worker."""
    existing = db().find_playlist(playlist, source="request")
    before = {r["id"] for r in db().tracks(existing["id"])} if existing else set()
    result = await server.request_music(items, playlist=playlist, download=False)
    playlist_id = result["playlist_id"]
    added = [r for r in db().tracks(playlist_id) if r["id"] not in before]
    pending = [r["id"] for r in added if r["status"] == "pending"]
    result["tracks"] = [{"track_id": r["id"], "artist": r["artist"], "title": r["title"], "album": r["album"], "status": r["status"]}
                        for r in added][:50]

    if not download or not pending:
        result["note"] = "nothing to fetch: everything named is already in the library" if not pending else "prepared only; nothing fetched"
        return result

    await server.bridge().status()   # fail early when Nicotine+ is unreachable
    prefs = _prefs(lossy, min_bitrate, "auto", harvest_seconds)
    started = _start_job("request", playlist_id, {"track_ids": pending, "prefs": prefs_to(prefs), "min_confidence": min_confidence},
                         {"total": len(pending), "phase": "matching"})
    result.update(started)
    result["min_confidence"] = min_confidence
    result["note"] = (f"fetching {len(pending)} track(s) in the background; run `flacli status {playlist_id}` in a minute or two. "
                      "Matches at or above min_confidence are queued in Nicotine+ automatically; doubtful ones wait in "
                      f"`flacli review {playlist_id}`.")
    return result


# Playlists #

def _looks_like_url(target: str) -> bool:
    return target.startswith(("http://", "https://"))


def guess_service(target: str) -> str | None:
    for host, service in REMOTE_HINTS.items():
        if host in target:
            return service

    return None


async def import_playlist(target: str, service: str | None = None, name: str | None = None) -> dict:
    """A file path, a share URL, or a service playlist id (with service=) becomes a stored playlist."""
    if service is None and _looks_like_url(target):
        service = guess_service(target)

        if service is None:
            raise ValueError(f"cannot tell which service {target} belongs to; pass service=tidal|deezer|youtube-music")

    if service:
        if service not in SERVICES:
            raise ValueError(f"service must be one of {', '.join(SERVICES)}")

        result = await server.import_remote_playlist(service, target)
    else:
        path = Path(target).expanduser()

        if not path.is_file():
            raise FileNotFoundError(f"{target} is not a file; for a service playlist pass its share URL or service= with its id")

        result = await server.import_playlist_file(str(path), playlist_name=name)

    return result


async def prepare(playlist_id: int, resolve: bool = True) -> dict:
    """Resolve against MusicBrainz, scan the library, diff. The part of a sync that needs no Soulseek."""
    result = {"playlist_id": playlist_id}

    if resolve:
        resolved = await server.resolve_playlist(playlist_id)
        result["resolved"] = resolved.get("resolved")
        result["unresolved"] = resolved.get("unresolved")

    root = config.music_dir()

    if root.is_dir():
        result["library_scan"] = await server.scan_library()
        diff = await server.diff_library(playlist_id)
        result["already_in_library"] = diff.get("in_library")
    else:
        result["warning"] = f"music dir {root} does not exist; nothing diffed"

    result["counts"] = {k: v for k, v in db().status_counts(playlist_id).items() if v}
    return result


async def sync(target: str, service: str | None = None, name: str | None = None, playlist_id: int | None = None,
               yes: bool = False, min_confidence: float = 0.85, lossy: bool = False, min_bitrate: int | None = None,
               album_mode: str = "auto", harvest_seconds: float = 10.0, max_tracks: int | None = None) -> dict:
    """Import (if needed), resolve, diff, then match in the background. With yes=True the confident matches are
    also queued by the worker; otherwise `queue` shows the totals and needs an explicit yes."""
    result = {}

    if playlist_id is None:
        if target.isdigit():
            playlist_id = int(target)
        else:
            imported = await import_playlist(target, service=service, name=name)
            result["imported"] = imported["imported"]

            if len(imported["imported"]) != 1:
                result["note"] = "several playlists imported; run `flacli sync <playlist_id>` for each"
                return result

            playlist_id = imported["imported"][0]["playlist_id"]

    db().get_playlist(playlist_id)
    await server.bridge().status()   # fail early, before minutes of MusicBrainz lookups, when Nicotine+ is unreachable
    result.update(await prepare(playlist_id))
    pending = [r["id"] for r in db().tracks(playlist_id, statuses=["pending", "searching"])]
    result["to_fetch"] = len(pending)

    if not pending:
        result["note"] = f"nothing to fetch; write the playlist with `flacli m3u {playlist_id}`"
        return result

    prefs = _prefs(lossy, min_bitrate, album_mode, harvest_seconds, max_tracks)

    if yes:
        started = _start_job("request", playlist_id, {"track_ids": pending, "prefs": prefs_to(prefs), "min_confidence": min_confidence},
                             {"total": len(pending), "phase": "matching"})
        result["note"] = (f"matching {len(pending)} track(s) in the background and queueing every match at or above "
                          f"{min_confidence}; check `flacli status {playlist_id}`")
    else:
        started = _start_job("match", playlist_id, {"prefs": prefs_to(prefs)}, {"total": len(pending)})
        result["note"] = (f"matching {len(pending)} track(s) in the background, nothing queued yet; when `flacli status "
                          f"{playlist_id}` shows the job finished, run `flacli queue {playlist_id}` to see the totals")

    result.update(started)
    return result


async def status(playlist_id: int | None = None) -> dict:
    """Without an id: every playlist. With one: counts, the job, and the download state (finished transfers are
    filed at the same time)."""
    if playlist_id is None:
        return await server.list_playlists()

    playlist = db().get_playlist(playlist_id)
    result = {"playlist_id": playlist_id, "name": playlist["name"], "tracks": playlist["track_count"]}
    job = db().active_job(playlist_id) or db().conn.execute(
        "SELECT * FROM jobs WHERE playlist_id = ? ORDER BY id DESC LIMIT 1", (playlist_id,)).fetchone()
    result["job"] = jobs.describe(job)
    queued = db().tracks(playlist_id, statuses=["queued", "downloading"])

    if queued:
        try:
            synced = await server.sync_downloads(playlist_id)
            result["downloads"] = {k: synced[k] for k in ("checked", "done", "downloading", "queued", "failed", "retried", "missing")}

            if synced.get("tidied"):
                result["tidied"] = synced["tidied"]
            if synced.get("tidy_error"):
                result["tidy_error"] = synced["tidy_error"]
        except BridgeError as error:
            result["downloads_error"] = str(error)

    counts = {k: v for k, v in db().status_counts(playlist_id).items() if v}
    result["counts"] = counts
    result["next"] = _next_step(playlist_id, result["job"], counts)
    return result


def _next_step(playlist_id, job, counts) -> str:
    running = job and job["status"] in ("running", "waiting")

    if running:
        return f"job still running; check again in a minute (flacli status {playlist_id})"
    if counts.get("queued") or counts.get("downloading"):
        return f"downloads in progress; check again later (flacli status {playlist_id})"
    if counts.get("approved"):
        return f"approved tracks not queued yet: flacli queue {playlist_id} --yes (after the user has seen the totals)"
    if counts.get("candidates"):
        return f"{counts['candidates']} track(s) need a decision: flacli review {playlist_id}, then flacli approve / flacli skip"
    if counts.get("pending"):
        return f"{counts['pending']} track(s) not matched yet: flacli sync {playlist_id}"
    if counts.get("failed") or counts.get("not_found"):
        return f"some tracks failed or were not found; flacli review {playlist_id} --status not_found shows them; the rest is done: flacli m3u {playlist_id}"

    return f"all done: flacli m3u {playlist_id} writes the playlist"


async def review(playlist_id: int, status_name: str = "candidates", doubtful_only: bool = False, limit: int = 25, offset: int = 0) -> dict:
    max_confidence = 0.849 if doubtful_only else None
    result = await server.review_candidates(playlist_id, status=status_name, max_confidence=max_confidence, limit=limit,
                                            offset=offset, candidates_per_track=3)
    result["how_to_decide"] = ("flacli approve <playlist_id> --tracks <ids> [--candidate N]; flacli skip <playlist_id> --tracks <ids>; "
                               "confidence >= 0.85 is normally safe, look at 'why' and 'quality' below that")
    return result


async def approve(playlist_id: int, track_ids: list[int] | None = None, min_confidence: float | None = None, candidate: int = 0) -> dict:
    if track_ids:
        return await server.approve(track_ids=track_ids, candidate_index=candidate)

    return await server.approve(playlist_id=playlist_id, min_confidence=min_confidence if min_confidence is not None else 0.85)


async def skip(track_ids: list[int], reason: str = "skipped by user") -> dict:
    return await server.skip_tracks(track_ids, reason=reason)


async def queue(playlist_id: int, yes: bool = False, min_confidence: float | None = None) -> dict:
    """Totals of what would be downloaded; with yes=True the transfers are queued in Nicotine+."""
    approved_now = None

    if min_confidence is not None:
        approved_now = (await server.approve(playlist_id=playlist_id, min_confidence=min_confidence))["approved"]

    result = await server.queue_approved(playlist_id, confirm=yes)

    if approved_now is not None:
        result["approved_now"] = approved_now
    if not yes:
        result["note"] = "nothing queued; show these totals to the user and rerun with --yes on their OK"
    else:
        result["note"] = f"queued; `flacli status {playlist_id}` follows the downloads and files each finished track"

    return result


async def wait_for(playlist_id: int, timeout_s: float = 600, poll_s: float = 10) -> dict:
    """Block until the playlist's job is over and no transfer is in flight (or the timeout passes)."""
    deadline = time.time() + timeout_s
    last = None

    while True:
        last = await status(playlist_id)
        job = last.get("job") or {}
        running = job.get("status") in ("running", "waiting")
        in_flight = last["counts"].get("queued") or last["counts"].get("downloading")

        if not running and not in_flight:
            last["waited"] = True
            return last
        if time.time() >= deadline:
            last["waited"] = False
            last["note"] = f"timeout after {timeout_s:g} s; run `flacli status {playlist_id}` later"
            return last

        await asyncio.sleep(poll_s)


async def cancel(playlist_id: int) -> dict:
    task = server.State.jobs.get(playlist_id)

    if task and not task.done():
        task.cancel()
        return {"cancelled": True, "playlist_id": playlist_id}

    return jobs.cancel(db(), playlist_id)


async def m3u(playlist_id: int, path: str | None = None, relative_to: str | None = None) -> dict:
    return await server.write_m3u(playlist_id, path=path, relative_to=relative_to)


async def delete(playlist_id: int) -> dict:
    return await server.delete_playlist(playlist_id)


# Library #

async def scan(rescan: bool = False) -> dict:
    return await server.scan_library(rescan=rescan)


# Wikis #

def _wiki_sources() -> wiki.Sources:
    return wiki.Sources(db(), config.user_agent(), fetch=server.State.mb_fetch, **({"sleep": server.State.mb_sleep} if server.State.mb_sleep else {}))


async def wiki_missing(include_all: bool = False) -> dict:
    """Artists and albums without a bio / wiki yet (after an incremental scan)."""
    await server.scan_library()
    return await asyncio.to_thread(wiki.missing, db(), config.music_dir(), include_all)


async def wiki_sources(artist: str, album: str | None = None) -> dict:
    """Facts and links for one artist or album: MusicBrainz, Wikidata, the Wikipedia lead when there is one."""
    await server.scan_library()
    albums, artists = await asyncio.to_thread(wiki.inventory, db(), config.music_dir())
    entry = wiki.find_entry(albums, artists, artist, album)
    return await asyncio.to_thread(wiki.sources_for, _wiki_sources(), entry)


async def wiki_set(artist: str, content: str, attribution: str, album: str | None = None, url: str | None = None,
                   force: bool = False) -> dict:
    """Write the text to its sidecar file and push it into the configured player caches."""
    await server.scan_library()
    return await asyncio.to_thread(wiki.set_text, db(), config.music_dir(), artist, album, content, attribution, url,
                                   wiki.target_paths(config.wiki_targets()), force)


async def wiki_fill(limit: int = 10) -> dict:
    """Wikipedia text for entries that have an article; briefs with facts for the rest."""
    await server.scan_library()
    return await asyncio.to_thread(wiki.fill, db(), config.music_dir(), _wiki_sources(), wiki.target_paths(config.wiki_targets()), limit)


async def wiki_push(artist: str | None = None, album: str | None = None) -> dict:
    """Copy sidecar texts into the player caches again (after a cache wipe, or a new target)."""
    await server.scan_library()
    albums, artists = await asyncio.to_thread(wiki.inventory, db(), config.music_dir())
    entries = [wiki.find_entry(albums, artists, artist, album)] if artist else artists + albums
    return await asyncio.to_thread(wiki.push, config.music_dir(), entries, wiki.target_paths(config.wiki_targets()))


async def tidy(apply: bool = False, force: bool = False, music_dir: str | None = None) -> dict:
    if apply:
        return await server.tidy_apply(music_dir=music_dir, confirm=True, force=force)

    result = await server.tidy_analyse(music_dir=music_dir)
    result["note"] = ("dry run, nothing changed. Read report_path, resolve the open questions in approved_path, show the user "
                      "every deletion by name, then `flacli tidy --apply` on their yes")
    return result


async def tidy_new(paths: list[str] | None = None, music_dir: str | None = None) -> dict:
    return await server.tidy_new(paths=paths, music_dir=music_dir)


# Services #

async def service(action: str, name: str | None = None, headers_raw: str | None = None, auth_file: str | None = None,
                  user: str | None = None, force: bool = False) -> dict:
    if action == "status":
        return await server.service_status(name)
    if name is None:
        raise ValueError("which service? tidal, deezer or youtube-music")
    if action == "connect":
        return await server.connect_service(name, headers_raw=headers_raw, auth_file=auth_file, force=force)
    if action == "disconnect":
        return await server.disconnect_service(name)
    if action == "playlists":
        return await server.list_remote_playlists(name, user=user)

    raise ValueError("action must be status, connect, disconnect or playlists")


# Raw Soulseek #

async def search(query: str, wait_seconds: int = 10, lossless_only: bool = False, max_folders: int = 10) -> dict:
    from . import soulseek_mcp
    return await soulseek_mcp.search(query, wait_seconds=wait_seconds, lossless_only=lossless_only, max_folders=max_folders)


async def downloads(status_name: str | None = None, limit: int = 100) -> dict:
    from . import soulseek_mcp
    return await soulseek_mcp.list_downloads(statuses=[status_name] if status_name else None, limit=limit)


async def download_folder(username: str, folder: str) -> dict:
    from . import soulseek_mcp
    return await soulseek_mcp.download_folder(username, folder)
