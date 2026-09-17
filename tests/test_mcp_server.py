"""Drive the stdio MCP server end-to-end against the harness bridge."""

import json
import os
import sys

from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

SOULSEEK_SERVER = ["-m", "flacli.soulseek_mcp"]
LIBRARY_SERVER = ["-m", "flacli.server"]
ALBUM = "@@music\\Test Artist\\Album (2001)"

pytestmark = pytest.mark.anyio


@asynccontextmanager
async def mcp_session(socket_path, args=None, env=None):
    params = StdioServerParameters(
        command=sys.executable, args=args or SOULSEEK_SERVER,
        env={**os.environ, "NICOTINE_MCP_SOCKET": socket_path, **(env or {})},
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


def payload(result):
    if result.structured_content is not None:
        return result.structured_content

    return json.loads(result.content[0].text)


def error_text(result):
    assert result.is_error, "expected the tool call to fail"
    return " ".join(block.text for block in result.content if getattr(block, "text", None))


async def test_tool_list_and_status(bridge):
    async with mcp_session(bridge.socket_path) as session:
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        assert set(tools) == {
            "nicotine_status", "search", "get_search_results", "list_searches", "stop_search",
            "download_files", "download_folder", "browse_folder", "get_folder_contents", "list_downloads",
            "cancel_downloads", "retry_downloads", "clear_downloads",
        }
        assert tools["nicotine_status"].annotations.read_only_hint is True
        assert tools["stop_search"].annotations.destructive_hint is True
        assert tools["download_files"].annotations.destructive_hint is False

        status = payload(await session.call_tool("nicotine_status", {}))
        assert status["online"] is True
        assert status["username"] == bridge.username
        assert status["protocol"] == 2
        assert "warning" not in status
        assert status["rate_limit"]["capacity"] == 34


async def test_unreachable_bridge_is_a_tool_error(bridge, tmp_path):
    async with mcp_session(str(tmp_path / "nowhere.sock")) as session:
        text = error_text(await session.call_tool("nicotine_status", {}))
        assert "Cannot reach Nicotine+" in text
        assert "MCP Bridge" in text


async def test_search_and_grouped_results(bridge):
    async with mcp_session(bridge.socket_path) as session:
        first = payload(await session.call_tool("search", {"query": "test artist album", "wait_seconds": 0}))
        token = first["search_id"]
        assert first["files_received"] == 0 and first["folders"] == []

        bridge.send_search_response(token, "busy", [
            bridge.make_file(f"{ALBUM}\\01 - One.flac", size=31_457_280, duration=200, sample_rate=44100, bit_depth=16),
            bridge.make_file(f"{ALBUM}\\02 - Two.flac", size=31_457_280, duration=185, sample_rate=44100, bit_depth=16),
        ], free_slots=0, queue=4, speed=2_000_000)
        bridge.send_search_response(token, "free", [
            bridge.make_file("@@s\\Test Artist - Album\\01 One.mp3", size=8_388_608, duration=200, bitrate=320),
        ], free_slots=1, queue=0, speed=250_000)

        data = payload(await session.call_tool("get_search_results", {"search_id": token}))
        assert data["files_received"] == 3 and data["users_responded"] == 2
        assert data["folders_matching"] == 2 and data["folders_shown"] == 2
        assert [g["user"] for g in data["folders"]] == ["free", "busy"]
        busy = data["folders"][1]
        assert busy["folder"] == ALBUM
        assert busy["quality"] == "flac 16bit 44.1kHz"
        assert busy["total_mb"] == 60.0
        assert busy["files"][0] == {"id": 0, "name": "01 - One.flac", "mb": 30.0, "quality": "flac 16bit 44.1kHz", "length": "3:20"}

        lossless = payload(await session.call_tool("get_search_results", {"search_id": token, "lossless_only": True}))
        assert [g["user"] for g in lossless["folders"]] == ["busy"]
        assert lossless["files_matching_filters"] == 2

        one_user = payload(await session.call_tool("get_search_results", {"search_id": token, "username": "free", "path_contains": "mp3"}))
        assert one_user["files_matching_filters"] == 1

        listed = payload(await session.call_tool("list_searches", {}))
        assert [s["search_id"] for s in listed["searches"]] == [token]

        assert payload(await session.call_tool("stop_search", {"search_id": token})) == {"stopped": token}
        text = error_text(await session.call_tool("get_search_results", {"search_id": token}))
        assert "unknown search_id" in text


async def test_search_parameter_errors(bridge):
    async with mcp_session(bridge.socket_path) as session:
        text = error_text(await session.call_tool("search", {"query": "x", "mode": "user", "wait_seconds": 0}))
        assert "requires a list of usernames" in text


async def test_download_files_and_manage_downloads(bridge):
    async with mcp_session(bridge.socket_path) as session:
        token = payload(await session.call_tool("search", {"query": "test artist album", "wait_seconds": 0}))["search_id"]
        bridge.send_search_response(token, "peer1", [
            bridge.make_file(f"{ALBUM}\\01 - One.flac", size=31_457_280, duration=200),
            bridge.make_file(f"{ALBUM}\\02 - Two.flac", size=31_457_280, duration=185),
        ])

        queued = payload(await session.call_tool("download_files", {"search_id": token, "result_ids": [0, 1]}))
        ids = [q["download_id"] for q in queued["queued"]]
        assert len(ids) == 2 and queued["errors"] == []

        listed = payload(await session.call_tool("list_downloads", {}))
        assert listed["total"] == 2
        item = listed["downloads"][0]
        assert item["name"] == "01 - One.flac"
        assert item["mb"] == 30.0
        assert item["speed_kbps"] == 0
        assert item["status"] == "Queued"
        assert "size" not in item and "speed" not in item

        assert payload(await session.call_tool("cancel_downloads", {"download_ids": ids[:1]})) == {"cancelled": 1}
        cancelled = payload(await session.call_tool("list_downloads", {"statuses": ["Cancelled"]}))
        assert [d["download_id"] for d in cancelled["downloads"]] == ids[:1]

        assert payload(await session.call_tool("retry_downloads", {"download_ids": ids[:1]})) == {"retried": 1}
        assert payload(await session.call_tool("list_downloads", {"statuses": ["Cancelled"]}))["total"] == 0

        text = error_text(await session.call_tool("clear_downloads", {}))
        assert "refusing to clear everything" in text

        assert payload(await session.call_tool("clear_downloads", {"download_ids": ids})) == {"cleared": 2}
        assert payload(await session.call_tool("list_downloads", {}))["total"] == 0


async def test_download_folder_flow(bridge):
    async with mcp_session(bridge.socket_path) as session:
        result = payload(await session.call_tool("download_folder", {
            "username": "peer1", "folder_path": ALBUM, "include_subfolders": True,
        }))
        assert result["requested"] == {"user": "peer1", "folder": ALBUM}

        status = payload(await session.call_tool("nicotine_status", {}))
        assert len(status["pending_folder_requests"]) == 1

        bridge.send_folder_contents("peer1", ALBUM, {
            ALBUM: [(1, "01 - One.flac", 31_457_280, "flac", bridge.make_attrs(duration=200))],
            ALBUM + "\\Scans": [(1, "front.jpg", 500_000, "jpg", bridge.make_attrs())],
        })

        listed = payload(await session.call_tool("list_downloads", {}))
        assert sorted(d["name"] for d in listed["downloads"]) == ["01 - One.flac", "front.jpg"]

        status = payload(await session.call_tool("nicotine_status", {}))
        assert status["pending_folder_requests"] == []
        assert status["recent_folder_requests"][-1]["outcome"] == "queued 2 files"


async def test_browse_folder_flow(bridge):
    async with mcp_session(bridge.socket_path) as session:
        pending = payload(await session.call_tool("browse_folder", {
            "username": "peer1", "folder_path": ALBUM, "wait_seconds": 0,
        }))
        assert pending["status"] == "pending"

        bridge.send_folder_contents("peer1", ALBUM, {
            ALBUM: [
                (1, "01 - One.flac", 31_457_280, "flac", bridge.make_attrs(duration=200, sample_rate=44100, bit_depth=16)),
                (1, "02 - Two.flac", 31_457_280, "flac", bridge.make_attrs(duration=185, sample_rate=44100, bit_depth=16)),
            ],
        })

        ready = payload(await session.call_tool("get_folder_contents", {"username": "peer1", "folder_path": ALBUM}))
        assert ready["status"] == "ready" and ready["total_files"] == 2
        (folder,) = ready["folders"]
        assert folder["folder"] == ALBUM and folder["files"] == 2 and folder["total_mb"] == 60.0
        assert folder["entries"][0] == {"name": "01 - One.flac", "mb": 30.0, "quality": "flac 16bit 44.1kHz", "length": "3:20"}

        listed = payload(await session.call_tool("list_downloads", {}))
        assert listed["total"] == 0


async def test_rate_limit_error_is_explained(bridge):
    bridge.set_plugin_setting("search_rate_limit", 1)
    bridge.set_plugin_setting("search_rate_window", 60)

    def reset_bucket():
        bridge.plugin._rate_tokens = None

    bridge.on_main(reset_bucket)
    try:
        async with mcp_session(bridge.socket_path) as session:
            await session.call_tool("search", {"query": "first", "wait_seconds": 0})
            text = error_text(await session.call_tool("search", {"query": "second", "wait_seconds": 0}))
            assert "rate limit" in text and "retry in" in text
    finally:
        bridge.set_plugin_setting("search_rate_limit", 34)
        bridge.set_plugin_setting("search_rate_window", 220)
        bridge.on_main(reset_bucket)


async def test_library_server_over_stdio(bridge, tmp_path):
    env = {"FLACLI_DATA": str(tmp_path / "data"), "FLACLI_MUSIC_DIR": str(tmp_path / "Music")}

    async with mcp_session(bridge.socket_path, args=LIBRARY_SERVER, env=env) as session:
        tools = {tool.name for tool in (await session.list_tools()).tools}
        assert {"import_playlist_file", "resolve_playlist", "scan_library", "diff_library", "match_playlist",
                "review_candidates", "approve", "queue_approved", "sync_downloads", "write_m3u", "playlist_status",
                "list_playlists", "library_status"} <= tools

        status = payload(await session.call_tool("library_status", {}))
        assert status["bridge"]["reachable"] is True and status["bridge"]["protocol"] == 2
        assert status["schema_version"] == 1 and status["data_dir"] == str(tmp_path / "data")

        imported = payload(await session.call_tool("import_playlist_file", {
            "path": str(Path(__file__).parent / "fixtures" / "exportify.csv"),
        }))
        assert imported["imported"][0]["tracks"] == 2
        listed = payload(await session.call_tool("list_playlists", {}))
        assert listed["playlists"][0]["counts"] == {"pending": 2}

        text = error_text(await session.call_tool("diff_library", {"playlist_id": 1}))
        assert "library index is empty" in text
        text = error_text(await session.call_tool("import_playlist_file", {"path": str(tmp_path / "missing.csv")}))
        assert "does not exist" in text
