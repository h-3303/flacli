"""Synthetic end-to-end run of the library server against the Nicotine+ harness with fake peers:

fixture Spotify export -> resolve (canned MusicBrainz) -> diff against generated tagged FLACs ->
match (album mode + single files) -> approve -> queue -> transfers finish -> M3U in original order.
"""

import asyncio
import json
import os
import threading
import time

from pathlib import Path

import pytest

from libtools import FakeMusicBrainz, make_flac, recording

FIXTURES = Path(__file__).parent / "fixtures"
pytestmark = pytest.mark.anyio

B_FOLDER = "@@shares\\Band B\\B Album (2005) [FLAC]"
C_FOLDER = "@@music\\Solo C\\Alone"


class FakePeers(threading.Thread):
    """Answers bridge searches the way Soulseek peers would, based on the query words."""

    def __init__(self, harness):
        super().__init__(daemon=True)
        self.harness = harness
        self.seen: set[int] = set()
        self.queries: list[str] = []
        self.folder_requests: list[str] = []
        self.stop = threading.Event()

    def run(self):
        while not self.stop.is_set():
            try:
                searches = self.harness.rpc("list_searches")
            except Exception:
                break

            for search in searches:
                token = search["search_id"]

                if token in self.seen:
                    continue

                self.seen.add(token)
                self.queries.append(search["query"])
                self.answer(token, search["query"])

            try:
                pending = self.harness.rpc("status")["pending_folder_requests"]
            except Exception:
                break

            for request in pending:
                if request["user"] == "peerB" and request["folder"] == B_FOLDER:
                    self.folder_requests.append(request["folder"])
                    self.harness.send_folder_contents("peerB", B_FOLDER, {B_FOLDER: [
                        (1, f"0{i} - Track {i}.flac", 25_000_000, "flac",
                         self.harness.make_attrs(duration=200 + i, sample_rate=44100, bit_depth=16)) for i in (1, 2, 3)
                    ] + [(1, "cover.jpg", 200_000, "jpg", self.harness.make_attrs())]})
                # user "partial" never answers folder requests

            time.sleep(0.05)

    def answer(self, token, query):
        h = self.harness
        words = set(query.split())

        if {"band", "b", "album"} <= words:
            h.send_search_response(token, "peerB", [
                h.make_file(f"{B_FOLDER}\\0{i} - Track {i}.flac", size=25_000_000, duration=200 + i, sample_rate=44100, bit_depth=16)
                for i in (1, 2, 3)
            ], free_slots=1, speed=900_000, queue=0)
            h.send_search_response(token, "partial", [
                h.make_file("@@x\\Band B\\B Album\\01 - Track 1.mp3", size=8_000_000, duration=201, bitrate=320),
            ], free_slots=1, speed=900_000, queue=0)

        elif "lonely" in words:
            h.send_search_response(token, "peerC-mp3", [
                h.make_file(f"{C_FOLDER}\\03 - Lonely Song.mp3", size=7_000_000, duration=185, bitrate=320),
            ], free_slots=1, speed=2_000_000, queue=0)
            h.send_search_response(token, "peerC-flac", [
                h.make_file(f"{C_FOLDER}\\03 - Lonely Song.flac", size=28_000_000, duration=186, sample_rate=44100, bit_depth=16),
                h.make_file(f"{C_FOLDER}\\folder.jpg", size=100_000),
            ], free_slots=0, speed=500_000, queue=2)

        # "ghost" gets no replies at all


def finish_transfers(harness, music_root: Path):
    """Pretend every queued transfer completed: write the file where Nicotine+ would put it."""
    def apply():
        finished = []

        for transfer in harness.core.downloads.transfers.values():
            name = transfer.virtual_path.rpartition("\\")[2]
            folder = Path(transfer.folder_path or harness.core.downloads.get_default_download_folder())
            target = folder / name
            transfer.status = "Finished"
            finished.append(target)

        return finished

    targets = harness.on_main(apply)

    for target in targets:
        make_flac(target, seconds=200, title=target.stem.split(" - ", 1)[-1], artist="x")

    return targets


@pytest.fixture
def library_server(bridge, tmp_path, monkeypatch):
    import flacli.server as server
    from flacli.bridge import BridgeClient

    data = tmp_path / "data"
    music = tmp_path / "Music"
    monkeypatch.setenv("FLACLI_DATA", str(data))
    monkeypatch.setenv("FLACLI_MUSIC_DIR", str(music))
    monkeypatch.setenv("FLACLI_CONTACT", "tests")

    fake_mb = FakeMusicBrainz([
        recording("r-a1", "Song One", "Artist A", 200000, "rel-a", "Album A", 10),
        recording("r-a2", "Song Two", "Artist A", 210000, "rel-a", "Album A", 10),
        recording("r-b1", "Track 1", "Band B", 201000, "rel-b", "B Album", 3),
        recording("r-b2", "Track 2", "Band B", 202000, "rel-b", "B Album", 3),
        recording("r-b3", "Track 3", "Band B", 203000, "rel-b", "B Album", 3),
        recording("r-c1", "Lonely Song", "Solo C", 185000, "rel-c", "Alone", 12),
    ])
    server.State.db = None
    server.State.bridge = BridgeClient(bridge.socket_path)
    server.State.jobs = {}
    server.State.mb_fetch = fake_mb
    server.State.mb_sleep = lambda seconds: None

    # Local library: Song One (MusicBrainz-tagged) and Song Two (plain tags) already owned
    make_flac(music / "Artist A" / "Album A" / "01 - Song One.flac", seconds=200, title="Song One", artist="Artist A",
              album="Album A", musicbrainz_trackid="r-a1")
    make_flac(music / "Artist A" / "Album A" / "02 - Song Two.flac", seconds=210, title="Song Two", artist="Artist A",
              album="Album A")

    peers = FakePeers(bridge)
    peers.start()
    yield server, bridge, music, fake_mb, peers
    peers.stop.set()
    peers.join(timeout=2)

    for task in server.State.jobs.values():
        task.cancel()

    if server.State.db is not None:
        server.State.db.close()
        server.State.db = None


async def test_full_pipeline(library_server, tmp_path):
    server, harness, music, fake_mb, peers = library_server

    # 1. import
    imported = await server.import_playlist_file(str(FIXTURES / "Playlist1.json"), playlist_name="Road Trip")
    (playlist,) = imported["imported"]
    playlist_id = playlist["playlist_id"]
    assert playlist["tracks"] == 7 and playlist["needs_durations"] is True
    assert Path(playlist["jspf_path"]).is_file()
    assert json.loads(Path(playlist["jspf_path"]).read_text())["playlist"]["title"] == "Road Trip"

    # 2. resolve
    resolved = await server.resolve_playlist(playlist_id)
    assert resolved["resolved"] == 6 and resolved["unresolved"] == 1
    assert resolved["unresolved_tracks"][0]["title"] == "Ghost Track"
    status = await server.playlist_status(playlist_id)
    assert status["unresolved"] == 1
    track_rows = server.db().tracks(playlist_id)
    assert track_rows[2]["mb_release_id"] == "rel-b" and track_rows[2]["mb_release_track_count"] == 3
    assert track_rows[0]["duration_ms"] == 200000 and track_rows[0]["duration_source"] == "musicbrainz"

    # 3. library
    with pytest.raises(Exception, match="library index is empty"):
        await server.diff_library(playlist_id)

    scanned = await server.scan_library()
    assert scanned["indexed"] == 2
    diff = await server.diff_library(playlist_id)
    assert diff["in_library"] == 2 and diff["by_method"] == {"mbid": 1, "text+duration": 1}
    assert (await server.playlist_status(playlist_id))["counts"] == {"pending": 5, "in_library": 2}

    # 4. match (fast harvest for the test)
    started = await server.match_playlist(playlist_id, harvest_seconds=0.4)
    assert started["tracks_to_match"] == 5

    with pytest.raises(Exception, match="already running"):
        await server.match_playlist(playlist_id)

    await asyncio.wait_for(server.State.jobs[playlist_id], timeout=60)
    status = await server.playlist_status(playlist_id)
    assert status["job"]["status"] == "finished", status
    assert status["counts"] == {"in_library": 2, "candidates": 4, "not_found": 1}
    assert status["job"]["album_groups"] == 1
    assert "band b album" in peers.queries, peers.queries
    assert peers.folder_requests == [B_FOLDER], "album mode inspects the best folder listing before proposing it"
    assert not [q for q in peers.queries if "track 1" in q], "album mode must not search the album's tracks singly"
    assert harness.rpc("list_searches") == [], "every search is stopped after harvesting"
    assert harness.transfers() == [], "matching never downloads"

    # 5. review
    review = await server.review_candidates(playlist_id, status="candidates")
    by_title = {t["title"]: t for t in review["tracks"]}
    lonely = by_title["Lonely Song - 2011 Remaster"]
    assert lonely["candidates"][0]["user"] == "peerC-flac"       # FLAC preferred over the free-slot mp3
    assert lonely["candidates"][0]["quality"] == "flac 16bit 44.1kHz"
    assert lonely["candidates"][1]["user"] == "peerC-mp3"
    assert lonely["confidence"] == 1.0
    album_track = by_title["Track 2"]
    assert album_track["candidates"][0]["kind"] == "folder"
    assert album_track["candidates"][0]["track_count"] == 3 and album_track["candidates"][0]["expected_track_count"] == 3
    assert album_track["candidates"][0]["file"] == "02 - Track 2.flac"
    not_found = await server.review_candidates(playlist_id, status="not_found")
    assert [t["title"] for t in not_found["tracks"]] == ["Ghost Track"]

    # 6. approve + 7. queue
    approved = await server.approve(playlist_id=playlist_id, min_confidence=0.85)
    assert approved["approved"] == 4
    plan = await server.queue_approved(playlist_id)
    assert plan["tracks"] == 4 and plan["single_files"] == 1 and plan["folders"] == 1
    assert plan["users"] == ["peerB", "peerC-flac"]
    assert plan["total_mb"] == pytest.approx((75_000_000 + 28_000_000) / 1048576, abs=0.1)
    assert "nothing queued" in plan["note"] and harness.transfers() == []

    queued = await server.queue_approved(playlist_id, confirm=True)
    assert queued["queued"] == 4 and queued["errors"] == []

    for _ in range(100):  # the fake peer answers the folder request in the background
        if len(harness.transfers()) >= 5:
            break
        await asyncio.sleep(0.05)

    paths = sorted(t.virtual_path for t in harness.transfers())
    expected = [f"{B_FOLDER}\\0{i} - Track {i}.flac" for i in (1, 2, 3)]
    expected += [f"{B_FOLDER}\\cover.jpg", f"{C_FOLDER}\\03 - Lonely Song.flac"]   # whole folder = cover art too
    assert paths == sorted(expected)

    # 8. sync: still queued, then finished
    synced = await server.sync_downloads(playlist_id)
    assert synced["queued"] == 4 and synced["done"] == 0
    finish_transfers(harness, music)
    synced = await server.sync_downloads(playlist_id)
    assert synced["done"] == 4
    assert synced["counts"] == {"in_library": 2, "done": 4, "not_found": 1}

    # 9. M3U in original order, missing track reported
    written = await server.write_m3u(playlist_id, path=str(tmp_path / "out.m3u8"), relative_to=str(tmp_path))
    text = (tmp_path / "out.m3u8").read_text().splitlines()
    assert text[:2] == ["#EXTM3U", "#PLAYLIST:Road Trip"]
    labels = [line.split(",", 1)[1] for line in text if line.startswith("#EXTINF")]
    assert labels == ["Artist A - Song One", "Artist A - Song Two (feat. Guest)", "Band B - Track 1", "Band B - Track 2",
                      "Band B - Track 3", "Solo C - Lonely Song - 2011 Remaster"]
    files = [line for line in text if line and not line.startswith("#")]
    assert files[0] == "Music/Artist A/Album A/01 - Song One.flac"
    assert all(os.path.isfile(tmp_path / f) for f in files)
    assert written["written"] == 6 and written["missing"] == [{"position": 7, "title": "Ghost Track", "artist": "Nobody", "status": "not_found"}]

    # resumability: a new server process sees the same state
    server.State.db.close()
    server.State.db = None
    assert (await server.list_playlists())["playlists"][0]["counts"] == {"in_library": 2, "done": 4, "not_found": 1}


async def test_failed_transfer_retries_next_candidate(library_server, tmp_path):
    server, harness, music, fake_mb, peers = library_server
    (playlist,) = (await server.import_playlist_file(str(FIXTURES / "exportify.csv")))["imported"]
    playlist_id = playlist["playlist_id"]
    await server.resolve_playlist(playlist_id)
    await server.match_playlist(playlist_id, album_mode="off", harvest_seconds=0.3)
    await asyncio.wait_for(server.State.jobs[playlist_id], timeout=60)

    review = await server.review_candidates(playlist_id)
    lonely = next(t for t in review["tracks"] if t["title"].startswith("Lonely"))
    assert [c["user"] for c in lonely["candidates"]] == ["peerC-flac", "peerC-mp3"]

    await server.approve(track_ids=[lonely["track_id"]])
    await server.queue_approved(playlist_id, confirm=True)
    (transfer,) = harness.transfers()
    assert transfer.username == "peerC-flac"

    def fail():
        transfer.status = "User logged off"

    harness.on_main(fail)
    synced = await server.sync_downloads(playlist_id)
    assert synced["retried"] == 1 and synced["failed"] == 0
    users = sorted(t.username for t in harness.transfers())
    assert users == ["peerC-flac", "peerC-mp3"], "the next candidate was queued"
    row = server.db().track(lonely["track_id"])
    assert row["status"] == "queued" and server.db().candidates(row)[0]["user"] == "peerC-mp3"
    assert server.db().user_failures(server.db().active_job(playlist_id)["id"] if server.db().active_job(playlist_id) else
                                     server.db().conn.execute("SELECT id FROM jobs ORDER BY id DESC").fetchone()["id"]) == {"peerC-flac": 1}


async def test_rate_limit_wait_is_visible(library_server):
    server, harness, music, fake_mb, peers = library_server
    harness.set_plugin_setting("search_rate_limit", 1)
    harness.set_plugin_setting("search_rate_window", 4)

    def reset_bucket():
        harness.plugin._rate_tokens = None

    harness.on_main(reset_bucket)

    try:
        (playlist,) = (await server.import_playlist_file(str(FIXTURES / "exportify.csv")))["imported"]
        playlist_id = playlist["playlist_id"]
        await server.match_playlist(playlist_id, album_mode="off", harvest_seconds=0.2)
        job = server.State.jobs[playlist_id]
        waited = None

        for _ in range(100):
            await asyncio.sleep(0.1)
            status = await server.playlist_status(playlist_id)

            if status["job"].get("waiting_rate_limit_until"):
                waited = status["job"]
                break

        assert waited is not None and "rate limit" in waited["note"]
        await asyncio.wait_for(job, timeout=60)
        assert (await server.playlist_status(playlist_id))["job"]["status"] == "finished"
    finally:
        harness.set_plugin_setting("search_rate_limit", 34)
        harness.set_plugin_setting("search_rate_window", 220)
        harness.on_main(reset_bucket)
