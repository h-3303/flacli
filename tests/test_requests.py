"""Direct requests: item parsing, album expansion through MusicBrainz, and the one-call request_music flow
(persistent playlist -> resolve -> library diff -> match -> auto-approve -> queue) with the auto-tidy of finished
downloads that sync_downloads performs, and the tidy_new tool for files that arrived by other routes.
"""

import asyncio
import os
import time

from pathlib import Path

import pytest

from flacli import requester
from flacli.db import Database
from flacli.musicbrainz import MusicBrainzClient

from libtools import FakeMusicBrainz, make_flac, release
from test_e2e_pipeline import B_FOLDER, C_FOLDER, library_server  # noqa: F401 - fixture

pytestmark = pytest.mark.anyio


# Parsing #

def test_parse_items():
    assert requester.parse_item("Lorde - Royals") == {"kind": "track", "artist": "Lorde", "title": "Royals", "album": ""}
    assert requester.parse_item("Lorde – Pure Heroine (album)") == {"kind": "album", "artist": "Lorde", "album": "Pure Heroine"}
    assert requester.parse_item("album: Lorde - Pure Heroine") == {"kind": "album", "artist": "Lorde", "album": "Pure Heroine"}
    assert requester.parse_item("Royals") == {"kind": "track", "artist": "", "title": "Royals", "album": ""}
    assert requester.parse_item({"artist": "Lorde", "title": "Royals", "album": "Pure Heroine"})["album"] == "Pure Heroine"
    assert requester.parse_item({"artist": "Lorde", "album": "Pure Heroine"})["kind"] == "album"
    assert requester.parse_item({"kind": "album", "artist": "Lorde", "album": "Melodrama"})["album"] == "Melodrama"

    for bad in ("", "   ", {"artist": "Lorde"}, {"kind": "video", "title": "x"}):
        with pytest.raises(ValueError):
            requester.parse_item(bad)

    with pytest.raises(ValueError, match="no items"):
        requester.parse_items([])


# MusicBrainz release expansion #

def test_find_release_prefers_plain_official_album_and_lists_tracks(tmp_path):
    db = Database(tmp_path / "state.db")
    fake = FakeMusicBrainz([], releases=[
        release("rel-deluxe", "Pure Heroine", "Lorde", [(f"r{i}", f"T{i}", 200000) for i in range(15)], date="2013-12-01"),
        release("rel-plain", "Pure Heroine", "Lorde", [(f"r{i}", f"T{i}", 200000) for i in range(10)], date="2013-09-27"),
        release("rel-boot", "Pure Heroine", "Lorde", [("x", "T", 1)], status="Bootleg"),
        release("rel-other", "Pure Heroine", "Someone Else", [("y", "T", 1)]),
    ])
    client = MusicBrainzClient(db, "flacli/test ( tests )", fetch=fake, sleep=lambda s: None)

    chosen = client.find_release("Lorde", "Pure Heroine")
    assert chosen["release_id"] == "rel-plain" and chosen["track_count"] == 10 and chosen["artist"] == "Lorde"
    assert client.find_release("Lorde", "Nonexistent") is None

    tracks = client.release_tracks("rel-plain")
    assert [t["position"] for t in tracks] == list(range(1, 11))
    assert tracks[0] == {"position": 1, "title": "T0", "artist": "Lorde", "recording_id": "r0", "duration_ms": 200000}

    parsed = requester.parse_items([{"artist": "Lorde", "album": "Pure Heroine"}, "Lorde - Royals", "Nobody - Nothing (album)"])
    tracks, understood = requester.expand(client, parsed)
    assert len(tracks) == 11 and tracks[0].mb_release_id == "rel-plain" and tracks[0].mb_release_track_count == 10
    assert tracks[10].source_uri == "request:track" and tracks[10].title == "Royals"
    assert understood[0]["release"]["id"] == "rel-plain" and understood[0]["tracks"] == 10
    assert understood[2]["tracks"] == 0 and "no matching release" in understood[2]["error"]
    db.close()


# The whole flow against the Nicotine+ harness #

def finish_into(harness, incoming: Path):
    """Complete every transfer as if Nicotine+ had put the file, properly tagged, under incoming/<remote folder>."""
    tags = {
        "Track 1": dict(seconds=201, artist="Band B", album="B Album", tracknumber="1", date="2005"),
        "Track 2": dict(seconds=202, artist="Band B", album="B Album", tracknumber="2", date="2005"),
        "Track 3": dict(seconds=203, artist="Band B", album="B Album", tracknumber="3", date="2005"),
        "Lonely Song": dict(seconds=185, artist="Solo C", album="Alone", tracknumber="3"),
    }

    def apply():
        out = []

        for transfer in harness.core.downloads.transfers.values():
            name = transfer.virtual_path.rpartition("\\")[2]
            remote_folder = transfer.virtual_path.rpartition("\\")[0].rpartition("\\")[2]
            transfer.folder_path = str(incoming / remote_folder)
            transfer.status = "Finished"
            out.append(incoming / remote_folder / name)

        return out

    targets = harness.on_main(apply)

    for target in targets:
        if target.suffix == ".flac":
            title = target.stem.split(" - ", 1)[-1]
            make_flac(target, title=title, **tags[title])
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"jpeg")

    return targets


async def test_request_music_downloads_directly_and_files_the_results(library_server, tmp_path):
    server, harness, music, fake_mb, peers = library_server
    fake_mb.releases.append(release("rel-b", "B Album", "Band B", [("r-b1", "Track 1", 201000), ("r-b2", "Track 2", 202000),
                                                                  ("r-b3", "Track 3", 203000)], date="2005-01-01"))

    result = await server.request_music(
        ["Artist A - Song One", {"artist": "Band B", "album": "B Album"}, "Solo C - Lonely Song", "Nobody - Ghost Track"],
        harvest_seconds=0.4,
    )
    playlist_id = result["playlist_id"]
    assert result["created"] is True and result["playlist"] == "Requests"
    kinds = [u["kind"] for u in result["understood"]]
    assert kinds == ["track", "album", "track", "track"]
    assert result["understood"][1]["release"] == {"id": "rel-b", "title": "B Album", "artist": "Band B", "date": "2005-01-01", "track_count": 3}
    assert result["added"] == 6 and result["already_in_library"] == 1 and result["to_fetch"] == 5
    assert result["unresolved"] == [{"artist": "Nobody", "title": "Ghost Track"}]
    assert result["job_id"] and "background" in result["note"]

    with pytest.raises(Exception, match="already running"):
        await server.request_music(["Solo C - Lonely Song"])

    await asyncio.wait_for(server.State.jobs[playlist_id], timeout=60)
    status = await server.playlist_status(playlist_id)
    job = status["job"]
    assert job["kind"] == "request" and job["status"] == "finished" and job["phase"] == "finished", job
    assert job["queued"] == 4 and job["folders"] == 1 and job["not_found"] == 1 and job["for_review"] == 0
    assert job["users"] == ["peerB", "peerC-flac"] and job["queue_errors"] == []
    assert status["counts"] == {"in_library": 1, "queued": 4, "not_found": 1}
    assert "band b album" in peers.queries and not [q for q in peers.queries if "track 1" in q], "albums go folder-wise"

    for _ in range(100):
        if len(harness.transfers()) >= 5:
            break
        await asyncio.sleep(0.05)

    assert sorted(t.virtual_path for t in harness.transfers()) == sorted(
        [f"{B_FOLDER}\\0{i} - Track {i}.flac" for i in (1, 2, 3)] + [f"{B_FOLDER}\\cover.jpg", f"{C_FOLDER}\\03 - Lonely Song.flac"])

    # downloads finish inside the library -> sync_downloads files them, only them
    make_flac(music / "loose.flac", title="Untouched", artist="Someone", album="Elsewhere", tracknumber="1")
    incoming = music / "incoming"
    finish_into(harness, incoming)
    synced = await server.sync_downloads(playlist_id)
    assert synced["done"] == 4
    tidied = synced["tidied"]
    assert tidied["moved"] == 4 and tidied["retagged"] == 4 and tidied["held"] == [] and tidied["errors"] == []
    assert tidied["extras_filed"] == 1 and tidied["pruned_folders"] == 3 and tidied["relocated_tracks"] == 4
    assert tidied["moved_to"] == ["Band B/B Album", "Solo C/Alone"]
    assert sorted(p.name for p in (music / "Band B" / "B Album").iterdir()) == ["01 - Track 1.flac", "02 - Track 2.flac", "03 - Track 3.flac", "cover.jpg"]
    assert (music / "Solo C" / "Alone" / "03 - Lonely Song.flac").is_file()
    assert not incoming.exists(), "emptied download folders are pruned"
    assert (music / "loose.flac").is_file(), "files that did not just arrive are not touched"
    rows = {r["title"]: r for r in server.db().tracks(playlist_id)}
    assert rows["Track 2"]["local_path"] == str(music / "Band B" / "B Album" / "02 - Track 2.flac")
    assert server.db().library_file(rows["Track 2"]["local_path"]) is not None, "index follows the move"

    written = await server.write_m3u(playlist_id, path=str(tmp_path / "requests.m3u8"))
    assert written["written"] == 5 and [m["title"] for m in written["missing"]] == ["Ghost Track"]

    # later requests append to the same playlist
    again = await server.request_music(["Solo C - Lonely Song"], download=False)
    assert again["playlist_id"] == playlist_id and again["created"] is False
    assert again["added"] == 1 and again["already_in_library"] == 1 and again["to_fetch"] == 0
    assert server.db().get_playlist(playlist_id)["track_count"] == 7
    assert [r["position"] for r in server.db().tracks(playlist_id)] == list(range(7))
    assert (await server.list_playlists())["playlists"][0]["source"] == "request"


async def test_auto_tidy_off_leaves_files_for_tidy_new(library_server, tmp_path, monkeypatch):
    server, harness, music, fake_mb, peers = library_server
    monkeypatch.setenv("FLACLI_AUTO_TIDY", "false")

    result = await server.request_music(["Solo C - Lonely Song"], harvest_seconds=0.4)
    playlist_id = result["playlist_id"]
    await asyncio.wait_for(server.State.jobs[playlist_id], timeout=60)
    assert (await server.playlist_status(playlist_id))["job"]["queued"] == 1

    incoming = music / "incoming"
    (target,) = finish_into(harness, incoming)
    synced = await server.sync_downloads(playlist_id)
    assert synced["done"] == 1 and "tidied" not in synced and target.is_file()
    assert (await server.library_status())["auto_tidy"] is False

    filed = await server.tidy_new(paths=[str(target)])
    assert filed["moved"] == 1 and filed["settling"] == [] and filed["auto_tidy"] is False
    assert (music / "Solo C" / "Alone" / "03 - Lonely Song.flac").is_file() and not incoming.exists()
    assert server.db().track(server.db().tracks(playlist_id)[0]["id"])["local_path"].endswith("Alone/03 - Lonely Song.flac")


async def test_tidy_new_finds_new_files_by_scanning(tmp_path, monkeypatch):
    import flacli.server as server

    music = tmp_path / "Music"
    monkeypatch.setenv("FLACLI_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("FLACLI_MUSIC_DIR", str(music))
    server.State.db = None
    old = time.time() - 3600
    make_flac(music / "old.flac", title="Old", artist="Art", album="Alb", tracknumber="1")
    os.utime(music / "old.flac", (old, old))
    await server.scan_library()

    assert (await server.tidy_new())["note"] == "nothing new to tidy"
    assert (music / "old.flac").is_file(), "known files are not new, whatever their tags"

    make_flac(music / "new.flac", title="New", artist="Art", album="Alb", tracknumber="2")
    os.utime(music / "new.flac", (old, old))
    make_flac(music / "landing.flac", title="Landing", artist="Art", album="Alb", tracknumber="3")

    filed = await server.tidy_new()
    assert filed["moved"] == 1 and filed["settling"] == [str(music / "landing.flac")]
    assert (music / "Art" / "Alb" / "02 - New.flac").is_file() and (music / "old.flac").is_file()
    assert server.db().library_file(str(music / "Art" / "Alb" / "02 - New.flac")) is not None
    assert server.db().library_file(str(music / "new.flac")) is None
    server.State.db.close()
    server.State.db = None
