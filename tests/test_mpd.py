"""The player's MPD: scoped updates after tidy, playlists stored under their own name, doctor's report. Against a
fake MPD on a Unix socket in the temp dir; never a real one (conftest sets FLACLI_MPD=off for every other test)."""

import json
import os

from pathlib import Path

import pytest

from flacli import cli, config, mpd
from flacli.db import Database

from fakempd import FakeMpd
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


@pytest.fixture
def player(home, monkeypatch):
    server = FakeMpd(home / "Music", home / "mpd.sock").start()
    monkeypatch.setenv("FLACLI_MPD", str(home / "mpd.sock"))
    yield server
    server.stop()


def run(*argv):
    import io
    from contextlib import redirect_stdout

    buffer = io.StringIO()

    with redirect_stdout(buffer):
        code = cli.main(list(argv))

    return code, json.loads(buffer.getvalue())


def test_addresses_and_quoting(monkeypatch):
    assert mpd.parse_address("secret@/run/mpd/socket") == ("/run/mpd/socket", "secret")
    assert mpd.parse_address("localhost:6600") == ("localhost:6600", None)
    assert mpd.candidates() == []                     # conftest: off
    monkeypatch.setenv("FLACLI_MPD", "pw@nas:6601")
    assert mpd.candidates() == [("nas:6601", "pw")]
    monkeypatch.setenv("FLACLI_MPD", "")
    monkeypatch.setenv("MPD_HOST", "nas")
    monkeypatch.setenv("MPD_PORT", "6602")
    assert mpd.candidates() == [("nas:6602", None)]
    monkeypatch.delenv("MPD_HOST")
    monkeypatch.delenv("MPD_PORT")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/nonexistent")
    assert mpd.candidates()[-1] == ("localhost:6600", None)
    assert mpd.playlist_name(" Road/Trip \n") == "Road_Trip _"
    assert mpd.playlist_name("..") == "playlist"
    assert mpd._quote('a "b" \\c') == '"a \\"b\\" \\\\c"'


def test_off_and_unreachable(home, monkeypatch):
    assert mpd.probe() == {"reachable": False, "skipped": "mpd is off (config set mpd '' to detect it again)"}
    assert mpd.notify_paths([str(home / "Music")]) == {"skipped": "mpd is off"}
    monkeypatch.setenv("FLACLI_MPD", str(home / "no-such.sock"))
    report = mpd.probe()
    assert report["reachable"] is False and "cannot connect" in report["error"] and report["tried"] == [str(home / "no-such.sock")]
    assert "skipped" in mpd.notify_paths([str(home / "Music")])
    assert "skipped" in mpd.save_playlist("x", [str(home / "Music" / "a.flac")])


def test_probe_reports_the_same_library(player, home):
    report = mpd.probe()
    assert report["reachable"] is True and report["version"] == "0.24.0" and report["same_library"] is True
    assert report["music_directory"] == str(home / "Music") and report["playlists"] == 0

    code, doctor = run("doctor")
    assert code == 0 and doctor["mpd"]["reachable"] is True and doctor["mpd"]["same_library"] is True

    code, status = run("mpd")
    assert code == 0 and status["address"] == str(home / "mpd.sock")


def test_probe_notices_a_different_library(home, monkeypatch):
    other = home / "Elsewhere"
    other.mkdir()
    server = FakeMpd(other, home / "other.sock").start()
    monkeypatch.setenv("FLACLI_MPD", str(home / "other.sock"))

    try:
        report = mpd.probe()
        assert report["same_library"] is False and "skipped" in report["note"]
        assert "outside" in mpd.notify_paths([str(home / "Music" / "A")])["skipped"]
    finally:
        server.stop()


def test_music_dir_inside_a_larger_library(home, monkeypatch):
    """MPD serving ~ while flacli files into ~/Music: uris are relative to MPD's root."""
    server = FakeMpd(home, home / "big.sock").start()
    monkeypatch.setenv("FLACLI_MPD", str(home / "big.sock"))
    make_flac(home / "Music" / "A" / "B" / "01 - x.flac", artist="A", album="B", title="x", tracknumber="1")

    try:
        assert mpd.probe()["note"].endswith("at Music")
        sent = mpd.notify_paths([str(home / "Music" / "A" / "B" / "01 - x.flac")])
        assert sent["updated"] == ["Music/A/B"] and sent["job"] == 1
        assert 'update "Music/A/B"' in server.commands
    finally:
        server.stop()


def test_tidy_new_tells_mpd_about_the_folder(player, home):
    incoming = home / "Music" / "Downloads" / "some_user" / "x.flac"
    make_flac(incoming, artist="The Cardigans", album="Gran Turismo", title="Paper Cup", tracknumber="1")

    code, result = run("tidy", "--new", "--paths", str(incoming))
    assert code == 0 and result["moved"] == 1
    assert result["mpd"]["updated"] == ["The Cardigans/Gran Turismo"]
    assert player.commands.count('update "The Cardigans/Gran Turismo"') == 1
    assert "The Cardigans/Gran Turismo/01 - Paper Cup.flac" in player.known

    # many folders at once: one update of everything
    paths = []

    for n in range(mpd.MAX_SCOPED_UPDATES + 1):
        path = home / "Music" / f"Artist {n}" / "Album" / "01 - t.flac"
        make_flac(path, artist=f"Artist {n}", album="Album", title="t", tracknumber="1")
        paths.append(str(path))

    sent = mpd.notify_paths(paths, wait=True)
    assert sent["updated"] == [""] and sent["finished"] is True and player.commands.count("update") == 1


def test_m3u_stores_the_playlist_in_mpd(player, home):
    fixtures = Path(__file__).parent / "fixtures"
    code, imported = run("import", str(fixtures / "exportify.csv"))
    (entry,) = imported["imported"]
    playlist_id = entry["playlist_id"]
    code, review = run("review", str(playlist_id), "--status", "pending", "--limit", "2")
    first, second = review["tracks"][0], review["tracks"][1]

    # two of its tracks are on disk (marked as the library diff would, without MusicBrainz); MPD has only
    # been told about one of them
    db = Database(config.db_path())

    for n, track in enumerate((first, second), start=1):
        path = home / "Music" / track["artist"] / "Album" / f"0{n} - {track['title']}.flac"
        make_flac(path, artist=track["artist"], album="Album", title=track["title"], tracknumber=str(n))
        db.set_match(track["track_id"], "in_library" if n == 1 else "done", local_path=str(path))

    db.close()
    player.scan(first["artist"])

    code, m3u = run("m3u", str(playlist_id), "--path", str(home / "out.m3u8"))
    assert code == 0 and m3u["missing_count"] == entry["tracks"] - 2
    stored = m3u["mpd"]
    assert stored["playlist"] == entry["name"] and stored["added"] == 2 and stored["not_in_db"] == [] and stored["updated"] is True
    assert player.playlists[entry["name"]] == [f"{first['artist']}/Album/01 - {first['title']}.flac",
                                               f"{second['artist']}/Album/02 - {second['title']}.flac"]

    # a second write replaces the stored playlist instead of appending to it
    code, again = run("mpd", "playlist", str(playlist_id))
    assert code == 0 and again["local_tracks"] == 2 and again["mpd"]["added"] == 2
    assert len(player.playlists[entry["name"]]) == 2

    code, error = run("mpd", "playlist")
    assert code == 1 and "usage" in error["error"]


def test_update_command_and_tcp_style_without_config(home, monkeypatch):
    """Over TCP MPD refuses `config`; flacli then assumes its own music_dir is MPD's root."""
    server = FakeMpd(home / "Music", home / "tcp.sock", refuse_config=True).start()
    monkeypatch.setenv("FLACLI_MPD", str(home / "tcp.sock"))
    make_flac(home / "Music" / "A" / "B" / "01 - x.flac", artist="A", album="B", title="x", tracknumber="1")

    try:
        report = mpd.probe()
        assert report["music_directory"] is None and "TCP" in report["note"]
        code, sent = run("mpd", "update", str(home / "Music" / "A"))
        assert code == 0 and sent["updated"] == ["A"] and sent["finished"] is True
        code, whole = run("mpd", "update")
        assert code == 0 and whole["updated"] == [""]
    finally:
        server.stop()


def test_password(home, monkeypatch):
    server = FakeMpd(home / "Music", home / "pw.sock", password="hunter2").start()

    try:
        monkeypatch.setenv("FLACLI_MPD", f"hunter2@{home / 'pw.sock'}")
        assert mpd.probe()["reachable"] is True
        monkeypatch.setenv("FLACLI_MPD", str(home / "pw.sock"))
        assert "permission" in mpd.probe()["error"]
    finally:
        server.stop()


def test_config_setting_round_trip(home):
    code, written = run("config", "set", "mpd", "off")
    assert code == 0 and written["mpd"] == "off" and config.mpd_address() == "off"
    assert mpd.candidates() == []
