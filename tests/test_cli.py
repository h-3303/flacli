"""The flacli command line and the config file: offline commands only (no bridge, no MusicBrainz)."""

import json
import os
import subprocess
import sys

from pathlib import Path

import pytest

from flacli import cli, config, jobs
from flacli.db import Database

from libtools import make_flac


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FLACLI_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("FLACLI_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("FLACLI_MUSIC_DIR", str(tmp_path / "Music"))
    monkeypatch.setenv("NICOTINE_MCP_SOCKET", str(tmp_path / "nowhere.sock"))
    monkeypatch.delenv("FLACLI_CONTACT", raising=False)
    (tmp_path / "Music").mkdir()
    return tmp_path


def run(*argv):
    import io
    from contextlib import redirect_stdout

    buffer = io.StringIO()

    with redirect_stdout(buffer):
        code = cli.main(list(argv))

    text = buffer.getvalue()
    return code, (json.loads(text) if text.lstrip().startswith("{") else text)


def test_config_file_env_precedence(home, monkeypatch):
    assert config.get("music_dir") == str(home / "Music")
    monkeypatch.delenv("FLACLI_MUSIC_DIR")
    assert config.get("music_dir") == "~/Music"

    path = config.write_setting("music_dir", "/srv/music")
    assert path == home / "config.toml"
    assert config.get("music_dir") == "/srv/music" and config.music_dir() == Path("/srv/music")
    config.write_setting("auto_tidy", "off")
    assert config.auto_tidy() is False and config.get("music_dir") == "/srv/music"

    monkeypatch.setenv("FLACLI_MUSIC_DIR", "/env/music")
    assert config.effective()["music_dir"] == {"value": "/env/music", "source": "env FLACLI_MUSIC_DIR",
                                               "description": config.DESCRIPTIONS["music_dir"]}
    assert config.effective()["auto_tidy"]["source"] == "file"

    with pytest.raises(ValueError, match="unknown setting"):
        config.write_setting("colour", "blue")


def test_config_command(home):
    code, shown = run("config")
    assert code == 0 and shown["config_file"] == str(home / "config.toml")
    assert shown["settings"]["music_dir"]["value"] == str(home / "Music")

    code, written = run("config", "set", "contact", "tests@example.invalid")
    assert code == 0 and written["contact"] == "tests@example.invalid"
    assert config.user_agent().startswith("flacli/") and "tests@example.invalid" in config.user_agent()

    code, error = run("config", "set", "nope", "x")
    assert code == 1 and "unknown setting" in error["error"]


def test_guide_and_help(home):
    code, text = run("guide", "--short")
    assert code == 0 and text.startswith("## Quick reference") and "flacli get" in text
    code, text = run("guide")
    assert "## Rules" in text

    with pytest.raises(SystemExit) as raised:
        cli.main(["--help"])

    assert raised.value.code == 0


def test_doctor_without_bridge(home):
    code, report = run("doctor")
    assert code == 0
    assert report["ok"] is False and report["nicotine"]["reachable"] is False and "MCP Bridge" in report["nicotine"]["fix"]
    assert report["music_dir_exists"] is True and report["playlists"] == 0


def test_skip_remaining_offline(home):
    fixtures = Path(__file__).parent / "fixtures"
    code, imported = run("import", str(fixtures / "exportify.csv"))
    assert code == 0
    (entry,) = imported["imported"]
    playlist_id = entry["playlist_id"]

    code, skipped = run("skip", str(playlist_id), "--remaining")
    assert code == 0 and skipped == {"skipped": entry["tracks"], "cancelled_downloads": 0, "playlist_id": playlist_id}

    code, status = run("status", str(playlist_id))
    assert code == 0 and status["counts"] == {"skipped": entry["tracks"]} and status["missing"] == [] or \
        {m["status"] for m in status["missing"]} == {"skipped"}

    code, _ = run("skip", str(playlist_id))
    assert code != 0


def test_import_status_scan_m3u_offline(home):
    fixtures = Path(__file__).parent / "fixtures"
    code, imported = run("import", str(fixtures / "exportify.csv"))
    assert code == 0
    (entry,) = imported["imported"]
    playlist_id = entry["playlist_id"]
    assert entry["tracks"] > 0

    make_flac(home / "Music" / "A" / "B" / "01 - x.flac", artist="A", album="B", title="x", tracknumber="1")
    code, scanned = run("scan")
    assert code == 0 and scanned.get("indexed", scanned.get("files", 1)) >= 1

    code, status = run("status")
    assert code == 0 and status["playlists"][0]["playlist_id"] == playlist_id
    # The player contract: these keys are what Flaclify's incoming indicator reads. Renaming one fails here first.
    listed = status["playlists"][0]
    assert set(listed) >= {"playlist_id", "name", "mpd_playlist", "counts", "job", "next"}
    assert listed["job"] is None and listed["mpd_playlist"] == listed["name"] and "flacli sync" in listed["next"]

    code, one = run("status", str(playlist_id))
    assert code == 0 and one["counts"] == {"pending": entry["tracks"]} and one["job"] is None
    assert "flacli sync" in one["next"] and one["mpd_playlist"] == one["name"]
    # ...and these are what its ghost rows read: every track not on disk, in order.
    assert [m["position"] for m in one["missing"]] == list(range(1, entry["tracks"] + 1))
    assert set(one["missing"][0]) == {"track_id", "position", "artist", "title", "album", "status"}
    assert {m["status"] for m in one["missing"]} == {"pending"}

    code, review = run("review", str(playlist_id), "--status", "pending", "--limit", "2")
    assert code == 0 and review["total"] == entry["tracks"] and len(review["tracks"]) == 2

    code, error = run("approve", str(playlist_id))
    assert code == 1 and "--tracks" in error["error"]

    code, m3u = run("m3u", str(playlist_id), "--path", str(home / "out.m3u8"))
    assert code == 0 and m3u["missing_count"] == entry["tracks"] and (home / "out.m3u8").exists()

    code, error = run("sync", str(playlist_id))
    assert code == 1 and "Nicotine+" in error["error"]   # bridge unreachable: fails early, no job created

    code, deleted = run("delete", str(playlist_id))
    assert code == 0 and deleted["deleted"] == playlist_id
    code, error = run("status", str(playlist_id))
    assert code == 1


def test_get_needs_bridge_but_prepares_offline(home, monkeypatch):
    from flacli import server
    from libtools import FakeMusicBrainz, recording

    server.State.mb_fetch = FakeMusicBrainz([recording("r1", "Royals", "Lorde", 190000)])
    server.State.mb_sleep = lambda s: None

    try:
        code, result = run("get", "Lorde - Royals", "--prepare-only")
        assert code == 0 and result["added"] == 1 and result["to_fetch"] == 1 and result["tracks"][0]["title"] == "Royals"
        assert "nothing fetched" in result["note"]

        code, error = run("get", "Lorde - Royals")
        assert code == 1 and "Nicotine+" in error["error"]
    finally:
        server.State.mb_fetch = None
        server.State.mb_sleep = None


def test_stale_job_reaping_and_cancel(home):
    db = Database(config.db_path())
    playlist_id = db.add_playlist("p", "csv", [])
    dead = db.create_job(playlist_id, "match", {"total": 1, "pid": 999_999_999})
    assert jobs.reap_stale(db) == 1 and db.job(dead)["status"] == "interrupted"

    live = db.create_job(playlist_id, "match", {"total": 1, "pid": os.getpid()})
    assert jobs.reap_stale(db) == 0
    description = jobs.describe(db.job(live))
    assert description["worker_alive"] is True and description["status"] == "running"

    orphan = db.create_job(playlist_id, "match", {"total": 1})
    db.update_job(live, status="finished")
    assert jobs.cancel(db, playlist_id)["note"] == "worker was already gone"
    assert db.job(orphan)["status"] == "cancelled"
    db.close()


def test_worker_module_runs_a_match_job_with_unreachable_bridge(home):
    db = Database(config.db_path())
    playlist_id = db.add_playlist("p", "csv", [])
    job_id = db.create_job(playlist_id, "match", {"total": 0})
    db.close()
    payload = json.dumps({"prefs": {"harvest_seconds": 0.1}})
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parent.parent / "src")}
    proc = subprocess.run([sys.executable, "-m", "flacli.worker", "match", str(playlist_id), str(job_id), payload],
                          capture_output=True, text=True, timeout=60, env=env)
    assert proc.returncode == 0, proc.stderr

    db = Database(config.db_path())
    assert db.job(job_id)["status"] == "finished"   # no rows to match: finishes without touching the bridge
    db.close()
