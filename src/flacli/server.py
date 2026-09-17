# SPDX-License-Identifier: GPL-3.0-or-later
"""flacli full MCP server (stdio): every operation as its own tool, for capable models.

Small models are better served by `flacli mcp` (the simple server in mcp_simple.py) or the CLI.

Tools return compact summaries and ids; whole tracklists are never dumped unless a slice is asked for.
"""

import asyncio
import functools
import os
import time

from pathlib import Path
from typing import Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import __version__, config
from . import mpd as _mpd
from .beets import Beets, BeetsError
from .bridge import BridgeClient, BridgeError
from .connectors import SERVICES, ConnectorError, get_connector
from .db import Database
from .importers import FORMATS, import_file
from .jspf import write_jspf
from .library import diff_playlist, reindex_moved
from .library import scan_library as _scan_library
from .m3u import write_m3u as _write_m3u
from .matcher import MatchJob, MatchPrefs, download_id
from .models import MATCH_STATUSES, Track
from .musicbrainz import MusicBrainzClient, MusicBrainzError
from .requester import expand, parse_items
from .tidy import Tidy, TidyError
from .troi_resolver import Troi, TroiError
from . import avatar as _avatar
from . import cover as _cover
from . import wiki as _wiki
from .wiki import WikiError

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE_LOCAL = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
NETWORK = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)

FAILED_TRANSFER_STATUSES = {"Cancelled", "Filtered", "User logged off", "Connection closed", "Connection timeout",
                            "Download folder error", "Local file error"}

mcp = MCPServer(
    name="flacli",
    instructions=(
        "flacli (full tool set): import playlist files, canonicalise them against MusicBrainz, find which tracks are already "
        "in the local library, and match the rest on Soulseek through the running Nicotine+ (via the 'nicotine' "
        "bridge). Nothing is downloaded until queue_approved(confirm=True) is called after the user has seen its "
        "totals. Typical order: import_playlist_file -> resolve_playlist -> scan_library (once) -> diff_library -> "
        "match_playlist -> playlist_status until the job finishes -> review_candidates -> approve -> "
        "queue_approved -> sync_downloads -> write_m3u. Playlists can also come straight from a service: "
        "connect_service('tidal') (official API, browser login) or connect_service('youtube-music', headers_raw=...), "
        "then list_remote_playlists / import_remote_playlist; Deezer public playlists need no login. When the user "
        "simply names songs or albums they want, request_music(items) does the whole thing in one call: it adds them "
        "to a persistent 'Requests' playlist, expands albums through MusicBrainz, matches on Soulseek and queues the "
        "confident matches without a separate confirmation (the request is the instruction); report what it "
        "understood right away. Library housekeeping: tidy_analyse (dry run, writes a report) then "
        "tidy_apply(confirm=True) once the user has approved the plan; sync_downloads tidies the tracks it marks done "
        "by itself (tags + Artist/Album/NN - Title, never deletions) unless auto_tidy is off, and tidy_new files "
        "tracks that arrived by other routes. Optional extras when installed: beets_import(playlist_id) hands finished downloads to "
        "`beet import` (dry run first, confirm=True to import); troi_scan / troi_resolve find tracks in a "
        "MusicBrainz-tagged collection through the ListenBrainz content resolver."
    ),
)


class State:
    db: Database | None = None
    bridge: BridgeClient | None = None
    jobs: dict[int, asyncio.Task] = {}
    mb_fetch = None      # tests inject a fake fetcher
    mb_sleep = None
    fetch_bytes = None   # tests inject a fake picture download


def db() -> Database:
    if State.db is None:
        State.db = Database(config.db_path())
        State.db.interrupt_running_jobs()

    return State.db


def bridge() -> BridgeClient:
    if State.bridge is None:
        State.bridge = BridgeClient()

    return State.bridge


def tool_errors(function):
    @functools.wraps(function)
    async def wrapper(*args, **kwargs):
        try:
            return await function(*args, **kwargs)
        except (LookupError, ValueError, FileNotFoundError, BridgeError, MusicBrainzError, PermissionError, TidyError,
                ConnectorError, BeetsError, TroiError, WikiError) as error:
            raise ToolError(str(error)) from None

    return wrapper


def _row_summary(row, candidates_shown=3):
    candidates = db().candidates(row)
    item = {
        "track_id": row["id"], "position": row["position"] + 1, "artist": row["artist"], "title": row["title"],
        "album": row["album"] or None, "status": row["status"], "confidence": row["confidence"],
    }

    if row["local_path"]:
        item["local_path"] = row["local_path"]
    if row["download_id"]:
        item["download_id"] = row["download_id"]
    if row["last_error"] and row["status"] in ("failed", "not_found"):
        item["error"] = row["last_error"]

    if candidates:
        item["candidates"] = [_candidate_summary(c) for c in candidates[:candidates_shown]]

    return item


def _quality(candidate):
    if candidate["kind"] == "folder":
        return "/".join(candidate.get("formats") or [])

    parts = [candidate["ext"]]

    if candidate.get("bit_depth") and candidate.get("sample_rate"):
        parts.append(f"{candidate['bit_depth']}bit {candidate['sample_rate'] / 1000:g}kHz")
    elif candidate.get("bitrate"):
        parts.append(f"{candidate['bitrate']}{'v' if candidate.get('vbr') else ''}kbps")

    return " ".join(parts)


def _candidate_summary(candidate):
    base = {
        "kind": candidate["kind"], "user": candidate["user"], "confidence": candidate["confidence"],
        "score": candidate["score"], "quality": _quality(candidate), "mb": round((candidate.get("size") or 0) / 1048576, 1),
        "free_slot": candidate.get("free_slot"), "queue": candidate.get("queue"), "why": candidate.get("breakdown"),
    }

    if candidate["kind"] == "folder":
        base["folder"] = candidate["folder"]
        base["track_count"] = candidate["track_count"]
        base["expected_track_count"] = candidate.get("expected_track_count")
        base["file"] = candidate.get("expected_path", "").rpartition("\\")[2] or None
    else:
        base["file"] = candidate["name"]
        base["folder"] = candidate["path"].rpartition("\\")[0]

        if candidate.get("duration"):
            base["length"] = f"{candidate['duration'] // 60}:{candidate['duration'] % 60:02d}"

    return base


# Import #

@mcp.tool(annotations=WRITE_LOCAL)
@tool_errors
async def import_playlist_file(
    path: str,
    format: Literal["auto", "spotify-export", "exportify", "csv", "m3u", "jspf", "xspf"] = "auto",
    playlist_name: str | None = None,
    column_mapping: dict[str, str] | None = None,
) -> dict:
    """Import a playlist file: Spotify data export (Playlist*.json / YourLibrary.json), Exportify CSV, generic
    CSV (header synonyms or column_mapping like {"title": "Song", "artist": "Band"}), M3U/M3U8, JSPF or XSPF.
    A Spotify export with several playlists imports them all unless playlist_name picks one. Each playlist is
    written as JSPF under the flacli data dir and gets a playlist_id. Spotify exports have no durations;
    resolve_playlist fills them in from MusicBrainz."""
    if format not in FORMATS:
        raise ValueError(f"format must be one of {', '.join(FORMATS)}")

    playlists = import_file(path, format=format, column_mapping=column_mapping, playlist_name=playlist_name)
    return {"imported": [_store_playlist(playlist) for playlist in playlists]}


def _store_playlist(playlist) -> dict:
    """Persist an ImportedPlaylist (rows + JSPF) and return the compact summary the import tools report."""
    playlist_id = db().add_playlist(playlist.name, playlist.source, playlist.tracks, source_ref=playlist.source_ref)
    safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in playlist.name).strip() or "playlist"
    jspf_path = config.playlists_dir() / f"{playlist_id:04d}-{safe}.jspf"
    write_jspf(jspf_path, playlist.name, playlist.tracks, playlist.source, playlist.source_ref)
    db().set_playlist_jspf(playlist_id, jspf_path)
    return {
        "playlist_id": playlist_id, "name": playlist.name, "tracks": len(playlist.tracks),
        "detected_format": playlist.source, "source_ref": playlist.source_ref, "jspf_path": str(jspf_path),
        "already_local": sum(1 for t in playlist.tracks if t.local_path),
        "needs_durations": not playlist.has_durations,
        "warnings": playlist.warnings[:20] + ([f"... {len(playlist.warnings) - 20} more"] if len(playlist.warnings) > 20 else []),
    }


# Streaming services #

Service = Literal["tidal", "deezer", "youtube-music"]


@mcp.tool(annotations=NETWORK)
@tool_errors
async def connect_service(
    service: Service,
    headers_raw: str | None = None,
    auth_file: str | None = None,
    force: bool = False,
) -> dict:
    """Connect a streaming service so its playlists can be listed and imported directly.
    tidal: official API with a browser login (PKCE). First call returns an authorize_url (and tries to open it);
    after the user has approved in the browser, call again to finish. Needs the tidal_client_id user config.
    youtube-music: pass headers_raw (request headers copied from a logged-in music.youtube.com tab) or
    auth_file (an existing ytmusicapi browser.json). deezer: nothing to connect, public playlists only.
    Tokens are stored 0600 under the flacli data dir and never leave this machine."""
    connector = get_connector(service)
    return await asyncio.to_thread(connector.connect, headers_raw=headers_raw, auth_file=auth_file, force=force)


@mcp.tool(annotations=WRITE_LOCAL)
@tool_errors
async def disconnect_service(service: Service) -> dict:
    """Forget a service's stored tokens / cookies."""
    return get_connector(service).disconnect()


@mcp.tool(annotations=READ_ONLY)
@tool_errors
async def service_status(service: Service | None = None) -> dict:
    """Whether each streaming service is connected, and how to connect it if not."""
    services = [service] if service else list(SERVICES)
    return {"services": [get_connector(s).status() for s in services]}


@mcp.tool(annotations=NETWORK)
@tool_errors
async def list_remote_playlists(service: Service, user: str | None = None) -> dict:
    """The connected account's playlists (id, name, track count, url). Deezer has no login here: pass user=
    a Deezer user id or deezer.com/profile/<id> URL to list that user's public playlists."""
    connector = get_connector(service)
    playlists = await asyncio.to_thread(connector.list_playlists, user=user)
    return {"service": connector.key, "playlists": playlists}


@mcp.tool(annotations=NETWORK)
@tool_errors
async def import_remote_playlist(service: Service, id_or_url: str) -> dict:
    """Fetch one playlist from a service (by id or share URL) and import it like a file: it gets a
    playlist_id and a JSPF under the flacli data dir. Follow with resolve_playlist as usual. TIDAL and Deezer
    supply ISRCs and durations; YouTube Music supplies durations only."""
    connector = get_connector(service)
    playlist = await asyncio.to_thread(connector.fetch_playlist, id_or_url)
    return {"imported": [_store_playlist(playlist)]}


@mcp.tool(annotations=READ_ONLY)
@tool_errors
async def list_playlists() -> dict:
    """Imported playlists with their status counts."""
    playlists = []

    for row in db().list_playlists():
        counts = db().status_counts(row["id"])
        playlists.append({
            "playlist_id": row["id"], "name": row["name"], "source": row["source"], "imported_at": row["imported_at"],
            "tracks": row["track_count"], "counts": {k: v for k, v in counts.items() if v},
        })

    return {"playlists": playlists}


@mcp.tool(annotations=READ_ONLY)
@tool_errors
async def playlist_status(playlist_id: int) -> dict:
    """Counts by match status plus the active or last matching job (progress, rate-limit waits)."""
    playlist = db().get_playlist(playlist_id)
    job = db().active_job(playlist_id) or db().conn.execute(
        "SELECT * FROM jobs WHERE playlist_id = ? ORDER BY id DESC LIMIT 1", (playlist_id,)
    ).fetchone()
    result = {
        "playlist_id": playlist_id, "name": playlist["name"], "tracks": playlist["track_count"],
        "counts": {k: v for k, v in db().status_counts(playlist_id).items() if v},
        "unresolved": db().conn.execute(
            "SELECT COUNT(*) FROM tracks WHERE playlist_id = ? AND mb_recording_id IS NULL", (playlist_id,)
        ).fetchone()[0],
    }

    if job is not None:
        import json
        progress = json.loads(job["progress_json"] or "{}")
        result["job"] = {"job_id": job["id"], "kind": job["kind"], "status": job["status"], "started_at": job["started_at"],
                         "updated_at": job["updated_at"], **progress}

        if job["error"]:
            result["job"]["error"] = job["error"]
        if progress.get("waiting_rate_limit_until"):
            result["job"]["note"] = f"waiting on Soulseek search rate limit, ~{max(0, round(progress['waiting_rate_limit_until'] - time.time()))} s"

    return result


@mcp.tool(annotations=WRITE_LOCAL)
@tool_errors
async def delete_playlist(playlist_id: int) -> dict:
    """Forget an imported playlist and its match state (files on disk are untouched)."""
    playlist = db().get_playlist(playlist_id)
    task = State.jobs.get(playlist_id)

    if task and not task.done():
        task.cancel()

    db().delete_playlist(playlist_id)
    return {"deleted": playlist_id, "name": playlist["name"]}


# Direct requests #

REQUESTS_PLAYLIST = "Requests"


def _request_playlist(name: str) -> tuple[int, str, bool]:
    """The persistent request playlist with this name (created when missing): (playlist_id, name, created)."""
    row = db().find_playlist(name, source="request")

    if row is not None:
        return row["id"], row["name"], False

    playlist_id = db().add_playlist(name, "request", [], source_ref="request_music")
    safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in name).strip() or "requests"
    jspf_path = config.playlists_dir() / f"{playlist_id:04d}-{safe}.jspf"
    write_jspf(jspf_path, name, [], "request", "request_music")
    db().set_playlist_jspf(playlist_id, jspf_path)
    return playlist_id, name, True


def _rewrite_jspf(playlist_id: int):
    playlist = db().get_playlist(playlist_id)

    if not playlist["jspf_path"]:
        return

    tracks = [Track(title=r["title"], artist=r["artist"], album=r["album"], duration_ms=r["duration_ms"], isrc=r["isrc"],
                    source_uri=r["source_uri"], mb_recording_id=r["mb_recording_id"], mb_release_id=r["mb_release_id"],
                    mb_release_track_count=r["mb_release_track_count"], position=r["position"],
                    local_path=r["local_path"] if r["status"] in ("in_library", "done") else None)
              for r in db().tracks(playlist_id)]
    write_jspf(Path(playlist["jspf_path"]), playlist["name"], tracks, playlist["source"], playlist["source_ref"])


@mcp.tool(annotations=NETWORK)
@tool_errors
async def request_music(
    items: list[str | dict[str, str]],
    playlist: str = REQUESTS_PLAYLIST,
    download: bool = True,
    min_confidence: float = 0.85,
    prefer_formats: list[str] | None = None,
    allow_formats: list[str] | None = None,
    min_bitrate: int | None = None,
    harvest_seconds: float = 10.0,
) -> dict:
    """Get named songs and whole albums in one go. Items: {"artist": ..., "title": ...} for a song,
    {"artist": ..., "album": ...} for an album, or strings "Artist - Title" / "Artist - Album (album)". The items are
    appended to a persistent request playlist (default "Requests"; it keeps growing across sessions), albums are
    expanded to their tracklist via MusicBrainz, everything is resolved and checked against the local library, and
    a background job then matches the missing tracks on Soulseek and queues every match at or above min_confidence
    (whole folders in album mode) without a further confirmation: the request itself is the go-ahead. The result
    says what was understood (for albums: the release picked and its track count) and how many tracks are being
    fetched; poll playlist_status for the job, then sync_downloads. Doubtful matches stay in 'candidates' for
    review_candidates / approve. download=False only prepares the playlist."""
    parsed = parse_items(items)
    playlist_id, name, created = _request_playlist(playlist)
    active = State.jobs.get(playlist_id)

    if active and not active.done():
        raise ValueError(f"a job is already running for playlist {playlist_id} ({name}); wait for it or use another playlist name")

    client = _musicbrainz()
    tracks, understood = await asyncio.to_thread(expand, client, parsed)
    ids = db().append_tracks(playlist_id, tracks) if tracks else []
    unresolved = []

    if ids:
        rows = [db().track(i) for i in ids if not db().track(i)["mb_recording_id"]]
        _, unresolved = await asyncio.to_thread(_resolve_rows, client, rows)
        root = config.music_dir()

        if root.is_dir():
            await asyncio.to_thread(scan_library_sync, root, False)
            diff_playlist(db(), playlist_id)

        _rewrite_jspf(playlist_id)

    rows = [db().track(i) for i in ids]
    pending = [r["id"] for r in rows if r["status"] == "pending"]
    owned = [r for r in rows if r["status"] == "in_library"]
    result = {
        "playlist_id": playlist_id, "playlist": name, "created": created, "understood": understood,
        "added": len(ids), "already_in_library": len(owned), "to_fetch": len(pending),
        "unresolved": [{"artist": u["artist"], "title": u["title"]} for u in unresolved][:25],
        "musicbrainz_requests": client.requests_made,
    }

    if not download or not pending:
        result["note"] = "nothing to fetch" if not pending else "playlist prepared; run match_playlist when ready"
        return result

    await bridge().status()  # fail early if Nicotine+ is unreachable
    prefs = _match_prefs(prefer_formats, allow_formats, min_bitrate, "auto", None, harvest_seconds, 3.0)
    job_id = db().create_job(playlist_id, "request", {"total": len(pending), "phase": "matching"})
    State.jobs[playlist_id] = asyncio.create_task(_request_job(job_id, playlist_id, pending, prefs, min_confidence))
    result.update({"job_id": job_id, "min_confidence": min_confidence,
                   "note": "matching and queueing in the background; poll playlist_status, then sync_downloads"})
    return result


async def _request_job(job_id, playlist_id, track_ids, prefs, min_confidence):
    """Match the requested tracks, auto-approve the confident candidates, queue them. State lives in the job row."""
    job = MatchJob(db(), bridge(), prefs)
    await job.run(job_id, playlist_id, track_ids)

    if db().job(job_id)["status"] != "finished":
        return

    progress = dict(job.progress, phase="queueing")
    db().update_job(job_id, status="running", progress=progress)

    try:
        for_review, not_found = 0, 0

        for track_id in track_ids:
            row = db().track(track_id)

            if row["status"] == "not_found":
                not_found += 1
            elif row["status"] == "candidates":
                candidates = db().candidates(row)

                if candidates and (row["confidence"] or 0) >= min_confidence:
                    db().set_match(track_id, "approved", confidence=candidates[0]["confidence"])
                else:
                    for_review += 1

        files, folders = _queue_plan(playlist_id, track_ids)
        summary = _queue_summary(playlist_id, files, folders)
        summary.update(await _queue(files, folders))
        progress.update(phase="finished", queued=summary["queued"], total_mb=summary["total_mb"], users=summary["users"],
                        folders=summary["folders"], for_review=for_review, not_found=not_found,
                        queue_errors=summary["errors"][:20])
        db().update_job(job_id, status="finished", progress=progress)
    except asyncio.CancelledError:
        db().update_job(job_id, status="cancelled", progress=progress)
        raise
    except Exception as error:  # noqa: BLE001 - recorded on the job, never crashes the server
        db().update_job(job_id, status="failed", progress=progress, error=f"{type(error).__name__}: {error}")


# MusicBrainz #

@mcp.tool(annotations=NETWORK)
@tool_errors
async def resolve_playlist(playlist_id: int, only_unresolved: bool = True, limit: int | None = None) -> dict:
    """Canonicalise tracks against MusicBrainz (ISRC first, then artist + title + duration search), at most one
    request per second, cached locally. Fills in missing durations, recording/release ids and release track
    counts (used by album mode). Reports tracks that could not be resolved."""
    db().get_playlist(playlist_id)
    rows = db().tracks(playlist_id)

    if only_unresolved:
        rows = [r for r in rows if not r["mb_recording_id"]]

    if limit:
        rows = rows[:limit]

    client = _musicbrainz()
    resolved, unresolved = await asyncio.to_thread(_resolve_rows, client, rows)
    return {
        "playlist_id": playlist_id, "attempted": len(rows), "resolved": resolved, "unresolved": len(unresolved),
        "requests": client.requests_made, "cache_hits": client.cache_hits,
        "unresolved_tracks": unresolved[:25],
    }


def _musicbrainz() -> MusicBrainzClient:
    return MusicBrainzClient(db(), config.user_agent(), fetch=State.mb_fetch, **({"sleep": State.mb_sleep} if State.mb_sleep else {}))


def _resolve_rows(client, rows) -> tuple[int, list]:
    """Canonicalise track rows against MusicBrainz (blocking; run in a thread). Returns (resolved, unresolved)."""
    resolved, unresolved = 0, []

    for row in rows:
        hit = client.resolve(row["title"], row["artist"], row["album"], row["duration_ms"], row["isrc"])

        if hit is None:
            unresolved.append({"track_id": row["id"], "position": row["position"] + 1, "artist": row["artist"],
                               "title": row["title"]})
            continue

        fields = {"mb_recording_id": hit["recording_id"], "mb_release_id": hit["release_id"],
                  "mb_release_track_count": hit["release_track_count"]}

        if not row["duration_ms"] and hit["duration_ms"]:
            fields["duration_ms"] = hit["duration_ms"]
            fields["duration_source"] = "musicbrainz"

        db().update_track(row["id"], **fields)
        resolved += 1

    return resolved, unresolved


# Local library #

@mcp.tool(annotations=WRITE_LOCAL)
@tool_errors
async def scan_library(music_dir: str | None = None, rescan: bool = False) -> dict:
    """Index the local music folder (tags, duration, MusicBrainz ids via mutagen). Incremental unless rescan."""
    root = Path(music_dir).expanduser() if music_dir else config.music_dir()
    return await asyncio.to_thread(scan_library_sync, root, rescan)


def scan_library_sync(root, rescan, collect_new=False):
    return _scan_library(db(), root, rescan=rescan, collect_new=collect_new)


# Wikis: artist bios and album wikis as sidecar files, pushed into the player's cache #

def _wiki_sources() -> _wiki.Sources:
    return _wiki.Sources(db(), config.user_agent(), fetch=State.mb_fetch, **({"sleep": State.mb_sleep} if State.mb_sleep else {}))


def _wiki_targets():
    return _wiki.target_paths(config.wiki_targets())


def _pictures() -> _avatar.PictureSources:
    return _avatar.PictureSources(_wiki_sources(), fetch_bytes=State.fetch_bytes)


def _covers() -> _cover.CoverSources:
    return _cover.CoverSources(_wiki_sources(), fetch_bytes=State.fetch_bytes)


def _cover_targets():
    return _cover.cover_targets(config.wiki_targets())


@mcp.tool(annotations=WRITE_LOCAL)
@tool_errors
async def wiki_todo(include_all: bool = False) -> dict:
    """Artists and albums in the library that have no bio / wiki text yet, with what a writer needs (tracks, year,
    MusicBrainz ids). Runs an incremental scan first. Then wiki_fill; write the rest with wiki_sources + wiki_write."""
    await scan_library()
    return await asyncio.to_thread(_wiki.missing, db(), config.music_dir(), include_all)


@mcp.tool(annotations=NETWORK)
@tool_errors
async def wiki_fill(limit: int = 10) -> dict:
    """Give every entry without text its English Wikipedia lead paragraph when MusicBrainz links one (attributed,
    CC BY-SA), `limit` entries per call. The rest come back in `to_write` with MusicBrainz facts and links: write
    those yourself, from the facts only, and store each with wiki_write. Call again while `remaining` > 0."""
    await scan_library()
    return await asyncio.to_thread(_wiki.fill, db(), config.music_dir(), _wiki_sources(), _wiki_targets(), limit)


@mcp.tool(annotations=NETWORK)
@tool_errors
async def wiki_sources(artist: str, album: str | None = None) -> dict:
    """Facts and links for one artist (album=None) or album: MusicBrainz type, dates, area, labels, tags, annotation,
    outbound links (Wikidata, Discogs, Bandcamp, homepage), and the Wikipedia lead when there is an article."""
    await scan_library()
    albums, artists = await asyncio.to_thread(_wiki.inventory, db(), config.music_dir())
    entry = _wiki.find_entry(albums, artists, artist, album)
    return await asyncio.to_thread(_wiki.sources_for, _wiki_sources(), entry)


@mcp.tool(annotations=WRITE_LOCAL)
@tool_errors
async def wiki_write(artist: str, content: str, attribution: str, album: str | None = None, url: str | None = None,
                     force: bool = False) -> dict:
    """Store the text for an artist (album=None) or an album: written to a sidecar file beside the music and pushed
    into the player's cache. Plain text, no markup; one paragraph for an album, two for an artist; only what the
    sources support. attribution is shown under the text: name the sources and, if you wrote it, yourself
    (e.g. "Written by Claude from MusicBrainz and Discogs, 2026-09-17"). force replaces existing text."""
    await scan_library()
    return await asyncio.to_thread(_wiki.set_text, db(), config.music_dir(), artist, album, content, attribution, url,
                                   _wiki_targets(), force)


@mcp.tool(annotations=WRITE_LOCAL)
@tool_errors
async def diff_library(playlist_id: int, duration_tolerance_s: float = 5.0) -> dict:
    """Mark playlist tracks already present locally (MusicBrainz id, then ISRC, then normalised artist + title
    with duration within tolerance). Run scan_library first."""
    db().get_playlist(playlist_id)

    if db().library_count() == 0:
        raise ValueError("the library index is empty; run scan_library first")

    return diff_playlist(db(), playlist_id, tolerance_s=duration_tolerance_s)


# Matching #

@mcp.tool(annotations=NETWORK)
@tool_errors
async def match_playlist(
    playlist_id: int,
    prefer_formats: list[str] | None = None,
    allow_formats: list[str] | None = None,
    min_bitrate: int | None = None,
    album_mode: Literal["auto", "on", "off"] = "auto",
    max_tracks: int | None = None,
    harvest_seconds: float = 10.0,
    duration_tolerance_s: float = 3.0,
) -> dict:
    """Start a background job that searches Soulseek (through Nicotine+) for every pending track, scores the
    results and stores the best candidates per track; nothing is downloaded. Album mode groups tracks of one
    release and inspects whole folders. Poll playlist_status; the job honours the bridge's search rate limit."""
    db().get_playlist(playlist_id)
    active = State.jobs.get(playlist_id)

    if active and not active.done():
        raise ValueError(f"a matching job is already running for playlist {playlist_id}")

    await bridge().status()  # fail early if Nicotine+ is unreachable
    prefs = _match_prefs(prefer_formats, allow_formats, min_bitrate, album_mode, max_tracks, harvest_seconds, duration_tolerance_s)
    pending = len([r for r in db().tracks(playlist_id, statuses=["pending", "searching"])])
    job_id = db().create_job(playlist_id, "match", {"total": pending})
    job = MatchJob(db(), bridge(), prefs)
    State.jobs[playlist_id] = asyncio.create_task(job.run(job_id, playlist_id))
    return {"job_id": job_id, "playlist_id": playlist_id, "tracks_to_match": pending,
            "note": "poll playlist_status; then review_candidates"}


def _match_prefs(prefer_formats, allow_formats, min_bitrate, album_mode, max_tracks, harvest_seconds, duration_tolerance_s):
    prefs = MatchPrefs(album_mode=album_mode, max_tracks=max_tracks, harvest_seconds=max(0.1, harvest_seconds),
                       min_bitrate=min_bitrate, duration_tolerance_s=duration_tolerance_s)

    if prefer_formats is not None:
        prefs.prefer_formats = [f.lower().lstrip(".") for f in prefer_formats]
    if allow_formats is not None:
        prefs.allow_formats = [f.lower().lstrip(".") for f in allow_formats]

    prefs.poll_interval_s = min(prefs.poll_interval_s, prefs.harvest_seconds)
    return prefs


@mcp.tool(annotations=WRITE_LOCAL)
@tool_errors
async def cancel_job(playlist_id: int) -> dict:
    """Cancel the running matching job of a playlist (progress so far is kept)."""
    task = State.jobs.get(playlist_id)

    if not task or task.done():
        raise ValueError(f"no running job for playlist {playlist_id}")

    task.cancel()
    return {"cancelled": True, "playlist_id": playlist_id}


@mcp.tool(annotations=READ_ONLY)
@tool_errors
async def review_candidates(
    playlist_id: int,
    status: str = "candidates",
    min_confidence: float | None = None,
    max_confidence: float | None = None,
    limit: int = 25,
    offset: int = 0,
    candidates_per_track: int = 3,
) -> dict:
    """Page through tracks in a status (default 'candidates') with their best candidates and why they scored.
    Use max_confidence to see only doubtful matches, min_confidence for the confident ones."""
    if status not in MATCH_STATUSES:
        raise ValueError(f"status must be one of {', '.join(MATCH_STATUSES)}")

    rows = db().tracks(playlist_id, statuses=[status])

    if min_confidence is not None:
        rows = [r for r in rows if (r["confidence"] or 0) >= min_confidence]
    if max_confidence is not None:
        rows = [r for r in rows if (r["confidence"] or 0) <= max_confidence]

    page = rows[offset: offset + max(1, min(limit, 100))]
    return {"playlist_id": playlist_id, "status": status, "total": len(rows), "offset": offset,
            "tracks": [_row_summary(r, candidates_per_track) for r in page]}


@mcp.tool(annotations=WRITE_LOCAL)
@tool_errors
async def approve(
    track_ids: list[int] | None = None,
    playlist_id: int | None = None,
    min_confidence: float | None = None,
    candidate_index: int = 0,
) -> dict:
    """Approve the chosen candidate (default: the best) for tracks. Either give track_ids, or a playlist_id with
    min_confidence to approve every track whose best candidate is at least that confident. Nothing is queued
    yet: queue_approved does that after showing totals."""
    if not track_ids and playlist_id is None:
        raise ValueError("give track_ids, or playlist_id with min_confidence")

    if track_ids:
        rows = [db().track(t) for t in track_ids]
    else:
        if min_confidence is None:
            raise ValueError("playlist-wide approval needs min_confidence")
        rows = [r for r in db().tracks(playlist_id, statuses=["candidates"]) if (r["confidence"] or 0) >= min_confidence]

    approved, skipped = [], []

    for row in rows:
        candidates = db().candidates(row)

        if row["status"] not in ("candidates", "approved", "failed") or not candidates:
            skipped.append({"track_id": row["id"], "reason": f"status {row['status']}, no candidates"})
            continue

        index = candidate_index if track_ids else 0

        if index >= len(candidates):
            skipped.append({"track_id": row["id"], "reason": f"only {len(candidates)} candidates"})
            continue

        chosen = candidates.pop(index)
        candidates.insert(0, chosen)
        db().set_match(row["id"], "approved", candidates=candidates, confidence=chosen["confidence"])
        approved.append({"track_id": row["id"], "artist": row["artist"], "title": row["title"],
                         "chosen": _candidate_summary(chosen)})

    return {"approved": len(approved), "tracks": approved[:50], "skipped": skipped[:50]}


@mcp.tool(annotations=WRITE_LOCAL)
@tool_errors
async def skip_tracks(track_ids: list[int], reason: str = "skipped by user") -> dict:
    """Mark tracks as skipped so matching and M3U reporting leave them alone."""
    for track_id in track_ids:
        db().track(track_id)
        db().set_match(track_id, "skipped", last_error=reason)

    return {"skipped": len(track_ids)}


def _queue_plan(playlist_id, track_ids=None):
    rows = db().tracks(playlist_id, statuses=["approved"])

    if track_ids is not None:
        rows = [r for r in rows if r["id"] in set(track_ids)]

    files, folders = [], {}

    for row in rows:
        candidates = db().candidates(row)

        if not candidates:
            continue

        chosen = candidates[0]

        if chosen["kind"] == "folder":
            key = (chosen["user"], chosen["folder"])
            entry = folders.setdefault(key, {"user": chosen["user"], "folder": chosen["folder"], "tracks": [],
                                             "size": chosen.get("size") or 0})
            entry["tracks"].append(row)
        else:
            files.append((row, chosen))

    return files, folders


@mcp.tool(annotations=NETWORK)
@tool_errors
async def queue_approved(playlist_id: int, confirm: bool = False) -> dict:
    """Show what queue_approved(confirm=True) would download: track count, total size and the users involved.
    Only with confirm=True are the transfers queued in Nicotine+ (single files, or whole folders in album mode).
    Always show these totals to the user and wait for their explicit OK before confirming."""
    db().get_playlist(playlist_id)
    files, folders = _queue_plan(playlist_id)
    summary = _queue_summary(playlist_id, files, folders)

    if not confirm:
        summary["note"] = "nothing queued; call again with confirm=True after the user agrees"
        return summary

    summary.update(await _queue(files, folders))
    return summary


def _queue_summary(playlist_id, files, folders) -> dict:
    users = sorted({c["user"] for _, c in files} | {f["user"] for f in folders.values()})
    total_bytes = sum(c.get("size") or 0 for _, c in files) + sum(f["size"] for f in folders.values())
    return {
        "playlist_id": playlist_id, "tracks": len(files) + sum(len(f["tracks"]) for f in folders.values()),
        "single_files": len(files), "folders": len(folders), "total_mb": round(total_bytes / 1048576, 1),
        "users": users,
    }


async def _queue(files, folders) -> dict:
    """Queue single files and whole folders in Nicotine+; marks rows queued/failed. Returns {queued, errors}."""
    queued, errors = 0, []

    for row, chosen in files:
        try:
            attrs = {k: chosen.get(k) for k in ("bitrate", "duration", "vbr", "sample_rate", "bit_depth")}
            result = await bridge().call("download_file", username=chosen["user"], path=chosen["path"],
                                         size=chosen.get("size") or 0, attrs=attrs)
            db().set_match(row["id"], "queued", download_id=result["queued"][0]["download_id"], last_error=None)
            queued += 1
        except BridgeError as error:
            errors.append({"track_id": row["id"], "error": str(error)})
            db().set_match(row["id"], "failed", last_error=str(error))

    for entry in folders.values():
        try:
            await bridge().call("download_folder", username=entry["user"], folder_path=entry["folder"])
        except BridgeError as error:
            for row in entry["tracks"]:
                errors.append({"track_id": row["id"], "error": str(error)})
                db().set_match(row["id"], "failed", last_error=str(error))
            continue

        for row in entry["tracks"]:
            chosen = db().candidates(row)[0]
            expected = chosen.get("expected_path")
            db().set_match(row["id"], "queued", download_id=download_id(entry["user"], expected) if expected else None,
                           last_error=None)
            queued += 1

    return {"queued": queued, "errors": errors}


async def _next_candidate(row, failed_user, job_failures):
    candidates = db().candidates(row)
    remaining = [c for c in candidates[1:] if c["kind"] == "file" and job_failures.get(c["user"], 0) < 2]

    if not remaining or row["attempts"] >= MatchPrefs.max_attempts:
        return None

    chosen = remaining[0]
    attrs = {k: chosen.get(k) for k in ("bitrate", "duration", "vbr", "sample_rate", "bit_depth")}
    result = await bridge().call("download_file", username=chosen["user"], path=chosen["path"],
                                 size=chosen.get("size") or 0, attrs=attrs)
    reordered = [chosen] + [c for c in candidates if c is not chosen]
    db().set_match(row["id"], "queued", candidates=reordered, confidence=chosen["confidence"],
                   download_id=result["queued"][0]["download_id"], last_error=f"retry after {failed_user} failed",
                   bump_attempts=True)
    return chosen


@mcp.tool(annotations=NETWORK)
@tool_errors
async def sync_downloads(playlist_id: int, retry: bool = True) -> dict:
    """Map queued transfers to their Nicotine+ status: done (with local path), downloading, queued, or failed.
    With retry, a failed transfer is re-queued from the next candidate (users that failed twice are skipped)."""
    db().get_playlist(playlist_id)
    rows = db().tracks(playlist_id, statuses=["queued", "downloading"])

    if not rows:
        return {"playlist_id": playlist_id, "checked": 0, "counts": {k: v for k, v in db().status_counts(playlist_id).items() if v}}

    listed = await bridge().call("list_downloads", limit=2000)
    by_id = {d["download_id"]: d for d in listed["downloads"]}
    job = db().conn.execute("SELECT id FROM jobs WHERE playlist_id = ? ORDER BY id DESC LIMIT 1", (playlist_id,)).fetchone()
    job_id = job["id"] if job else None
    failures = db().user_failures(job_id) if job_id else {}
    changes = {"done": 0, "downloading": 0, "queued": 0, "failed": 0, "retried": 0, "missing": 0}
    finished: list[str] = []

    for row in rows:
        transfer = by_id.get(row["download_id"]) if row["download_id"] else None

        if transfer is None:
            changes["missing"] += 1
            continue

        status = transfer["status"]

        if status == "Finished":
            local = os.path.join(transfer["folder"] or "", transfer["path"].rpartition("\\")[2])
            db().set_match(row["id"], "done", local_path=local, last_error=None)
            changes["done"] += 1
            finished.append(local)
        elif status == "Transferring":
            db().set_match(row["id"], "downloading")
            changes["downloading"] += 1
        elif status in FAILED_TRANSFER_STATUSES:
            user = transfer["user"]

            if job_id:
                db().penalise_user(job_id, user)
                failures[user] = failures.get(user, 0) + 1

            replacement = await _next_candidate(row, user, failures) if retry else None

            if replacement is None:
                db().set_match(row["id"], "failed", last_error=f"{status} from {user}")
                changes["failed"] += 1
            else:
                changes["retried"] += 1
        else:
            db().set_match(row["id"], "queued")
            changes["queued"] += 1

    result = {"playlist_id": playlist_id, "checked": len(rows), **changes,
              "counts": {k: v for k, v in db().status_counts(playlist_id).items() if v}}

    if finished and config.auto_tidy():
        try:
            tidied = await _tidy_files(finished)
        except (TidyError, FileNotFoundError, OSError) as error:
            result["tidy_error"] = f"{error}; the files are where Nicotine+ put them, run tidy_new later"
        else:
            if tidied:
                result["tidied"] = tidied

    return result


@mcp.tool(annotations=WRITE_LOCAL)
@tool_errors
async def write_m3u(playlist_id: int, path: str | None = None, relative_to: str | None = None) -> dict:
    """Write the playlist as M3U8 in its original order using the local files (in_library and done tracks), and
    store it under the same name in the player's MPD (the `mpd` field says how that went) so it shows in Flaclify.
    Tracks not owned yet are listed in the result, never silently dropped. Default path: <music dir>/Playlists/."""
    playlist = db().get_playlist(playlist_id)
    entries = [{"position": r["position"] + 1, "title": r["title"], "artist": r["artist"], "duration_ms": r["duration_ms"],
                "local_path": r["local_path"] if r["status"] in ("in_library", "done") else None, "status": r["status"]}
               for r in db().tracks(playlist_id)]
    safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in playlist["name"]).strip() or "playlist"
    target = Path(path).expanduser() if path else config.music_dir() / "Playlists" / f"{safe}.m3u8"
    relative = Path(relative_to).expanduser() if relative_to else None
    result = _write_m3u(target, playlist["name"], entries, relative)
    result["missing_count"] = len(result["missing"])
    result["missing"] = result["missing"][:50]
    result["mpd"] = await asyncio.to_thread(_mpd.save_playlist, playlist["name"], [e["local_path"] for e in entries if e["local_path"]])
    return result


# Artist pictures #

@mcp.tool(annotations=WRITE_LOCAL)
@tool_errors
async def avatar_todo(include_all: bool = False) -> dict:
    """Artists in the library with no picture beside their music (<Artist>/artist.jpg), which is what the player
    shows as the artist's avatar. Runs an incremental scan first. Then avatar_fill; give the rest one with avatar_set."""
    await scan_library()
    return await asyncio.to_thread(_avatar.missing, db(), config.music_dir(), include_all)


@mcp.tool(annotations=NETWORK)
@tool_errors
async def avatar_fill(limit: int = 10, providers: str = "local,wikidata,musicbrainz,deezer") -> dict:
    """Find a picture for every artist without one, `limit` artists per call, from the first provider that has one:
    a picture a tagger left in the artist folder, the artist's Wikidata portrait (Wikimedia Commons, with author
    and licence), a MusicBrainz image relation, then Deezer's public artist picture (exact name match only). The
    picture is saved beside the music, its origin in <music>/.wiki/avatars.json, and pushed into the player's
    cache so it shows at once. `not_found` artists need one from the user: avatar_set. Call again while
    `remaining` > 0. providers narrows or reorders the sources (e.g. "wikidata,musicbrainz" to leave Deezer out)."""
    await scan_library()
    return await asyncio.to_thread(_avatar.fill, db(), config.music_dir(), _pictures(), _wiki_targets(), limit, providers)


@mcp.tool(annotations=NETWORK)
@tool_errors
async def avatar_set(artist: str, source: str, attribution: str | None = None) -> dict:
    """Use one picture for an artist: `source` is a local image file or an http(s) URL (jpg, png or webp, at least
    200 px on the short edge). Saved beside the music, replacing any previous picture, and pushed into the player.
    attribution names the photographer or site when known."""
    await scan_library()
    return await asyncio.to_thread(_avatar.set_image, db(), config.music_dir(), artist, source, _pictures(), _wiki_targets(),
                                   attribution)


@mcp.tool(annotations=WRITE_LOCAL)
@tool_errors
async def avatar_push(artist: str | None = None) -> dict:
    """Copy the pictures beside the music into the players' caches again (one artist, or every artist with a
    picture), e.g. after the player's cache was cleared or a picture was replaced by hand."""
    await scan_library()
    albums, artists = await asyncio.to_thread(_wiki.inventory, db(), config.music_dir())
    entries = [_wiki.find_entry(albums, artists, artist)] if artist else artists
    return await asyncio.to_thread(_avatar.push, config.music_dir(), entries, _wiki_targets())


# Album covers #

@mcp.tool(annotations=WRITE_LOCAL)
@tool_errors
async def cover_todo(include_all: bool = False) -> dict:
    """Albums with no cover file beside their music (<album folder>/cover.jpg, what MPD's albumart and the player
    show) or with tracks that carry no embedded picture. Runs an incremental scan first. Then cover_fill; give the
    rest one with cover_set."""
    await scan_library()
    return await asyncio.to_thread(_cover.missing, db(), config.music_dir(), include_all)


@mcp.tool(annotations=NETWORK)
@tool_errors
async def cover_fill(limit: int = 10, providers: str = "local,coverart,deezer,itunes", embed: bool = True) -> dict:
    """Find a cover for every album without one, `limit` albums per call, from the first provider that has one: a
    picture a tagger left in the album folder or embedded in a track, the Cover Art Archive front (the tagged
    release, else the release group MusicBrainz finds), Deezer's public album search, the iTunes Search API
    (exact artist and title match only; the edition suffix is dropped for a second try). The cover is saved beside
    the music as cover.jpg, embedded in every track that has no picture (never replacing one; embed=False leaves
    the tags alone), its origin kept in <music>/.wiki/covers.json, and written into the player's cache, clearing
    its failed-lookup memo, so it shows at once. `not_found` albums need one from the user: cover_set. Call again
    while `remaining` > 0. providers narrows or reorders the sources."""
    await scan_library()
    return await asyncio.to_thread(_cover.fill, db(), config.music_dir(), _covers(), _cover_targets(), limit, providers, embed)


@mcp.tool(annotations=NETWORK)
@tool_errors
async def cover_set(artist: str, album: str, source: str, attribution: str | None = None, embed: bool = True) -> dict:
    """Use one cover for an album: `source` is a local image file or an http(s) URL (jpg, png or webp, at least
    300 px on the short edge). Saved beside the music as cover.jpg, replacing any previous cover file, embedded in
    the tracks that have no picture, and pushed into the player. attribution names the photographer or site."""
    await scan_library()
    return await asyncio.to_thread(_cover.set_image, db(), config.music_dir(), artist, album, source, _covers(),
                                   _cover_targets(), attribution, embed)


@mcp.tool(annotations=WRITE_LOCAL)
@tool_errors
async def cover_push(artist: str | None = None, album: str | None = None) -> dict:
    """Write the cover files beside the music into the players' caches again (one album, one artist's albums, or
    every album with a cover file), e.g. after the player's cache was cleared or a cover was replaced by hand."""
    await scan_library()
    albums, artists = await asyncio.to_thread(_wiki.inventory, db(), config.music_dir())

    if artist and album:
        entries = [_wiki.find_entry(albums, artists, artist, album)]
    elif artist:
        entries = [a for a in albums if _wiki._fold(a["artist"]) == _wiki._fold(artist)]

        if not entries:
            raise LookupError(f"artist {artist!r} is not in the library index; check the spelling or run flacli scan")
    else:
        entries = albums

    return await asyncio.to_thread(_cover.push, config.music_dir(), entries, _cover_targets())


# Tidy #

@mcp.tool(annotations=WRITE_LOCAL)
@tool_errors
async def tidy_analyse(music_dir: str | None = None) -> dict:
    """Dry run of the library tidy: plan tag fixes, lossy-duplicate deletions and moves to Artist/Album/NN - Title.

    Writes <music_dir>/.tidy/report.txt (read it whole for the detail) and plan.json, and returns a summary with the
    open questions that need decisions in <music_dir>/.tidy/approved.py. Changes nothing in the library.
    """
    root = Path(music_dir).expanduser() if music_dir else config.music_dir()
    return await asyncio.to_thread(lambda: Tidy(root).analyse())


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False))
@tool_errors
async def tidy_apply(music_dir: str | None = None, confirm: bool = False, force: bool = False) -> dict:
    """Apply the tidy plan: backup tags, write tags, delete planned duplicates, move files, prune empty folders.

    Without confirm=True this only returns the current plan summary (same as tidy_analyse) and changes nothing; call it
    with confirm=True only after the user has seen the deletions by name and said yes. Refuses while audio files are
    still being written (a download in progress) unless force=True.
    """
    root = Path(music_dir).expanduser() if music_dir else config.music_dir()

    if not confirm:
        summary = await asyncio.to_thread(lambda: Tidy(root).analyse())
        summary["applied"] = False
        summary["note"] = "Nothing changed. Show the deletions and counts to the user; call again with confirm=True on their yes."
        return summary

    result = await asyncio.to_thread(lambda: Tidy(root).apply(force=force))
    result["applied"] = True
    result["mpd"] = await asyncio.to_thread(_mpd.notify_paths, [str(root)])
    return result


def _under(root: Path, path: str) -> bool:
    try:
        Path(path).resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


async def _tidy_files(paths, music_dir: Path | None = None) -> dict | None:
    """Scoped tidy of just-arrived files, then fix up match rows and the library index. None when nothing applies."""
    root = music_dir or config.music_dir()
    inside = [p for p in paths if p and Path(p).is_file() and _under(root, p)]

    if not inside:
        return None

    result = await asyncio.to_thread(lambda: Tidy(root).apply_new(inside))
    moves = result.pop("moves")
    result["relocated_tracks"] = db().relocate(moves) if moves else 0
    result["reindexed"] = reindex_moved(db(), moves) if moves else 0
    result["moved_to"] = sorted({os.path.relpath(os.path.dirname(new), root) for new in moves.values()})[:50]
    result["auto_tidy"] = config.auto_tidy()
    landed = {os.path.abspath(old): new for old, new in moves.items()}
    result["mpd"] = await asyncio.to_thread(_mpd.notify_paths, [landed.get(os.path.abspath(path), path) for path in inside])
    return result


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False))
@tool_errors
async def tidy_new(paths: list[str] | None = None, music_dir: str | None = None) -> dict:
    """Tidy only newly arrived tracks: normalise their tags and file them as Artist/Album/NN - Title, touching nothing
    else. Never deletes; files it cannot place (missing tags, lossy copy of an owned FLAC, target exists) are held in
    place and listed. sync_downloads does this by itself for the tracks it marks done (unless auto_tidy is off); call
    this for tracks that arrived any other way. Without paths, an incremental library scan decides what is new; files
    written in the last few minutes are left to settle."""
    root = Path(music_dir).expanduser() if music_dir else config.music_dir()

    if paths:
        candidates, settling = [str(Path(p).expanduser()) for p in paths], []
    else:
        scanned = await asyncio.to_thread(scan_library_sync, root, False, True)
        candidates, settling = [], []
        cutoff = time.time() - Tidy(root).settle_seconds

        for path in scanned["new_paths"]:
            (settling if os.path.getmtime(path) > cutoff else candidates).append(path)

    result = await _tidy_files(candidates, root) or {"root": str(root), "requested": 0, "moved": 0, "held": []}
    result["settling"] = settling[:50]
    result["note"] = ("nothing new to tidy" if not candidates else
                      "only the listed files were touched; deletions and open questions wait for /music-tidy")
    return result


# beets (optional) #

def _beets():
    return Beets()


@mcp.tool(annotations=READ_ONLY)
@tool_errors
async def beets_status() -> dict:
    """Whether `beet` is installed, its version, config path, library directory and import settings (copy/move)."""
    return await asyncio.to_thread(_beets().status)


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True))
@tool_errors
async def beets_import(playlist_id: int, confirm: bool = False, move: bool = False) -> dict:
    """Hand this playlist's finished downloads to beets. Groups them by folder: a folder where one MusicBrainz
    release dominates is imported as an album with `--search-id <release id>`, anything else as singletons with
    the recording ids as hints. Without confirm it only runs `beet import --pretend` and returns the plan (folders,
    files, the exact commands). With confirm=True it imports in quiet mode (beets skips anything it is unsure
    about and logs it to <data>/beets-import.log) and then updates each track's local path to where beets put
    it. Files are copied or moved according to the beets config; move=True forces a move. Show the plan and get
    the user's yes before confirm=True."""
    db().get_playlist(playlist_id)
    beets = _beets()
    rows = db().tracks(playlist_id, statuses=["done"])
    plan = Beets.plan(rows)

    if not plan:
        return {"playlist_id": playlist_id, "folders": [], "note": "no finished downloads to import"}

    if not confirm:
        plan = await asyncio.to_thread(beets.dry_run, plan, move)
        return {"playlist_id": playlist_id, "dry_run": True, "folders": plan, "beets": await asyncio.to_thread(beets.status),
                "note": "nothing imported; call again with confirm=True after the user has approved this plan"}

    results = await asyncio.to_thread(beets.import_folders, plan, move)
    by_id = {r["id"]: r for r in rows}
    relocated = 0

    def relocate():
        nonlocal relocated

        for entry in results:
            if not entry["imported"]:
                continue

            for track_id in entry["track_ids"]:
                row = by_id[track_id]
                new_path = beets.path_for_recording(row["mb_recording_id"])

                if new_path and new_path != row["local_path"]:
                    db().set_match(track_id, "done", local_path=new_path)
                    relocated += 1

    await asyncio.to_thread(relocate)
    imported = sum(1 for r in results if r["imported"])
    return {"playlist_id": playlist_id, "dry_run": False, "folders_imported": imported,
            "folders_failed": len(results) - imported, "tracks_relocated": relocated,
            "log": str(config.data_dir() / "beets-import.log"), "folders": results}


# Troi (optional) #

def _troi():
    return Troi()


@mcp.tool(annotations=READ_ONLY)
@tool_errors
async def troi_status() -> dict:
    """Whether the ListenBrainz Troi content resolver is installed and has indexed the collection."""
    return await asyncio.to_thread(_troi().status)


@mcp.tool(annotations=WRITE_LOCAL)
@tool_errors
async def troi_scan(music_dir: str | None = None, force: bool = False) -> dict:
    """Index the collection with `troi db scan` (creates <data>/troi.db first if needed). Only files carrying
    MusicBrainz tags become resolvable by id; the rest only through fuzzy artist + title. Slow on a large
    library the first time; incremental afterwards unless force=True."""
    root = Path(music_dir).expanduser() if music_dir else config.music_dir()
    return await asyncio.to_thread(_troi().scan, root, force)


@mcp.tool(annotations=WRITE_LOCAL)
@tool_errors
async def troi_resolve(playlist_id: int, threshold: float = 0.8) -> dict:
    """Ask Troi to find this playlist's still-missing tracks in the indexed collection (recording MBID first,
    then fuzzy artist + title above the threshold). Tracks it finds on disk are marked in_library with their path,
    so they are not searched for on Soulseek. Run resolve_playlist first so tracks carry MusicBrainz ids."""
    db().get_playlist(playlist_id)
    rows = [r for r in db().tracks(playlist_id) if r["status"] not in ("in_library", "done", "skipped")]

    if not rows:
        return {"playlist_id": playlist_id, "queried": 0, "found": 0, "counts": {k: v for k, v in db().status_counts(playlist_id).items() if v}}

    found = await asyncio.to_thread(_troi().resolve, rows, threshold)
    by_id = {r["id"]: r for r in rows}
    tracks = []

    for track_id, path in found.items():
        db().set_match(track_id, "in_library", local_path=path, last_error=None)
        row = by_id[track_id]
        tracks.append({"track_id": track_id, "position": row["position"] + 1, "artist": row["artist"], "title": row["title"], "local_path": path})

    return {"playlist_id": playlist_id, "queried": len(rows), "found": len(found), "tracks": tracks[:50],
            "counts": {k: v for k, v in db().status_counts(playlist_id).items() if v}}


@mcp.tool(annotations=READ_ONLY)
@tool_errors
async def library_status() -> dict:
    """Health: data dir, database version, indexed files, and whether the Nicotine+ bridge answers."""
    result = {"version": __version__, "data_dir": str(config.data_dir()), "music_dir": str(config.music_dir()),
              "schema_version": db().schema_version, "library_files": db().library_count(),
              "playlists": len(db().list_playlists()), "bridge_socket": bridge().socket_path,
              "auto_tidy": config.auto_tidy()}

    try:
        status = await bridge().status()
        result["bridge"] = {"reachable": True, "protocol": status.get("protocol"), "online": status.get("online"),
                            "nicotine_version": status.get("nicotine_version"), "rate_limit": status.get("rate_limit")}
    except BridgeError as error:
        result["bridge"] = {"reachable": False, "error": str(error)}

    return result


def main():
    mcp.run()


if __name__ == "__main__":
    main()
