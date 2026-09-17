"""The tidy planner: tag rules, FLAC-beats-lossy, target paths, decisions file, apply and its refusals."""

import json
import os
import time

from pathlib import Path

import pytest

from flacli import tidy
from flacli.tidy import Tidy, TidyError

from libtools import make_flac

# One MPEG-1 Layer III frame: 128 kbit/s, 44.1 kHz, no padding = 417 bytes. mutagen needs a few to read the stream.
MP3_FRAME = bytes([0xFF, 0xFB, 0x90, 0x00]) + b"\x00" * 413


def make_mp3(path: Path, frames=40, **tags):
    from mutagen.id3 import ID3, TALB, TIT2, TPE1, TRCK
    from mutagen.mp3 import MP3

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(MP3_FRAME * frames)
    audio = MP3(str(path))
    audio.add_tags()
    frames_by_tag = {"artist": TPE1, "album": TALB, "title": TIT2, "tracknumber": TRCK}

    for key, value in tags.items():
        audio.tags.add(frames_by_tag[key](encoding=3, text=[str(value)]))

    audio.save()
    return path


def settle(root):
    """Push every mtime into the past so the in-flight-download guard does not trigger."""
    old = time.time() - 3600

    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            os.utime(os.path.join(dirpath, name), (old, old))


def fields(path):
    return tidy.get_fields(tidy.open_file(str(path)))


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "Music"
    root.mkdir()
    return root


def test_helpers():
    assert tidy.akey("The Béatles & Co.") == "beatles and co"
    assert tidy.split_feat("Lana Del Rey (feat. The Weeknd)") == ("Lana Del Rey", "The Weeknd")
    assert tidy.split_feat("Lana Del Rey ft. The Weeknd") == ("Lana Del Rey", "The Weeknd")
    assert tidy.album_key("Lust For Life (Deluxe Edition)") == tidy.album_key("Lust for Life")
    assert tidy.title_key("Song (Official Video) [feat. X]") == "song"
    assert tidy.sanitize('AC/DC: Back?in*Black "Live" <1>') == "AC-DC - BackinBlack 'Live' 1"


def test_analyse_plans_tags_moves_and_flac_beats_lossy(library):
    make_flac(library / "loose.flac", seconds=200, artist=" Artist A ", album="Album A", title="Song One", tracknumber="1/10", date="2001-05-06T00:00")
    make_flac(library / "Artist A" / "Album A" / "02 - Song Two.flac", seconds=180, artist="Artist A", albumartist="Artist A",
              album="Album A", title="Song Two", tracknumber="2", date="2001-05-06")
    make_flac(library / "third.flac", seconds=190, artist="Artist A", album="Album A", title="Song Three (feat. Guest)", tracknumber="3")
    make_mp3(library / "video rip.mp3", artist="Artist A", album="Album A", title="Song One (Official Video)", tracknumber="1")
    (library / "Download Report 1.txt").write_text("Download Report for playlist X\n\n1. Artist Z - Missing Song\n   Error: no results\n")
    (library / ".thumbnails" / "x.png").parent.mkdir()
    (library / ".thumbnails" / "x.png").write_bytes(b"png")
    (library / "notes.md").write_text("stray")
    (library / "Artist A").mkdir(exist_ok=True)
    (library / "Artist A" / "artist.md").write_text("bio")   # flacli wiki's own sidecar: not a stray
    settle(library)

    summary = Tidy(library).analyse()

    assert summary["files"] == 4 and summary["unreadable"] == [] and summary["recent_files"] == []
    assert Path(summary["report_path"]).is_file() and Path(summary["approved_path"]).is_file()
    assert Path(summary["plan_path"]).is_file()
    by_rule = summary["tag_changes"]["by_rule"]
    assert by_rule["R1 trim whitespace"]["files"] == 1
    assert by_rule["R6 DATE as YYYY or YYYY-MM-DD"]["files"] == 1
    assert by_rule["R6 DATE propagated from album siblings"]["files"] == 2   # third.flac and the rip
    assert by_rule["R7 total moved to TOTAL* field"]["files"] == 1
    assert by_rule["R3 set ALBUMARTIST"]["files"] == 3    # everything but the already-tagged file
    assert summary["deletions"] == [{"path": "video rip.mp3", "rule": "R10 flac beats lossy",
                                     "why": summary["deletions"][0]["why"]}]
    assert "music-video-length rip" in summary["deletions"][0]["why"]
    assert summary["moves"] == 2 and summary["download_reports"] == 1
    assert summary["failed_downloads"] == [{"report": "Download Report 1.txt", "item": "Artist Z - Missing Song", "error": "Error: no results"}]
    assert summary["open_questions"]["strays"] == ["notes.md"]
    assert summary["open_questions"]["path_clashes"] == 0

    plan = json.loads(Path(summary["plan_path"]).read_text())
    assert ["ARTIST", [" Artist A "], ["Artist A"], "R1 trim whitespace"] in plan["loose.flac"]
    report = Path(summary["report_path"]).read_text()
    assert "loose.flac" in report and "-> Artist A/Album A/01 - Song One.flac" in report
    assert "third.flac" in report and "Song Three" in report

    # the guest stays in TITLE: ARTIST alone does not name them, so R2b leaves it (only R3 fills ALBUMARTIST)
    third = plan["third.flac"]
    assert not any(rule.startswith("R2b") for _, _, _, rule in third)


def test_feat_moves_from_title_when_artist_names_the_guest(library):
    make_flac(library / "a.flac", artist="Primary & Guest", album="X", title="Song (feat. Guest)", tracknumber="1")
    settle(library)
    summary = Tidy(library).analyse()
    plan = json.loads(Path(summary["plan_path"]).read_text())["a.flac"]
    assert ["ARTIST", ["Primary & Guest"], ["Primary feat. Guest"], "R2b move feat. from TITLE into ARTIST"] in plan
    assert ["TITLE", ["Song (feat. Guest)"], ["Song"], "R2b move feat. from TITLE into ARTIST"] in plan
    assert ["ALBUMARTIST", [], ["Primary"], "R3 set ALBUMARTIST"] in plan


def test_various_artists_and_open_questions(library):
    for n, artist in enumerate(["One", "Two", "Three"], start=1):
        make_flac(library / "Mix" / f"{n}.flac", artist=artist, album="Mix", title=f"T{n}", tracknumber=str(n))

    make_flac(library / "b1.flac", artist="Built to Spill", album="Keep It Like a Secret", title="Carry the Zero", tracknumber="1")
    make_flac(library / "b2.flac", artist="Built To Spill", album="Keep It Like a Secret", title="Sidewalk", tracknumber="2")
    make_flac(library / "noalbum.flac", artist="Solo", title="Single")
    settle(library)

    summary = Tidy(library).analyse()
    questions = summary["open_questions"]
    assert questions["various_artists_groups"] == ["Mix"]
    assert questions["artist_spelling_variants"] == 1
    assert questions["missing_album"] == 1


def test_decisions_file_drives_canon_fill_and_deletion(library):
    make_flac(library / "b1.flac", artist="Built to Spill", album="Keep It Like a Secret", title="Carry the Zero", tracknumber="1")
    make_flac(library / "b2.flac", artist="Built To Spill", album="Keep It Like a Secret", title="Sidewalk", tracknumber="2")
    make_flac(library / "noalbum.flac", artist="Solo", title="Single")
    make_flac(library / "dump1.flac", artist="A", album="Mindful", title="x", tracknumber="1")
    make_flac(library / "dump2.flac", artist="B", album="Mindful", title="y", tracknumber="2")
    (library / "booklet.pdf").write_bytes(b"%PDF")
    work = library / ".tidy"
    work.mkdir()
    (work / "approved.py").write_text(
        "ARTIST_CANON = {'Built to Spill': 'Built To Spill'}\n"
        "ALBUM_FILL = {'noalbum.flac': 'Single'}\n"
        "DELETE_ALBUMS = ['mindful']\n"
        "DATE_FILL = {'built to spill|keep it like a secret': '1999-02-02'}\n"
        "MOVE = {'booklet.pdf': 'Built To Spill/Keep It Like a Secret/booklet.pdf'}\n"
    )
    settle(library)

    summary = Tidy(library).analyse()
    assert summary["open_questions"]["artist_spelling_variants"] == 1   # reported from the pre-canon snapshot
    by_rule = summary["tag_changes"]["by_rule"]
    assert by_rule["R2c canonical artist spelling"]["files"] == 1
    assert by_rule["R12 fill missing ALBUM"]["files"] == 1
    assert by_rule["R6c DATE filled from approved lookup"]["files"] == 2
    assert sorted(d["path"] for d in summary["deletions"]) == ["dump1.flac", "dump2.flac"]
    assert summary["extras_to_file"] == 1
    assert summary["moves"] == 3

    result = Tidy(library).apply()
    assert (result["deleted"], result["retagged"], result["moved"], result["extras_filed"], result["errors"]) == (2, 3, 3, 1, [])
    assert Path(result["backup_path"]).is_file()
    assert (library / "Built To Spill" / "Keep It Like a Secret" / "01 - Carry the Zero.flac").is_file()
    assert (library / "Built To Spill" / "Keep It Like a Secret" / "booklet.pdf").is_file()
    assert (library / "Solo" / "Single" / "Single.flac").is_file()
    assert not (library / "dump1.flac").exists()
    assert fields(library / "Built To Spill" / "Keep It Like a Secret" / "01 - Carry the Zero.flac")["DATE"] == ["1999-02-02"]
    assert fields(library / "Built To Spill" / "Keep It Like a Secret" / "01 - Carry the Zero.flac")["ALBUMARTIST"] == ["Built To Spill"]
    log = (work / "tag_changes.log").read_text()
    assert "DELETED" in log and "R14 Artist/Album/NN - Title" in log and "R17 album extra filed with its album" in log

    # a second pass has nothing left to do
    again = Tidy(library).analyse()
    assert again["tag_changes"]["files"] == 0 and again["deletions"] == [] and again["moves"] == 0


def test_apply_writes_every_format(library):
    make_flac(library / "a.flac", artist="Art", album="Alb", title="One", tracknumber="1/9")
    make_mp3(library / "b.mp3", artist="Art", album="Alb", title="Two", tracknumber="2/9")
    settle(library)

    result = Tidy(library).apply()
    assert result["retagged"] == 2 and result["errors"] == []
    flac = fields(library / "Art" / "Alb" / "01 - One.flac")
    mp3 = fields(library / "Art" / "Alb" / "02 - Two.mp3")
    assert flac["TRACKNUMBER"] == ["1"] and flac["TOTALTRACKS"] == ["9"] and flac["ALBUMARTIST"] == ["Art"]
    assert mp3["TRACKNUMBER"] == ["2"] and mp3["TOTALTRACKS"] == ["9"] and mp3["ALBUMARTIST"] == ["Art"]


def test_apply_refuses_recent_files_clashes_and_unreadable(library):
    make_flac(library / "a.flac", artist="Art", album="Alb", title="One", tracknumber="1")

    with pytest.raises(TidyError, match="download may be in progress"):
        Tidy(library).apply()

    settle(library)
    Tidy(library).apply()   # force not needed once settled

    make_flac(library / "dup.flac", artist="Art", album="Alb", title="One", tracknumber="1")
    settle(library)
    summary = Tidy(library).analyse()
    assert summary["open_questions"]["path_clashes"] == 1

    with pytest.raises(TidyError, match="path clashes"):
        Tidy(library).apply(force=True)

    (library / "dup.flac").unlink()
    (library / "broken.flac").write_bytes(b"fLaC" + b"\x00" * 10)
    settle(library)
    assert Tidy(library).analyse()["unreadable"][0]["path"] == "broken.flac"

    with pytest.raises(TidyError, match="unreadable"):
        Tidy(library).apply(force=True)


def test_missing_root():
    with pytest.raises(FileNotFoundError):
        Tidy("/nonexistent/music/dir")


def test_cli(library, capsys):
    make_flac(library / "a.flac", artist="Art", album="Alb", title="One", tracknumber="1")
    settle(library)
    tidy.main(["analyse", str(library)])
    out = capsys.readouterr().out
    assert "# music-tidy dry run" in out and '"moves": 1' in out
    tidy.main(["apply", str(library)])
    assert '"moved": 1' in capsys.readouterr().out
    assert (library / "Art" / "Alb" / "01 - One.flac").is_file()


@pytest.mark.anyio
async def test_server_tools_confirm_gate(library, monkeypatch):
    from flacli import server

    make_flac(library / "a.flac", artist="Art", album="Alb", title="One", tracknumber="1")
    settle(library)
    monkeypatch.setenv("FLACLI_MUSIC_DIR", str(library))

    summary = await server.tidy_analyse()
    assert summary["moves"] == 1 and summary["root"] == str(library)

    unconfirmed = await server.tidy_apply()
    assert unconfirmed["applied"] is False and unconfirmed["moves"] == 1
    assert (library / "a.flac").is_file()

    applied = await server.tidy_apply(confirm=True)
    assert applied["applied"] is True and applied["moved"] == 1
    assert (library / "Art" / "Alb" / "01 - One.flac").is_file()

    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError, match="does not exist"):
        await server.tidy_analyse(music_dir=str(library / "nope"))


def test_apply_new_touches_only_the_new_files(library):
    # already filed, with a whitespace problem the full tidy would fix: must stay untouched here
    make_flac(library / "Art" / "Alb" / "01 - One.flac", artist=" Art ", albumartist="Art", album="Alb", title="One", tracknumber="1")
    # just arrived (no settle: the caller knows these are complete)
    dl = library / "Art - Alb (2001) [FLAC]"
    make_flac(dl / "02 - Two.flac", artist="Art", album="Alb", title="Two", tracknumber="2/9")
    make_mp3(dl / "01 - One.mp3", artist="Art", album="Alb", title="One", tracknumber="1")   # lossy copy of an owned FLAC
    (dl / "cover.jpg").write_bytes(b"jpg")
    make_flac(library / "noalbum.flac", artist="Solo", title="Single")
    (library / "Download Report 2.txt").write_text("Download Report\n")

    result = Tidy(library).apply_new([dl / "02 - Two.flac", dl / "01 - One.mp3", library / "noalbum.flac",
                                      library / "missing.flac", "/elsewhere/x.flac"])
    assert result["moved"] == 1 and result["retagged"] == 2 and result["errors"] == []
    assert result["moves"] == {str(dl / "02 - Two.flac"): str(library / "Art" / "Alb" / "02 - Two.flac")}
    two = fields(library / "Art" / "Alb" / "02 - Two.flac")
    assert two["TRACKNUMBER"] == ["2"] and two["TOTALTRACKS"] == ["9"] and two["ALBUMARTIST"] == ["Art"]
    assert fields(library / "Art" / "Alb" / "01 - One.flac")["ARTIST"] == [" Art "], "siblings are not touched"
    held = {h["path"]: h["why"] for h in result["held"]}
    assert "the full tidy would delete it" in held["Art - Alb (2001) [FLAC]/01 - One.mp3"]
    assert "missing ARTIST, ALBUM or TITLE" in held["noalbum.flac"]
    assert "not found" in held["missing.flac"] and held["/elsewhere/x.flac"] == "outside the library"
    assert (dl / "01 - One.mp3").is_file() and result["deletions_waiting"] == 1, "never deletes"
    assert (library / "noalbum.flac").is_file()
    assert result["extras_filed"] == 0 and result["pruned_folders"] == 0 and (dl / "cover.jpg").is_file(), "audio still in the folder"
    assert result["reports_filed"] == 1 and not (library / "Download Report 2.txt").exists()
    assert "# apply-new" in (library / ".tidy" / "tag_changes.log").read_text()
    assert Path(result["backup_path"]).name.endswith("_new.json")

    # the mp3 gone (what the full tidy would do) and one more track arrives: the cover follows, the folder is pruned
    (dl / "01 - One.mp3").unlink()
    make_flac(dl / "03 - Three.flac", artist="Art", album="Alb", title="Three", tracknumber="3")
    result = Tidy(library).apply_new([dl / "03 - Three.flac"])
    assert result["moved"] == 1 and result["extras_filed"] == 1 and result["pruned_folders"] == 1
    assert (library / "Art" / "Alb" / "cover.jpg").is_file() and not dl.exists()

    # a new file whose target is taken stays put
    make_flac(library / "dup.flac", artist="Art", album="Alb", title="Two", tracknumber="2")
    result = Tidy(library).apply_new([library / "dup.flac"])
    assert result["moved"] == 0 and "clash" in result["held"][0]["why"] and (library / "dup.flac").is_file()
