"""Album covers: sources through a fake web (Cover Art Archive by release and release group, Deezer, iTunes), the
cover file beside the music, the picture embedded in each track, the ledger, and the push into a Flaclify-shaped
cache keyed by folder URI. No network."""

import json
import sqlite3
import urllib.parse

import pytest

from mutagen.flac import FLAC, Picture

from flacli import avatar, cover, server, wiki

from test_avatar import PictureBytes, jpg, png                     # noqa: F401 - fixture reuse
from test_wiki import FakeWeb, home, run                           # noqa: F401 - fixture reuse


class CoverWeb(FakeWeb):
    """The wiki fake plus Deezer's album search (World Coming Down only) and an iTunes search that finds nothing."""

    def __call__(self, url, user_agent):
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)

        if parsed.netloc == "api.deezer.com":
            self.urls.append(url)
            assert parsed.path == "/search/album"

            if 'album:"World Coming Down"' in params["q"][0]:
                return {"data": [
                    {"title": "World Coming Down (Live)", "artist": {"name": "Type O Negative"}, "cover_xl": "https://cdn.deezer/cover/live/1000x1000.jpg"},
                    {"title": "World Coming Down", "artist": {"name": "Type O Negative"}, "link": "https://www.deezer.com/album/9",
                     "cover_xl": "https://cdn.deezer/cover/wcd/1000x1000.jpg"},
                ]}

            return {"data": []}

        if parsed.netloc == "itunes.apple.com":
            self.urls.append(url)
            return {"results": [{"collectionName": "Something Else", "artistName": "Type O Negative", "artworkUrl100": "https://x/100x100bb.jpg"}]}

        return super().__call__(url, user_agent)


class CoverBytes(PictureBytes):
    def __init__(self):
        super().__init__()
        self.files.update({
            "https://coverartarchive.org/release/rel-gt/front-1200": jpg(1200, 1200),
            "https://cdn.deezer/cover/wcd/1000x1000.jpg": png(1000, 1000),
            "https://example.org/small.png": png(250, 250),
        })

    def __call__(self, url, user_agent):
        if url not in self.files:
            self.urls.append(url)
            raise wiki.WikiError(f"HTTP 404 for {url}")

        return super().__call__(url, user_agent)


@pytest.fixture
def covers(home, monkeypatch):
    player = sqlite3.connect(str(home / "player.sqlite"))
    player.executescript("""
        CREATE TABLE `images` (`key` VARCHAR not null, `is_thumbnail` INTEGER not null, `filename` VARCHAR not null,
            `last_modified` DATETIME not null, primary key (`key`, `is_thumbnail`));
        INSERT INTO images VALUES ('The Cardigans/Gran Turismo/', 0, '', '2026-01-01T00:00:00Z');
        INSERT INTO images VALUES ('The Cardigans/Gran Turismo/', 1, '', '2026-01-01T00:00:00Z');
    """)
    player.commit()
    player.close()
    web, files = CoverWeb(), CoverBytes()
    server.State.mb_fetch = web
    server.State.mb_sleep = lambda seconds: None
    server.State.fetch_bytes = files
    monkeypatch.setattr(avatar, "player_settings", lambda target: dict(avatar.DEFAULT_SETTINGS))
    monkeypatch.setitem(wiki.TARGETS, "flaclify", str(home / "not-there" / "metadata.sqlite"))   # never the real player
    yield home, web, files
    server.State.fetch_bytes = None


def cache_rows(home):
    conn = sqlite3.connect(str(home / "player.sqlite"))
    rows = conn.execute("SELECT key, is_thumbnail, filename FROM images ORDER BY key, is_thumbnail").fetchall()
    conn.close()
    return rows


def pictures_in(path) -> list[Picture]:
    return FLAC(str(path)).pictures


def test_missing_then_fill_from_coverart_and_deezer(covers):
    home, web, files = covers
    music = home / "Music"

    code, result = run("cover", "missing")
    assert code == 0 and result["albums"] == 2 and result["missing"] == 2 and result["tracks_without_picture"] == 4
    assert [(e["artist"], e["album"], e["cover"], e["tracks_without_picture"]) for e in result["entries"]] == [
        ("The Cardigans", "Gran Turismo", None, 3), ("Type O Negative", "World Coming Down", None, 1)]

    code, result = run("cover", "fill")
    assert code == 0 and result["not_found"] == [] and result["errors"] == [] and result["remaining"] == 0
    gt, wcd = result["filled"]

    # Gran Turismo: tagged release -> the Cover Art Archive front, embedded in all three tracks, memo cleared
    assert gt["source"] == "coverart" and gt["cover"] == "The Cardigans/Gran Turismo/cover.jpg" and gt["size"] == "1200x1200"
    assert gt["page"] == "https://musicbrainz.org/release/rel-gt"
    assert gt["tracks"]["embedded"] == ["01 - Paper Cup.flac", "02 - Rise & Shine.flac", "03 - Lovefool.flac"]
    assert gt["targets"] == {str(home / "player.sqlite"): "written, the player's failed-lookup memo cleared"}
    assert (music / "The Cardigans" / "Gran Turismo" / "cover.jpg").read_bytes() == files.files["https://coverartarchive.org/release/rel-gt/front-1200"]
    [picture] = pictures_in(music / "The Cardigans" / "Gran Turismo" / "03 - Lovefool.flac")
    assert picture.type == 3 and picture.mime == "image/jpeg" and picture.width == 1200

    # World Coming Down: no release id -> release group found by search -> no CAA art (404) -> Deezer, exact title
    assert wcd["source"] == "deezer" and wcd["cover"] == "1999 - World Coming Down/cover.png" and wcd["page"] == "https://www.deezer.com/album/9"
    assert "https://coverartarchive.org/release-group/rg-wcd/front-1200" in files.urls
    assert wcd["tracks"]["embedded"] == ["01 - Skip It.flac"]

    rows = cache_rows(home)
    assert [r[0] for r in rows] == ["1999 - World Coming Down/"] * 2 + ["The Cardigans/Gran Turismo/"] * 2
    assert all(r[2].endswith(".webp") and (home / "images" / r[2]).is_file() for r in rows)

    ledger = json.loads((music / ".wiki" / "covers.json").read_text())
    assert ledger["The Cardigans/Gran Turismo"]["source"] == "coverart" and ledger["1999 - World Coming Down"]["url"].endswith("wcd/1000x1000.jpg")

    code, result = run("cover", "missing")
    assert code == 0 and result["entries"] == [] and result["with_cover"] == 2 and result["tracks_without_picture"] == 0

    # A second fill has nothing to do and asks nobody
    before = len(web.urls) + len(files.urls)
    code, result = run("cover", "fill")
    assert code == 0 and result["filled"] == [] and len(web.urls) + len(files.urls) == before


def test_fill_reports_what_it_tried_and_honours_the_provider_list(covers):
    home, web, files = covers

    code, result = run("cover", "fill", "--providers", "itunes,bogus")
    assert code == 1 and "unknown cover provider bogus" in result["error"]

    code, result = run("cover", "fill", "--providers", "itunes", "--limit", "1")
    assert code == 0 and result["filled"] == [] and result["remaining"] == 1
    [miss] = result["not_found"]
    assert (miss["artist"], miss["album"], miss["tried"]) == ("The Cardigans", "Gran Turismo", ["itunes: nothing"])
    assert any("itunes.apple.com" in url and "Gran+Turismo" in url for url in web.urls)
    assert cache_rows(home)[0][2] == ""       # the memo stays until a cover is found

    # --no-embed writes the file only
    code, result = run("cover", "fill", "--providers", "coverart", "--no-embed")
    assert code == 0 and [f["album"] for f in result["filled"]] == ["Gran Turismo"] and "tracks" not in result["filled"][0]
    assert pictures_in(home / "Music" / "The Cardigans" / "Gran Turismo" / "01 - Paper Cup.flac") == []
    code, result = run("cover", "missing")
    assert [(e["album"], e["cover"] is not None, e["tracks_without_picture"]) for e in result["entries"]] == [
        ("Gran Turismo", True, 3), ("World Coming Down", False, 1)]


def test_local_pictures_come_first_and_are_never_replaced(covers):
    home, web, files = covers
    folder = home / "Music" / "The Cardigans" / "Gran Turismo"
    embedded = Picture()
    embedded.type, embedded.mime, embedded.data = 3, "image/png", png(600, 600, (10, 200, 10))
    audio = FLAC(str(folder / "02 - Rise & Shine.flac"))
    audio.add_picture(embedded)
    audio.save()
    (home / "Music" / "1999 - World Coming Down" / "folder.jpg").write_bytes(jpg(800, 800))

    code, result = run("cover", "fill", "--providers", "local")
    assert code == 0 and [f["source"] for f in result["filled"]] == ["local", "local"]
    gt, wcd = result["filled"]
    assert gt["page"] == "embedded in The Cardigans/Gran Turismo/02 - Rise & Shine.flac" and gt["cover"].endswith("cover.png")
    assert gt["tracks"] == {"embedded": ["01 - Paper Cup.flac", "03 - Lovefool.flac"], "already_had_one": 1, "unsupported": []}
    assert (folder / "cover.png").read_bytes() == embedded.data
    [kept] = pictures_in(folder / "02 - Rise & Shine.flac")
    assert kept.data == embedded.data
    assert wcd["page"] == "1999 - World Coming Down/folder.jpg" and (home / "Music" / "1999 - World Coming Down" / "cover.jpg").is_file()
    assert web.urls == [] and files.urls == []


def test_set_replaces_and_push_repeats(covers):
    home, web, files = covers
    folder = home / "Music" / "The Cardigans" / "Gran Turismo"
    run("cover", "fill", "--providers", "coverart")
    assert (folder / "cover.jpg").is_file()

    code, result = run("cover", "set", "The Cardigans", "Gran Turismo", "https://example.org/small.png")
    assert code == 1 and "too small" in result["error"]

    own = home / "own.png"
    own.write_bytes(png(1500, 1500))
    code, result = run("cover", "set", "The Cardigans", "Gran Turismo", str(own), "--attribution", "scanned by me")
    assert code == 0 and result["source"] == "manual" and result["cover"] == "The Cardigans/Gran Turismo/cover.png"
    assert not (folder / "cover.jpg").exists() and (folder / "cover.png").read_bytes() == own.read_bytes()
    assert result["tracks"]["already_had_one"] == 3        # the first fill embedded; set never replaces a picture
    assert result["targets"] == {str(home / "player.sqlite"): "replaced"}
    ledger = json.loads((home / "Music" / ".wiki" / "covers.json").read_text())
    assert ledger["The Cardigans/Gran Turismo"]["author"] == "scanned by me"

    code, result = run("cover", "set", "The Cardigans", "Nope", str(own))
    assert code == 1 and "not in the library index" in result["error"]

    before = cache_rows(home)
    code, result = run("cover", "push")
    assert code == 0 and [p["album"] for p in result["pushed"]] == ["Gran Turismo"]
    assert result["skipped"] == [{"artist": "Type O Negative", "album": "World Coming Down", "reason": "no cover file beside the music"}]
    after = cache_rows(home)
    assert [r[:2] for r in after] == [r[:2] for r in before] and [r[2] for r in after] != [r[2] for r in before]
    assert not any((home / "images" / r[2]).exists() for r in before)

    code, result = run("cover", "push", "The Cardigans")
    assert code == 0 and len(result["pushed"]) == 1
    code, result = run("cover", "push", "Nobody")
    assert code == 1 and "not in the library index" in result["error"]


def test_cover_targets_add_flaclify_only_when_its_cache_exists(home, monkeypatch):
    fake = home / "flaclify" / "metadata.sqlite"
    monkeypatch.setitem(wiki.TARGETS, "flaclify", str(fake))
    assert cover.cover_targets("") == []
    fake.parent.mkdir()
    fake.write_bytes(b"")
    assert cover.cover_targets("") == [("flaclify", fake)]
    assert cover.cover_targets(f"euphonica,{fake}") == [("euphonica", wiki.Path(wiki.TARGETS["euphonica"]).expanduser()), (str(fake), fake)]
