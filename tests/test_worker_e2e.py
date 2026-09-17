"""The CLI's background path against the Nicotine+ harness: a job created by the simple layer, run by a detached
`flacli.worker` process, followed through `status`, queued through `queue`. Plus the simple MCP server's tool list.
"""

import asyncio
import json
import os
import sys
import time

import pytest

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from flacli import jobs, simple
from flacli.models import Track

from test_e2e_pipeline import library_server  # noqa: F401 - fixture

pytestmark = pytest.mark.anyio


async def settle(playlist_id, timeout=60):
    deadline = time.time() + timeout

    while True:
        status = await simple.status(playlist_id)
        job = status["job"]

        if job and job["status"] not in ("running", "waiting"):
            return status

        assert time.time() < deadline, f"job did not finish: {job}"
        await asyncio.sleep(0.5)


async def test_get_runs_in_a_worker_and_queues(library_server):
    server, bridge, music, fake_mb, peers = library_server
    result = await simple.get(["Solo C - Lonely Song", "Artist A - Song One"], harvest_seconds=0.4)
    assert result["added"] == 2 and result["already_in_library"] == 1 and result["to_fetch"] == 1
    assert result["job_id"] and jobs.alive(result["pid"])
    assert "flacli status" in result["note"]

    status = await settle(result["playlist_id"])
    assert status["job"]["status"] == "finished" and status["job"]["phase"] == "finished"
    assert status["job"]["queued"] == 1 and status["job"]["for_review"] == 0
    assert status["counts"]["in_library"] == 1 and (status["counts"].get("queued") or status["counts"].get("downloading"))
    assert "lonely" in " ".join(peers.queries).lower()
    assert "downloads in progress" in status["next"]

    with pytest.raises(ValueError, match="no running job"):
        await simple.cancel(result["playlist_id"])


async def test_sync_matches_in_a_worker_then_queue_needs_yes(library_server):
    server, bridge, music, fake_mb, peers = library_server
    playlist_id = simple.db().add_playlist("Mix", "csv", [Track(title="Lonely Song", artist="Solo C"),
                                                          Track(title="Song Two", artist="Artist A")])
    result = await simple.sync(str(playlist_id), harvest_seconds=0.4)
    assert result["resolved"] == 2 and result["already_in_library"] == 1 and result["to_fetch"] == 1
    assert "nothing queued yet" in result["note"]

    with pytest.raises(ValueError, match="already running"):
        await simple.sync(str(playlist_id))

    status = await settle(playlist_id)
    assert status["job"]["status"] == "finished" and status["counts"] == {"in_library": 1, "candidates": 1}
    assert "flacli review" in status["next"]

    review = await simple.review(playlist_id)
    (track,) = review["tracks"]
    assert track["title"] == "Lonely Song" and track["candidates"][0]["quality"].startswith("flac")

    totals = await simple.queue(playlist_id, min_confidence=0.5)
    assert totals["tracks"] == 1 and totals["approved_now"] == 1 and "nothing queued" in totals["note"]
    assert (await simple.status(playlist_id))["counts"] == {"in_library": 1, "approved": 1}

    queued = await simple.queue(playlist_id, yes=True)
    assert queued["queued"] == 1 and queued["errors"] == []
    status = await simple.status(playlist_id)
    assert status["counts"].get("queued") or status["counts"].get("downloading")

    m3u = await simple.m3u(playlist_id, path=str(music / "mix.m3u8"))
    assert m3u["missing_count"] == 1 and (music / "mix.m3u8").exists()


async def test_simple_mcp_server_tool_list(tmp_path):
    env = {**os.environ, "FLACLI_DATA": str(tmp_path / "data"), "FLACLI_MUSIC_DIR": str(tmp_path / "Music"),
           "NICOTINE_MCP_SOCKET": str(tmp_path / "nowhere.sock")}
    params = StdioServerParameters(command=sys.executable, args=["-m", "flacli.mcp_simple"], env=env)

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = {tool.name for tool in (await session.list_tools()).tools}
            assert tools == {"doctor", "get_music", "sync_playlist", "status", "review_candidates", "approve_tracks",
                             "skip_tracks", "queue_downloads", "write_m3u", "tidy_library", "cancel_job"}

            result = await session.call_tool("doctor", {})
            report = result.structured_content or json.loads(result.content[0].text)
            assert report["ok"] is False and report["nicotine"]["reachable"] is False
