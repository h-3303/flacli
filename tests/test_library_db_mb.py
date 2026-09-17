"""Database migrations/state, MusicBrainz resolver (canned HTTP), library scan and diff."""

from pathlib import Path

import pytest

from flacli.db import Database
from flacli.library import diff_playlist, find_local, scan_library
from flacli.models import Track
from flacli.musicbrainz import MusicBrainzClient

from libtools import FakeMusicBrainz, make_flac, recording


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "state.db")
    yield database
    database.close()


def test_migrations_are_versioned(tmp_path):
    database = Database(tmp_path / "state.db")
    assert database.schema_version == 3
    database.close()
    again = Database(tmp_path / "state.db")   # reopening must not re-run migrations
    assert again.schema_version == 3
    assert again.list_playlists() == []
    again.close()


def test_missing_columns_are_put_back_on_open(tmp_path):
    """The version said 3 but a live database lacked migration 3's columns; opening it must repair that."""
    database = Database(tmp_path / "state.db")
    database.conn.execute("ALTER TABLE matches DROP COLUMN stalled_since")
    database.conn.execute("ALTER TABLE library_files DROP COLUMN albumartist")
    database.close()
    again = Database(tmp_path / "state.db")
    assert again.schema_version == 3
    assert "stalled_since" in {r["name"] for r in again.conn.execute("PRAGMA table_info(matches)")}
    assert "albumartist" in {r["name"] for r in again.conn.execute("PRAGMA table_info(library_files)")}
    again.close()


def test_failed_migration_leaves_version_and_schema_untouched(tmp_path, monkeypatch):
    from flacli import db as dbmod
    database = Database(tmp_path / "state.db")
    database.close()
    monkeypatch.setattr(dbmod, "MIGRATIONS", dbmod.MIGRATIONS + ["ALTER TABLE matches ADD COLUMN bogus TEXT; SELECT * FROM nowhere;"])

    with pytest.raises(Exception, match="nowhere"):
        Database(tmp_path / "state.db")

    monkeypatch.undo()
    again = Database(tmp_path / "state.db")
    assert again.schema_version == 3
    assert "bogus" not in {r["name"] for r in again.conn.execute("PRAGMA table_info(matches)")}
    again.close()


def test_playlist_and_match_state(db):
    tracks = [Track(title="A", artist="X", position=0), Track(title="B", artist="X", position=1, local_path="/music/b.flac")]
    playlist_id = db.add_playlist("P", "csv", tracks, source_ref="p.csv")
    assert db.get_playlist(playlist_id)["track_count"] == 2
    rows = db.tracks(playlist_id)
    assert [r["status"] for r in rows] == ["pending", "in_library"]
    assert rows[1]["local_path"] == "/music/b.flac"

    db.set_match(rows[0]["id"], "candidates", candidates=[{"user": "u", "confidence": 0.9}], confidence=0.9, bump_attempts=True)
    row = db.track(rows[0]["id"])
    assert row["status"] == "candidates" and row["attempts"] == 1 and db.candidates(row)[0]["user"] == "u"
    assert db.status_counts(playlist_id)["candidates"] == 1

    with pytest.raises(ValueError):
        db.set_match(rows[0]["id"], "bogus")

    job = db.create_job(playlist_id, "match", {"total": 1})
    db.update_job(job, status="waiting", progress={"total": 1, "done": 0})
    assert db.active_job(playlist_id)["id"] == job
    db.interrupt_running_jobs()
    assert db.active_job(playlist_id) is None
    db.penalise_user(job, "bad"); db.penalise_user(job, "bad")
    assert db.user_failures(job) == {"bad": 2}

    db.delete_playlist(playlist_id)
    assert db.tracks(playlist_id) == [] and db.job(job) is None


def test_musicbrainz_resolver_rate_limit_and_cache(db):
    fake = FakeMusicBrainz([
        recording("r-one", "Song One", "Artist A", 200000, "rel-a", "Album A", 10),
        recording("r-one-live", "Song One (live)", "Artist A", 260000, "rel-live", "Live", 12, score=80),
        recording("r-two", "Song Two", "Artist A", 210000, "rel-a", "Album A", 10),
    ], isrcs={"USAAA0100001": [recording("r-one-isrc", "Song One", "Artist A", 200000, "rel-a", "Album A", 10)]})
    naps = []
    client = MusicBrainzClient(db, "flacli/test ( tests )", fetch=fake, sleep=naps.append)

    hit = client.resolve("Song One", "Artist A", "Album A", duration_ms=201000)
    assert hit["recording_id"] == "r-one" and hit["release_id"] == "rel-a" and hit["release_track_count"] == 10
    assert hit["method"] == "search" and hit["duration_ms"] == 200000

    again = client.resolve("Song One", "Artist A", "Album A", duration_ms=201000)
    assert again == hit and client.requests_made == 1 and client.cache_hits == 1

    by_isrc = client.resolve("Song One", "Artist A", isrc="USAAA0100001")
    assert by_isrc["recording_id"] == "r-one-isrc" and by_isrc["method"] == "isrc"
    assert client.requests_made == 2
    assert naps and all(0 < nap <= 1.0 for nap in naps), "second live request waits for the 1 req/s limit"

    assert client.resolve("Completely Unknown", "Nobody") is None
    assert client.resolve("", "Nobody") is None
    assert all("fmt=json" in url for url in fake.urls)


def test_scan_and_diff(db, tmp_path):
    music = tmp_path / "Music"
    make_flac(music / "Artist A" / "Album A" / "01 - Song One.flac", seconds=200, title="Song One", artist="Artist A",
              album="Album A", musicbrainz_trackid="r-one")
    make_flac(music / "Artist A" / "Album A" / "02 - Song Two.flac", seconds=210, title="Song Two", artist="Artist A",
              album="Album A", isrc="USAAA0100002")
    make_flac(music / "Misc" / "untagged.flac", seconds=100)
    (music / "Misc" / "notes.txt").write_text("not audio")
    (music / "Misc" / "broken.flac").write_bytes(b"not a flac")

    result = scan_library(db, music)
    assert result["files"] == 4 and result["indexed"] == 3 and result["unreadable"] == 1
    assert db.library_count() == 3
    untagged = db.library_file(str(music / "Misc" / "untagged.flac"))
    assert untagged["title"] == "untagged" and untagged["duration_ms"] == 100000 and untagged["format"] == "flac"

    again = scan_library(db, music)
    assert again["indexed"] == 0 and again["unchanged"] == 3

    (music / "Misc" / "untagged.flac").unlink()
    assert scan_library(db, music)["removed"] == 1 and db.library_count() == 2

    playlist_id = db.add_playlist("P", "csv", [
        Track(title="Song One", artist="Artist A", mb_recording_id="r-one", position=0),
        Track(title="Song Two (feat. X)", artist="Artist A", isrc="USAAA0100002", position=1),
        Track(title="Song Two", artist="Artist A", duration_ms=211000, position=2),
        Track(title="Song Two", artist="Artist A", duration_ms=250000, position=3),
        Track(title="Missing", artist="Artist A", position=4),
    ])
    diff = diff_playlist(db, playlist_id)
    assert diff["in_library"] == 3
    assert diff["by_method"] == {"mbid": 1, "isrc": 1, "text+duration": 1}
    statuses = [r["status"] for r in db.tracks(playlist_id)]
    assert statuses == ["in_library", "in_library", "in_library", "pending", "pending"]
    assert db.tracks(playlist_id)[0]["local_path"].endswith("01 - Song One.flac")

    assert find_local(db, Track(title="song two", artist="ARTIST A")) is not None
    assert find_local(db, Track(title="nope")) is None
