# SPDX-License-Identifier: GPL-3.0-or-later
"""MCP server exposing a running Nicotine+ (Soulseek) client to any MCP client.

Requires the "MCP Bridge" plugin to be enabled in Nicotine+.
Socket path: $NICOTINE_MCP_SOCKET, the bridge_socket setting, else $XDG_RUNTIME_DIR/nicotine-mcp.sock.
Run it with `flacli mcp --soulseek`; the tools are also the `search`/`downloads` CLI commands.
"""

import asyncio
import json
import os

from collections import defaultdict
from typing import Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import config

PROTOCOL_VERSIONS = {1, 2}
LOSSLESS = {"flac", "wav", "ape", "wv", "aif", "aiff", "dsf", "dff", "tak", "tta"}
TRANSFER_STATUSES = (
    "Queued", "Getting status", "Transferring", "Paused", "Cancelled", "Filtered", "Finished",
    "User logged off", "Connection closed", "Connection timeout", "Download folder error", "Local file error",
)

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)

mcp = MCPServer(
    name="soulseek",
    instructions=(
        "Controls the user's local Nicotine+ Soulseek client. Soulseek search results trickle in from peers "
        "over tens of seconds, so re-query get_search_results for more. Prefer folders from users with a free "
        "upload slot, a short queue and high speed. To grab a whole album, use download_folder with the folder "
        "path shown in results. Download IDs from list_downloads are used for cancel/retry/clear."
    ),
)


def _socket_path():
    return config.bridge_socket_path()


async def call(method, **params):
    path = _socket_path()

    try:
        reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(path, limit=64 * 1024 * 1024), 5)
    except (FileNotFoundError, ConnectionRefusedError, asyncio.TimeoutError) as error:
        raise ToolError(
            f"Cannot reach Nicotine+ at {path} ({type(error).__name__}). Is Nicotine+ running with the "
            "'MCP Bridge' plugin enabled?"
        ) from None

    try:
        payload = {"method": method, "params": {k: v for k, v in params.items() if v is not None}}
        writer.write(json.dumps(payload).encode() + b"\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), 30)
    finally:
        writer.close()

    if not line:
        raise ToolError("Nicotine+ closed the connection without replying")

    response = json.loads(line)

    if not response.get("ok"):
        error = response.get("error", "unknown error from Nicotine+")

        if error == "rate_limited":
            raise ToolError(
                f"Soulseek search rate limit reached in Nicotine+; retry in {response.get('retry_after', '?')} s "
                "(the limit protects the account from a 30-minute server ban)"
            )

        raise ToolError(error)

    return response["result"]


def _ext(path):
    name = path.rpartition("\\")[2]
    return name.rpartition(".")[2].lower() if "." in name else ""


def _mb(size):
    return round((size or 0) / 1048576, 1)


def _quality(r):
    ext = _ext(r["path"])

    if ext in LOSSLESS:
        parts = [ext]
        if r.get("bit_depth"):
            parts.append(f"{r['bit_depth']}bit")
        if r.get("sample_rate"):
            parts.append(f"{r['sample_rate'] / 1000:g}kHz")
        return " ".join(parts)

    if r.get("bitrate"):
        return f"{ext} {r['bitrate']}{'v' if r.get('vbr') else ''}kbps"

    return ext or "?"


def _group_folders(results, max_folders, files_per_folder):
    folders = defaultdict(list)

    for r in results:
        folders[(r["user"], r["path"].rpartition("\\")[0])].append(r)

    groups = []

    for (user, folder), files in folders.items():
        first = files[0]
        qualities = sorted({_quality(f) for f in files})
        groups.append({
            "user": user,
            "folder": folder,
            "free_slot": first["free_slot"],
            "speed_kbps": round(first["speed"] * 8 / 1000),
            "queue": first["queue"],
            "files_matched": len(files),
            "total_mb": _mb(sum(f["size"] or 0 for f in files)),
            "quality": ", ".join(qualities[:3]) + ("…" if len(qualities) > 3 else ""),
            "files": [
                {
                    "id": f["id"],
                    "name": f["path"].rpartition("\\")[2],
                    "mb": _mb(f["size"]),
                    "quality": _quality(f),
                    **({"length": f"{f['duration'] // 60}:{f['duration'] % 60:02d}"} if f.get("duration") else {}),
                    **({"private": True} if f.get("private") else {}),
                }
                for f in sorted(files, key=lambda f: f["path"].lower())[:files_per_folder]
            ],
            **({"files_not_shown": len(files) - files_per_folder} if len(files) > files_per_folder else {}),
        })

    groups.sort(key=lambda g: (not g["free_slot"], g["queue"], -g["files_matched"], -g["speed_kbps"]))
    return groups[:max_folders], len(groups)


async def _results_summary(search_id, max_folders, files_per_folder, **filters):
    data = await call("search_results", search_id=search_id, limit=5000, **filters)
    groups, total_groups = _group_folders(data["results"], max_folders, files_per_folder)

    return {
        "search_id": data["search_id"],
        "query": data["query"],
        "age_seconds": data["age_seconds"],
        "files_received": data["total_files"],
        "users_responded": data["total_users"],
        "files_matching_filters": data["matched"],
        "folders_matching": total_groups,
        "folders_shown": len(groups),
        **({"note": "per-search result cap reached; narrow the query"} if data["truncated"] else {}),
        "folders": groups,
    }


@mcp.tool(annotations=READ_ONLY)
async def nicotine_status() -> dict:
    """Connection state, logged-in username, download folder, download counts by status,
    tracked searches, and recent folder-download requests."""
    status = await call("status")

    if status.get("protocol") not in PROTOCOL_VERSIONS:
        status["warning"] = "plugin/server protocol mismatch; update both halves"
    elif status.get("protocol") == 1:
        status["warning"] = "Nicotine+ plugin is protocol v1: no search rate limiting or folder browsing; reinstall it"

    return status


@mcp.tool(annotations=READ_ONLY)
async def search(
    query: str,
    wait_seconds: int = 12,
    mode: Literal["global", "buddies", "rooms", "user"] = "global",
    usernames: list[str] | None = None,
    room: str | None = None,
    lossless_only: bool = False,
    extensions: list[str] | None = None,
    min_bitrate: int | None = None,
    free_slot_only: bool = False,
    max_folders: int = 15,
    files_per_folder: int = 25,
) -> dict:
    """Search Soulseek and return results grouped by (user, folder), best candidates first.

    Soulseek matches every word against the full file path; use artist + album words, avoid
    punctuation. Results keep arriving after this returns; call get_search_results later with the
    same search_id for more. mode="user" needs usernames; mode="rooms" needs room.
    Filters (lossless_only, extensions like ["flac"], min_bitrate in kbps for lossy files,
    free_slot_only) only affect what is returned, not what is collected."""
    started = await call("search", query=query, mode=mode, users=usernames, room=room)
    await asyncio.sleep(max(0, min(wait_seconds, 60)))

    return await _results_summary(
        started["search_id"], max_folders, files_per_folder,
        lossless_only=lossless_only, extensions=extensions, min_bitrate=min_bitrate, free_slot_only=free_slot_only,
    )


@mcp.tool(annotations=READ_ONLY)
async def get_search_results(
    search_id: int,
    lossless_only: bool = False,
    extensions: list[str] | None = None,
    min_bitrate: int | None = None,
    free_slot_only: bool = False,
    username: str | None = None,
    path_contains: str | None = None,
    max_folders: int = 15,
    files_per_folder: int = 25,
) -> dict:
    """Re-read (and re-filter) an existing search. path_contains = space-separated words that must all
    appear in the path (case-insensitive); username restricts to one peer, handy for seeing a full folder."""
    return await _results_summary(
        search_id, max_folders, files_per_folder,
        lossless_only=lossless_only, extensions=extensions, min_bitrate=min_bitrate,
        free_slot_only=free_slot_only, username=username, path_contains=path_contains,
    )


@mcp.tool(annotations=READ_ONLY)
async def list_searches() -> dict:
    """Searches currently tracked by the bridge (id, query, age, file/user counts)."""
    return {"searches": await call("list_searches")}


@mcp.tool(annotations=DESTRUCTIVE)
async def stop_search(search_id: int) -> dict:
    """Stop collecting results for a search and discard them (also closes its tab in Nicotine+)."""
    return await call("stop_search", search_id=search_id)


@mcp.tool(annotations=WRITE)
async def download_files(search_id: int, result_ids: list[int], keep_folder_structure: bool = True) -> dict:
    """Queue specific files from a search by their result ids. With keep_folder_structure, files land in
    <download folder>/<remote parent folder name>/, which keeps album tracks together."""
    return await call(
        "download_results", search_id=search_id, result_ids=result_ids, keep_folder_structure=keep_folder_structure
    )


@mcp.tool(annotations=WRITE)
async def download_folder(username: str, folder_path: str, include_subfolders: bool = False) -> dict:
    """Queue an entire remote folder (e.g. an album) from a user. folder_path is the 'folder' value from
    search results. The peer is asked for the folder listing first, so files appear in list_downloads a few
    seconds later. include_subfolders also grabs e.g. CD1/CD2 or Scans subfolders."""
    return await call(
        "download_folder", username=username, folder_path=folder_path, include_subfolders=include_subfolders
    )


def _folder_listing_summary(data):
    if data["status"] != "ready":
        return data

    folders = []

    for folder, files in data["folders"].items():
        folders.append({
            "folder": folder,
            "files": len(files),
            "total_mb": _mb(sum(f["size"] or 0 for f in files)),
            "entries": [
                {
                    "name": f["name"],
                    "mb": _mb(f["size"]),
                    "quality": _quality(f),
                    **({"length": f"{f['duration'] // 60}:{f['duration'] % 60:02d}"} if f.get("duration") else {}),
                }
                for f in files
            ],
        })

    return {"status": "ready", "user": data["user"], "folder": data["folder"], "total_files": data["total_files"],
            "folders": folders}


@mcp.tool(annotations=READ_ONLY)
async def browse_folder(username: str, folder_path: str, include_subfolders: bool = False, wait_seconds: int = 10) -> dict:
    """Ask a user for the file listing of a folder WITHOUT downloading anything (needs Nicotine+ plugin
    protocol v2). Waits up to wait_seconds for the reply; if still pending, call get_folder_contents later.
    Use it to check an album folder's track count and quality before download_folder."""
    await call("folder_contents", username=username, folder_path=folder_path, include_subfolders=include_subfolders)
    deadline = asyncio.get_running_loop().time() + max(0, min(wait_seconds, 60))

    while True:
        data = await call("folder_contents_result", username=username, folder_path=folder_path)

        if data["status"] != "pending" or asyncio.get_running_loop().time() >= deadline:
            return _folder_listing_summary(data)

        await asyncio.sleep(1)


@mcp.tool(annotations=READ_ONLY)
async def get_folder_contents(username: str, folder_path: str) -> dict:
    """Re-check a folder listing requested earlier with browse_folder (status: pending, ready, timed_out, unknown)."""
    return _folder_listing_summary(await call("folder_contents_result", username=username, folder_path=folder_path))


@mcp.tool(annotations=READ_ONLY)
async def list_downloads(
    statuses: list[Literal[TRANSFER_STATUSES]] | None = None,
    username: str | None = None,
    limit: int = 50,
) -> dict:
    """List downloads with status, progress (0–1), speed and queue position. Most recent last."""
    data = await call("list_downloads", statuses=statuses, username=username, limit=limit)

    for item in data["downloads"]:
        item["mb"] = _mb(item.pop("size"))
        item["name"] = item["path"].rpartition("\\")[2]
        item["speed_kbps"] = round((item.pop("speed") or 0) * 8 / 1000)

    return data


@mcp.tool(annotations=DESTRUCTIVE)
async def cancel_downloads(download_ids: list[str]) -> dict:
    """Cancel downloads by download_id (from list_downloads). Partial files stay in the incomplete folder."""
    return await call("cancel_downloads", download_ids=download_ids)


@mcp.tool(annotations=WRITE)
async def retry_downloads(download_ids: list[str]) -> dict:
    """Retry failed, paused or cancelled downloads by download_id."""
    return await call("retry_downloads", download_ids=download_ids)


@mcp.tool(annotations=DESTRUCTIVE)
async def clear_downloads(
    download_ids: list[str] | None = None,
    statuses: list[Literal[TRANSFER_STATUSES]] | None = None,
) -> dict:
    """Remove entries from the download list (does not delete finished files from disk).
    Give download_ids, statuses (e.g. ["Finished"]), or both."""
    return await call("clear_downloads", download_ids=download_ids, statuses=statuses)




def main():
    mcp.run()


if __name__ == "__main__":
    main()
