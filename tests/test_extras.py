"""Phase 3 extras: beets hand-off, Troi resolver and the download-completion monitor.

beets and troi are stand-ins: small executables written into a temp dir that record their argv and answer
like the real tools. The monitor runs against the in-process Nicotine+ harness.
"""

import importlib.util
import json
import os
import stat
import sys

from pathlib import Path

import pytest

from flacli.beets import Beets, BeetsError
from flacli.db import Database
from flacli.models import Track
from flacli.troi_resolver import Troi, TroiError



def fake_tool(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text("#!/usr/bin/env python3\nimport json, os, sys\nARGS = sys.argv[1:]\nLOG = os.environ['FAKE_LOG']\n"
                    "open(LOG, 'a').write(json.dumps(ARGS) + '\\n')\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def logged(log: Path):
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def rows_for(tracks):
    return [dict(t) for t in tracks]


# beets #

BEET_BODY = r'''
cmd = ARGS[0] if ARGS else ""
if cmd == "version":
    print("beets version 2.14.0"); print("Python version 3.14.7")
elif cmd == "config" and "-p" in ARGS:
    print("/home/x/.config/beets/config.yaml")
elif cmd == "config":
    print("directory: /music/library\nlibrary: /music/library.db\nimport:\n    copy: yes\n    move: no\n    write: yes\nplugins: []")
elif cmd == "import":
    folder = ARGS[-1]
    if "--pretend" in ARGS:
        for name in sorted(os.listdir(folder)):
            print(os.path.join(folder, name))
        sys.exit(0)
    if os.environ.get("FAKE_BEET_FAIL") and "fail" in folder:
        print("Skipping."); sys.exit(1)
    print(f"imported {folder}")
elif cmd == "ls":
    query = ARGS[-1]
    table = json.loads(os.environ.get("FAKE_BEET_PATHS") or "{}")
    if query.startswith("mb_trackid:") and query.partition(":")[2] in table:
        print(table[query.partition(":")[2]])
'''


@pytest.fixture
def beet(tmp_path, monkeypatch):
    log = tmp_path / "beet.log"
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FLACLI_DATA", str(tmp_path / "data"))
    executable = fake_tool(tmp_path, "beet", BEET_BODY)
    return Beets(which=lambda name: str(executable) if name == "beet" else None), log


def done_rows(tmp_path):
    album = tmp_path / "dl" / "Band B - B Album"
    single = tmp_path / "dl" / "misc"
    files = {
        1: (album / "01 - Track 1.flac", "rel-b", "r-b1"), 2: (album / "02 - Track 2.flac", "rel-b", "r-b2"),
        3: (album / "03 - Track 3.flac", "rel-b", "r-b3"), 4: (single / "Lonely Song.flac", "rel-c", "r-c1"),
        5: (single / "Song Two.flac", None, "r-a2"),
    }
    rows = []

    for track_id, (path, release, recording) in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fLaC")
        rows.append({"id": track_id, "local_path": str(path), "mb_release_id": release, "mb_recording_id": recording})

    rows.append({"id": 6, "local_path": None, "mb_release_id": None, "mb_recording_id": None})
    return rows


def test_beets_status_and_plan(beet, tmp_path):
    beets, log = beet
    status = beets.status()
    assert status["installed"] is True and status["version"] == "beets version 2.14.0"
    assert status["config"] == "/home/x/.config/beets/config.yaml" and status["library_directory"] == "/music/library"
    assert status["import"] == {"copy": "yes", "move": "no", "write": "yes"}

    plan = Beets.plan(done_rows(tmp_path))
    assert [(p["mode"], p["tracks"], p["release_id"]) for p in plan] == [("album", 3, "rel-b"), ("singletons", 2, None)]
    assert plan[1]["recording_ids"] == ["r-c1", "r-a2"] and plan[0]["missing_files"] == []


def test_beets_dry_run_then_import(beet, tmp_path, monkeypatch):
    beets, log = beet
    plan = beets.dry_run(Beets.plan(done_rows(tmp_path)))
    assert plan[0]["command"].endswith(f"import -q --pretend --search-id rel-b {plan[0]['folder']}")
    assert plan[1]["command"].endswith(f"import -q --pretend -s --search-id r-c1 --search-id r-a2 {plan[1]['folder']}")
    assert [os.path.basename(line) for line in plan[0]["pretend"]] == ["01 - Track 1.flac", "02 - Track 2.flac", "03 - Track 3.flac"]
    assert all(entry[0] != "import" or "--pretend" in entry for entry in logged(log)), "the dry run must never import"

    monkeypatch.setenv("FAKE_BEET_PATHS", json.dumps({"r-b1": "/music/library/Band B/B Album/01 Track 1.flac"}))
    results = beets.import_folders(plan, move=True)
    assert [r["imported"] for r in results] == [True, True]
    real = [entry for entry in logged(log) if entry[0] == "import" and "--pretend" not in entry]
    assert real[0][:4] == ["import", "-q", "-m", "-l"] and real[0][4].endswith("beets-import.log")
    assert real[0][-3:] == ["--search-id", "rel-b", plan[0]["folder"]]
    assert beets.path_for_recording("r-b1") == "/music/library/Band B/B Album/01 Track 1.flac"
    assert beets.path_for_recording("r-b2") is None and beets.path_for_recording(None) is None


def test_beets_missing_files_and_absent_binary(beet, tmp_path):
    beets, _ = beet
    rows = done_rows(tmp_path)
    os.remove(rows[0]["local_path"])
    plan = beets.dry_run(Beets.plan(rows))
    assert plan[0]["missing_files"] == [rows[0]["local_path"]] and plan[0]["pretend"] == ["skipped: files missing on disk"]
    assert beets.import_folders(plan)[0]["imported"] is False

    absent = Beets(which=lambda name: None)
    assert absent.status()["installed"] is False

    with pytest.raises(BeetsError, match="not installed"):
        absent.dry_run(plan)


# Troi #

TROI_BODY = r'''
if ARGS[:2] == ["db", "create"]:
    open(ARGS[ARGS.index("-d") + 1], "w").write("db")
elif ARGS[:2] == ["db", "scan"]:
    print("add 100% Song One")
elif ARGS[0] == "resolve":
    query = json.load(open(ARGS[-1]))["playlist"]["track"]
    collection = json.loads(os.environ["FAKE_TROI_COLLECTION"])
    out = ["#EXTM3U", "#EXTENC: UTF-8", "#PLAYLIST t"]
    hits = 0
    for track in query:
        mbid = (track.get("identifier") or [""])[0].rpartition("/")[2]
        path = collection.get(mbid) or collection.get(track["title"])
        if path:
            out += [f"#EXTINF 0,{track['title']}", path]; hits += 1
    if not hits:
        sys.stderr.write("Sorry, but no tracks could be resolved, no playlist generated.\nCannot save empty playlist.\n"); sys.exit(0)
    open(ARGS[ARGS.index("-m") + 1], "w").write("\n".join(out) + "\n")
'''


@pytest.fixture
def troi(tmp_path, monkeypatch):
    log = tmp_path / "troi.log"
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FLACLI_DATA", str(tmp_path / "data"))
    executable = fake_tool(tmp_path, "troi", TROI_BODY)
    return Troi(which=lambda name: str(executable) if name == "troi" else None, db_file=tmp_path / "data" / "troi.db"), log


def test_troi_scan_and_resolve(troi, tmp_path, monkeypatch):
    resolver, log = troi
    assert resolver.status()["installed"] is True and resolver.status()["indexed"] is False

    with pytest.raises(TroiError, match="not indexed"):
        resolver.resolve([], 0.8)

    music = tmp_path / "Music"
    music.mkdir()
    result = resolver.scan(music)
    assert result["created"] is True and resolver.status()["indexed"] is True
    assert logged(log)[0][:2] == ["db", "create"] and logged(log)[1] == ["db", "scan", "-d", str(resolver.db_file), "-q", str(music)]

    one = music / "Artist A" / "Song One.flac"
    two = music / "Artist A" / "Song Two.flac"
    for f in (one, two):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"fLaC")

    monkeypatch.setenv("FAKE_TROI_COLLECTION", json.dumps({"r-a1": str(one), "Song Two": str(two), "Gone": str(music / "gone.flac")}))
    rows = [
        {"id": 11, "title": "Song One", "artist": "Artist A", "album": "Album A", "mb_recording_id": "r-a1"},
        {"id": 12, "title": "Song Two", "artist": "Artist A", "album": "", "mb_recording_id": None},
        {"id": 13, "title": "Gone", "artist": "X", "album": "", "mb_recording_id": None},
        {"id": 14, "title": "Nope", "artist": "Nobody", "album": "", "mb_recording_id": "r-zz"},
    ]
    found = resolver.resolve(rows, 0.8)
    assert found == {11: str(one), 12: str(two)}       # 13 resolved to a file that does not exist; 14 unresolved
    query = json.loads((tmp_path / "data" / "troi" / "query.jspf").read_text())["playlist"]["track"]
    assert query[0]["identifier"] == ["https://musicbrainz.org/recording/r-a1"] and query[1]["identifier"] == []
    assert all("https://musicbrainz.org/doc/jspf#track" in t["extension"] for t in query)
    resolve_call = [entry for entry in logged(log) if entry[0] == "resolve"][0]
    assert resolve_call[1:6] == ["-d", str(resolver.db_file), "-t", "0.8", "-m"] and resolve_call[-3:-1] == ["-y", "-q"]

    monkeypatch.setenv("FAKE_TROI_COLLECTION", "{}")
    assert resolver.resolve(rows, 0.8) == {}

    with pytest.raises(ValueError, match="threshold"):
        resolver.resolve(rows, 2)


def test_troi_absent():
    absent = Troi(which=lambda name: None, db_file=Path("/nonexistent/troi.db"))
    assert absent.status()["installed"] is False and "pipx" in absent.status()["note"]

    with pytest.raises(TroiError, match="pipx"):
        absent.scan(Path("/"))


def test_parse_m3u():
    text = "#EXTM3U\n#EXTINF 0,A, with comma\n/x/a.flac\n/x/b.flac\n#EXTINF 0,C\n\n/x/c.flac\n"
    assert Troi.parse_m3u(text) == [("A, with comma", "/x/a.flac"), ("", "/x/b.flac"), ("C", "/x/c.flac")]


# Server tools for the extras #

@pytest.fixture
def library(tmp_path, monkeypatch):
    import flacli.server as server

    monkeypatch.setenv("FLACLI_DATA", str(tmp_path / "data"))
    server.State.db = None
    yield server

    if server.State.db is not None:
        server.State.db.close()
        server.State.db = None


@pytest.mark.anyio
async def test_beets_import_tool(library, beet, tmp_path, monkeypatch):
    beets, log = beet
    monkeypatch.setattr(library, "_beets", lambda: beets)
    rows = done_rows(tmp_path)
    tracks = [Track(title=f"T{r['id']}", artist="A", mb_release_id=r["mb_release_id"], mb_recording_id=r["mb_recording_id"]) for r in rows]
    playlist_id = library.db().add_playlist("P", "csv", tracks)

    for row, track_row in zip(rows, library.db().tracks(playlist_id)):
        if row["local_path"]:
            library.db().set_match(track_row["id"], "done", local_path=row["local_path"])

    plan = await library.beets_import(playlist_id)
    assert plan["dry_run"] is True and [f["mode"] for f in plan["folders"]] == ["album", "singletons"]
    assert plan["beets"]["installed"] is True
    assert not [e for e in logged(log) if e[0] == "import" and "--pretend" not in e]

    first_done = next(r for r in library.db().tracks(playlist_id) if r["status"] == "done")
    monkeypatch.setenv("FAKE_BEET_PATHS", json.dumps({first_done["mb_recording_id"]: "/music/library/new/01.flac"}))
    result = await library.beets_import(playlist_id, confirm=True)
    assert result["folders_imported"] == 2 and result["folders_failed"] == 0 and result["tracks_relocated"] == 1
    assert library.db().tracks(playlist_id)[0]["local_path"] == "/music/library/new/01.flac"


@pytest.mark.anyio
async def test_troi_resolve_tool(library, troi, tmp_path, monkeypatch):
    resolver, _ = troi
    monkeypatch.setattr(library, "_troi", lambda: resolver)
    music = tmp_path / "Music"
    (music / "a").mkdir(parents=True)
    owned = music / "a" / "one.flac"
    owned.write_bytes(b"fLaC")
    await library.troi_scan(str(music))
    tracks = [Track(title="Song One", artist="Artist A", mb_recording_id="r-a1"), Track(title="Song Two", artist="Artist A"),
              Track(title="Already", artist="Artist A", local_path=str(owned))]
    playlist_id = library.db().add_playlist("P", "csv", tracks)
    monkeypatch.setenv("FAKE_TROI_COLLECTION", json.dumps({"r-a1": str(owned)}))

    result = await library.troi_resolve(playlist_id)
    assert result["queried"] == 2 and result["found"] == 1
    assert result["tracks"][0]["title"] == "Song One" and result["counts"] == {"in_library": 2, "pending": 1}

    status = await library.troi_status()
    assert status["indexed"] is True
