# SPDX-License-Identifier: GPL-3.0-or-later
"""flacli command line. Every command prints one JSON object; errors print {"error": ...} and exit 1.

Designed so that any agent with a shell can drive it, including small local models: few commands, obvious
names, defaults that do the right thing, and a `next` hint in status output saying what to run.
"""

import argparse
import asyncio
import json
import sys

from importlib import resources

from mcp.server.mcpserver.exceptions import ToolError

from . import __version__, config, simple
from .bridge import BridgeError
from .connectors import ConnectorError
from .musicbrainz import MusicBrainzError
from .tidy import TidyError

EXPECTED_ERRORS = (LookupError, ValueError, FileNotFoundError, PermissionError, BridgeError, MusicBrainzError, TidyError,
                   ConnectorError, OSError, ToolError)


def _ids(text: str) -> list[int]:
    try:
        return [int(part) for part in text.replace(" ", ",").split(",") if part]
    except ValueError:
        raise ValueError(f"track ids must be integers separated by commas: {text!r}") from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flacli",
        description="Get music onto disk through your own Nicotine+ (Soulseek) client, matched against MusicBrainz and "
                    "your local library. Output is JSON. `flacli guide` explains the workflow to an agent.",
    )
    parser.add_argument("--version", action="version", version=f"flacli {__version__}")
    parser.add_argument("--compact", action="store_true", help="single-line JSON output")
    commands = parser.add_subparsers(dest="command", metavar="command")
    commands.required = True

    p = commands.add_parser("guide", help="print the agent guide: workflow, commands, rules")
    p.add_argument("--short", action="store_true", help="only the quick reference")

    commands.add_parser("doctor", help="health check: Nicotine+ bridge, music dir, config, services")

    p = commands.add_parser("config", help="show or change settings (config file + env overrides)")
    p.add_argument("action", nargs="?", choices=["show", "set", "path"], default="show")
    p.add_argument("key", nargs="?", help="setting name, for set")
    p.add_argument("value", nargs="?", help="new value, for set")

    p = commands.add_parser("get", help="fetch named songs or albums: 'Artist - Title', 'Artist - Album (album)'")
    p.add_argument("items", nargs="+", help="one or more items; an album needs '(album)' after its name")
    p.add_argument("--playlist", default="Requests", help="request playlist to append to (default: Requests)")
    p.add_argument("--min-confidence", type=float, default=0.85, help="auto-queue matches at or above this (default 0.85)")
    p.add_argument("--lossy", action="store_true", help="accept mp3/ogg/opus/m4a as well as flac")
    p.add_argument("--min-bitrate", type=int, help="minimum bitrate for lossy files")
    p.add_argument("--harvest", type=float, default=10.0, help="seconds to collect Soulseek results per search (default 10)")
    p.add_argument("--prepare-only", action="store_true", help="add and resolve the items but fetch nothing")
    p.add_argument("--wait", action="store_true", help="block until matched and downloaded (or --timeout)")
    p.add_argument("--timeout", type=float, default=600, help="seconds to wait with --wait (default 600)")

    p = commands.add_parser("import", help="import a playlist file or a TIDAL/Deezer/YouTube Music URL without syncing it")
    p.add_argument("target", help="file path (Spotify export, Exportify/CSV, M3U, JSPF, XSPF), share URL, or service playlist id")
    p.add_argument("--service", choices=["tidal", "deezer", "youtube-music"], help="needed when target is a bare playlist id")
    p.add_argument("--name", help="pick one playlist from a multi-playlist Spotify export")

    p = commands.add_parser("sync", help="import if needed, resolve, diff against the library, match on Soulseek")
    p.add_argument("target", help="playlist id, file path, or share URL")
    p.add_argument("--service", choices=["tidal", "deezer", "youtube-music"])
    p.add_argument("--name", help="pick one playlist from a multi-playlist export")
    p.add_argument("--yes", action="store_true", help="also queue every match at or above --min-confidence without a further step")
    p.add_argument("--min-confidence", type=float, default=0.85)
    p.add_argument("--lossy", action="store_true")
    p.add_argument("--min-bitrate", type=int)
    p.add_argument("--album-mode", choices=["auto", "on", "off"], default="auto", help="fetch whole folders when most of an album is missing")
    p.add_argument("--harvest", type=float, default=10.0)
    p.add_argument("--max-tracks", type=int, help="match only the first N pending tracks")
    p.add_argument("--wait", action="store_true", help="block until the job (and any queued downloads) finish")
    p.add_argument("--timeout", type=float, default=900)

    p = commands.add_parser("status", help="all playlists, or one playlist's counts, job and downloads")
    p.add_argument("playlist_id", nargs="?", type=int)
    p.add_argument("--wait", action="store_true", help="block until nothing is running or in flight")
    p.add_argument("--timeout", type=float, default=600)

    p = commands.add_parser("review", help="tracks needing a decision, with their best candidates and why")
    p.add_argument("playlist_id", type=int)
    p.add_argument("--status", default="candidates", help="candidates (default), not_found, failed, pending, approved, done ...")
    p.add_argument("--doubtful", action="store_true", help="only tracks whose best candidate is below 0.85")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--offset", type=int, default=0)

    p = commands.add_parser("approve", help="approve candidates: --tracks ids, or --min-confidence for a whole playlist")
    p.add_argument("playlist_id", type=int)
    p.add_argument("--tracks", help="comma-separated track ids")
    p.add_argument("--candidate", type=int, default=0, help="which candidate to take for --tracks (0 = best)")
    p.add_argument("--min-confidence", type=float, help="approve every track at or above this confidence")

    p = commands.add_parser("skip", help="mark tracks as skipped")
    p.add_argument("playlist_id", type=int)
    p.add_argument("--tracks", required=True, help="comma-separated track ids")
    p.add_argument("--reason", default="skipped by user")

    p = commands.add_parser("queue", help="show what would be downloaded; --yes queues it in Nicotine+")
    p.add_argument("playlist_id", type=int)
    p.add_argument("--yes", action="store_true", help="queue the transfers (only after the user has seen the totals)")
    p.add_argument("--min-confidence", type=float, help="first approve every candidate at or above this")

    p = commands.add_parser("cancel", help="stop the running job of a playlist")
    p.add_argument("playlist_id", type=int)

    p = commands.add_parser("m3u", help="write the playlist as M3U8 in its original order")
    p.add_argument("playlist_id", type=int)
    p.add_argument("--path", help="output file (default <music dir>/Playlists/<name>.m3u8)")
    p.add_argument("--relative-to", help="write paths relative to this folder")

    p = commands.add_parser("delete", help="forget a playlist and its match state (files untouched)")
    p.add_argument("playlist_id", type=int)

    p = commands.add_parser("scan", help="index the music library (incremental)")
    p.add_argument("--rescan", action="store_true", help="re-read every file")

    p = commands.add_parser("tidy", help="library tidy: dry run by default, --apply to do it")
    p.add_argument("music_dir", nargs="?", help="library root (default: the configured music dir)")
    p.add_argument("--apply", action="store_true", help="apply the plan (tags, deletions, moves); show the plan first")
    p.add_argument("--force", action="store_true", help="apply even while files were written recently")
    p.add_argument("--new", action="store_true", help="only file newly arrived tracks; never deletes")
    p.add_argument("--paths", nargs="*", help="with --new: specific files to file")

    p = commands.add_parser("service", help="streaming services: status, connect, disconnect, playlists")
    p.add_argument("action", choices=["status", "connect", "disconnect", "playlists"])
    p.add_argument("name", nargs="?", choices=["tidal", "deezer", "youtube-music"])
    p.add_argument("--headers-file", help="youtube-music: file with the request headers copied from a logged-in tab")
    p.add_argument("--auth-file", help="youtube-music: an existing ytmusicapi browser.json")
    p.add_argument("--user", help="deezer: user id or profile URL whose public playlists to list")
    p.add_argument("--force", action="store_true", help="reconnect even if already connected")

    p = commands.add_parser("search", help="raw Soulseek search through Nicotine+ (folders grouped, best first)")
    p.add_argument("query")
    p.add_argument("--wait", type=int, default=10, help="seconds to collect results")
    p.add_argument("--lossless", action="store_true")
    p.add_argument("--max-folders", type=int, default=10)

    p = commands.add_parser("download", help="queue a whole remote folder from a search result")
    p.add_argument("username")
    p.add_argument("folder")

    p = commands.add_parser("downloads", help="Nicotine+ transfer list")
    p.add_argument("--status", help="filter: Queued, Transferring, Finished, ...")
    p.add_argument("--limit", type=int, default=100)

    p = commands.add_parser("mcp", help="run an MCP server over stdio (default: the simple one for small models)")
    p.add_argument("--full", action="store_true", help="every fine-grained tool (43); for capable models")
    p.add_argument("--soulseek", action="store_true", help="only the raw Nicotine+ tools")

    return parser


def guide_text(short=False) -> str:
    text = resources.files("flacli").joinpath("GUIDE.md").read_text(encoding="utf-8")

    if short:
        marker = "## Quick reference"
        start = text.find(marker)
        end = text.find("\n## ", start + len(marker))
        return text[start:end if end > 0 else None].strip() + "\n"

    return text


async def dispatch(args) -> dict | str | None:
    command = args.command

    if command == "guide":
        return guide_text(args.short)
    if command == "doctor":
        return await simple.doctor()
    if command == "config":
        if args.action == "path":
            return {"config_file": str(config.config_path())}
        if args.action == "set":
            if not args.key or args.value is None:
                raise ValueError("usage: flacli config set <key> <value>")
            path = config.write_setting(args.key, args.value)
            return {"written": str(path), args.key: config.get(args.key)}
        return {"config_file": str(config.config_path()), "settings": config.effective()}
    if command == "get":
        result = await simple.get(args.items, playlist=args.playlist, min_confidence=args.min_confidence, lossy=args.lossy,
                                  min_bitrate=args.min_bitrate, harvest_seconds=args.harvest, download=not args.prepare_only)

        if args.wait and result.get("job_id"):
            result["final"] = await simple.wait_for(result["playlist_id"], args.timeout)

        return result
    if command == "import":
        return await simple.import_playlist(args.target, service=args.service, name=args.name)
    if command == "sync":
        result = await simple.sync(args.target, service=args.service, name=args.name, yes=args.yes, min_confidence=args.min_confidence,
                                   lossy=args.lossy, min_bitrate=args.min_bitrate, album_mode=args.album_mode,
                                   harvest_seconds=args.harvest, max_tracks=args.max_tracks)

        if args.wait and result.get("job_id"):
            result["final"] = await simple.wait_for(result["playlist_id"], args.timeout)

        return result
    if command == "status":
        if args.wait and args.playlist_id is not None:
            return await simple.wait_for(args.playlist_id, args.timeout)

        return await simple.status(args.playlist_id)
    if command == "review":
        return await simple.review(args.playlist_id, status_name=args.status, doubtful_only=args.doubtful, limit=args.limit, offset=args.offset)
    if command == "approve":
        if not args.tracks and args.min_confidence is None:
            raise ValueError("give --tracks ids or --min-confidence")

        return await simple.approve(args.playlist_id, track_ids=_ids(args.tracks) if args.tracks else None,
                                    min_confidence=args.min_confidence, candidate=args.candidate)
    if command == "skip":
        return await simple.skip(_ids(args.tracks), reason=args.reason)
    if command == "queue":
        return await simple.queue(args.playlist_id, yes=args.yes, min_confidence=args.min_confidence)
    if command == "cancel":
        return await simple.cancel(args.playlist_id)
    if command == "m3u":
        return await simple.m3u(args.playlist_id, path=args.path, relative_to=args.relative_to)
    if command == "delete":
        return await simple.delete(args.playlist_id)
    if command == "scan":
        return await simple.scan(rescan=args.rescan)
    if command == "tidy":
        if args.new:
            return await simple.tidy_new(paths=args.paths or None, music_dir=args.music_dir)

        return await simple.tidy(apply=args.apply, force=args.force, music_dir=args.music_dir)
    if command == "service":
        headers_raw = None

        if args.headers_file:
            with open(args.headers_file, encoding="utf-8") as handle:
                headers_raw = handle.read()

        return await simple.service(args.action, args.name, headers_raw=headers_raw, auth_file=args.auth_file, user=args.user, force=args.force)
    if command == "search":
        return await simple.search(args.query, wait_seconds=args.wait, lossless_only=args.lossless, max_folders=args.max_folders)
    if command == "download":
        return await simple.download_folder(args.username, args.folder)
    if command == "downloads":
        return await simple.downloads(status_name=args.status, limit=args.limit)

    raise ValueError(f"unknown command {command}")


def run_mcp(args):
    if args.soulseek:
        from . import soulseek_mcp
        soulseek_mcp.main()
    elif args.full:
        from . import server
        server.main()
    else:
        from . import mcp_simple
        mcp_simple.main()


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "mcp":
        run_mcp(args)
        return 0

    try:
        result = asyncio.run(dispatch(args))
    except EXPECTED_ERRORS as error:
        print(json.dumps({"error": str(error), "command": args.command}), file=sys.stdout)
        return 1
    except KeyboardInterrupt:
        print(json.dumps({"error": "interrupted", "command": args.command}))
        return 130
    finally:
        simple.close()

    if isinstance(result, str):
        sys.stdout.write(result)
    else:
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":") if args.compact else None,
                         indent=None if args.compact else 1))

    return 0


if __name__ == "__main__":
    sys.exit(main())
