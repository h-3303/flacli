"""Artist pictures: sources through a fake web (Wikidata portrait, Commons, MusicBrainz image relation, Deezer),
the sidecar beside the music, the ledger, and the push into a Flaclify-shaped cache (images folder + table).
No network."""

import io
import sqlite3
import urllib.parse

from pathlib import Path

import pytest

from PIL import Image

from flacli import avatar, server

from test_wiki import FakeWeb, home, run   # noqa: F401 - fixture reuse


def png(width, height, colour=(200, 40, 40)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(buffer, format="PNG")
    return buffer.getvalue()


def jpg(width, height) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (20, 90, 160)).save(buffer, format="JPEG")
    return buffer.getvalue()


class PictureWeb(FakeWeb):
    """The wiki fake plus: the Cardigans have a Wikidata item with a portrait; Type O Negative only exist on Deezer."""

    def __call__(self, url, user_agent):
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)

        if parsed.netloc == "musicbrainz.org" and parsed.path.endswith("/artist/art-card"):
            data = super().__call__(url, user_agent)
            data["relations"].append({"type": "wikidata", "url": {"resource": "https://www.wikidata.org/wiki/Q1140440-card"}})
            return data

        if parsed.netloc == "www.wikidata.org" and parsed.path.endswith("/Q1140440-card.json"):
            self.urls.append(url)
            return {"entities": {"Q1140440-card": {"claims": {"P18": [{"mainsnak": {"datavalue": {"value": "The Cardigans 2005.jpg"}}}]}}}}

        if parsed.netloc == "commons.wikimedia.org":
            self.urls.append(url)
            assert params["titles"] == ["File:The Cardigans 2005.jpg"] and params["iiurlwidth"] == ["1200"]
            return {"query": {"pages": [{"title": "File:The Cardigans 2005.jpg", "imageinfo": [{
                "url": "https://upload.wikimedia.org/orig/The_Cardigans_2005.jpg",
                "thumburl": "https://upload.wikimedia.org/thumb/The_Cardigans_2005.jpg/1200px.jpg",
                "extmetadata": {"Artist": {"value": '<a href="x">Bengt Nyman</a>'}, "LicenseShortName": {"value": "CC BY 2.0"}},
            }]}]}}

        if parsed.netloc == "api.deezer.com":
            self.urls.append(url)
            name = params["q"][0]

            if name == "Type O Negative":
                return {"data": [{"name": "Type O Negative Tribute", "picture_xl": "https://cdn.deezer/artist/x/1000x1000.jpg"},
                                 {"name": "Type O Negative", "link": "https://www.deezer.com/artist/1", "picture_xl": "https://cdn.deezer/artist/abc/1000x1000.jpg"}]}

            return {"data": [{"name": name, "picture_xl": "https://cdn.deezer/artist//1000x1000.jpg"}]}   # the placeholder

        return super().__call__(url, user_agent)


class PictureBytes:
    def __init__(self):
        self.urls = []
        self.files = {
            "https://upload.wikimedia.org/thumb/The_Cardigans_2005.jpg/1200px.jpg": jpg(900, 1200),
            "https://cdn.deezer/artist/abc/1000x1000.jpg": png(1000, 1000),
            "https://example.org/tiny.png": png(50, 50),
            "https://example.org/wide.png": png(1600, 400),
        }

    def __call__(self, url, user_agent):
        assert user_agent.startswith("flacli/")
        self.urls.append(url)
        return self.files[url]


@pytest.fixture
def pictures(home, monkeypatch):
    player = sqlite3.connect(str(home / "player.sqlite"))
    player.executescript("""
        CREATE TABLE `images` (`key` VARCHAR not null, `is_thumbnail` INTEGER not null, `filename` VARCHAR not null,
            `last_modified` DATETIME not null, primary key (`key`, `is_thumbnail`));
        INSERT INTO images VALUES ('avatar:The Cardigans', 0, '', '2026-01-01T00:00:00Z');
        INSERT INTO images VALUES ('avatar:The Cardigans', 1, '', '2026-01-01T00:00:00Z');
    """)
    player.commit()
    player.close()
    web, files = PictureWeb(), PictureBytes()
    server.State.mb_fetch = web
    server.State.mb_sleep = lambda seconds: None
    server.State.fetch_bytes = files
    monkeypatch.setattr(avatar, "player_settings", lambda target: dict(avatar.DEFAULT_SETTINGS))
    yield home, web, files
    server.State.fetch_bytes = None


def cache_rows(home):
    conn = sqlite3.connect(str(home / "player.sqlite"))
    rows = conn.execute("SELECT key, is_thumbnail, filename FROM images ORDER BY key, is_thumbnail").fetchall()
    conn.close()
    return rows


def test_inspect_image_rejects_junk_and_icons():
    assert avatar.inspect_image(png(300, 400)) == (".png", 300, 400)
    assert avatar.inspect_image(jpg(640, 480)) == (".jpg", 640, 480)

    with pytest.raises(ValueError, match="too small"):
        avatar.inspect_image(png(50, 50))
    with pytest.raises(ValueError, match="not an image"):
        avatar.inspect_image(b"<html>nope</html>")


def test_missing_then_fill_from_wikidata_and_deezer(pictures):
    home, web, files = pictures
    code, result = run("avatar", "missing")
    assert code == 0 and result["missing"] == 2 and result["with_picture"] == 0
    assert [e["name"] for e in result["entries"]] == ["The Cardigans", "Type O Negative"]

    code, result = run("avatar", "fill")
    assert code == 0 and result["errors"] == [] and result["not_found"] == [] and result["remaining"] == 0
    cardigans, ton = result["filled"]
    assert cardigans["source"] == "wikidata" and cardigans["license"] == "CC BY 2.0" and cardigans["author"] == "Bengt Nyman"
    assert cardigans["picture"] == "The Cardigans/artist.jpg" and cardigans["size"] == "900x1200"
    assert cardigans["page"] == "https://commons.wikimedia.org/wiki/File:The_Cardigans_2005.jpg"
    assert ton["source"] == "deezer" and ton["picture"] == ".wiki/Type O Negative.png" and ton["page"] == "https://www.deezer.com/artist/1"
    assert cardigans["targets"] == {str(home / "player.sqlite"): "written, the player's failed-lookup memo cleared"}
    assert ton["targets"] == {str(home / "player.sqlite"): "written"}
    assert (home / "Music" / "The Cardigans" / "artist.jpg").read_bytes() == files.files["https://upload.wikimedia.org/thumb/The_Cardigans_2005.jpg/1200px.jpg"]

    ledger = avatar.load_ledger(home / "Music")
    assert ledger["The Cardigans"]["url"].endswith("1200px.jpg") and ledger["Type O Negative"]["license"] is None
    assert sorted(ledger) == ["The Cardigans", "Type O Negative"]

    rows = cache_rows(home)
    assert [(k, t) for k, t, _ in rows] == [("avatar:The Cardigans", 0), ("avatar:The Cardigans", 1),
                                            ("avatar:Type O Negative", 0), ("avatar:Type O Negative", 1)]
    images = home / "images"

    for key, thumb, filename in rows:
        assert filename.endswith(".webp") and (images / filename).is_file()
        with Image.open(images / filename) as rendition:
            assert rendition.format == "WEBP"
            if key.endswith("Cardigans"):
                assert rendition.size == ((128, 171) if thumb else (768, 1024))    # portrait: long edge 1024, short edge 128
            else:
                assert rendition.size == ((128, 128) if thumb else (1000, 1000))   # never enlarged

    # nothing left to do; the sources are not asked again
    asked = len(web.urls)
    code, result = run("avatar", "fill")
    assert result["filled"] == [] and result["remaining"] == 0 and len(web.urls) == asked
    code, result = run("avatar", "missing", "--all")
    assert result["missing"] == 0 and [e["source"] for e in result["entries"]] == ["wikidata", "deezer"]


def test_fill_reports_what_it_tried_and_honours_the_provider_list(pictures):
    home, web, files = pictures
    code, result = run("avatar", "fill", "--providers", "local,musicbrainz")
    assert code == 0 and result["filled"] == [] and result["providers"] == ["local", "musicbrainz"]
    assert [n["artist"] for n in result["not_found"]] == ["The Cardigans", "Type O Negative"]
    assert result["not_found"][0]["tried"] == ["local: nothing", "musicbrainz: nothing"]
    assert not any("deezer" in url for url in web.urls)

    code, result = run("avatar", "fill", "--providers", "bogus")
    assert code == 1 and "unknown picture provider" in result["error"]

    # a picture a tagger left in the artist folder comes first, and needs no network
    (home / "Music" / "The Cardigans" / "folder.jpg").write_bytes(jpg(500, 500))
    code, result = run("avatar", "fill", "--providers", "local", "--limit", "1")
    (found,) = result["filled"]
    assert found["source"] == "local" and found["picture"] == "The Cardigans/artist.jpg" and result["remaining"] == 0
    assert result["tried_before"] == 1          # Type O Negative: these providers found nothing last time

    # a miss is remembered, so the sources are not asked again until --retry (or a new mbid, or a folder picture)
    asked = len(web.urls)
    code, result = run("avatar", "fill", "--providers", "local,musicbrainz")
    assert (result["filled"], result["not_found"], result["tried_before"], len(web.urls)) == ([], [], 1, asked)
    code, result = run("avatar", "missing")
    assert [(e["name"], e["tried"]) for e in result["entries"]] == [("Type O Negative", ["local: nothing", "musicbrainz: nothing"])]
    code, result = run("avatar", "fill", "--providers", "local,musicbrainz", "--retry")
    assert [n["artist"] for n in result["not_found"]] == ["Type O Negative"]     # asked again (from the request cache)


def test_set_replaces_and_push_repeats(pictures):
    home, web, files = pictures
    code, result = run("avatar", "set", "The Cardigans", "https://example.org/tiny.png")
    assert code == 1 and "too small" in result["error"]

    code, result = run("avatar", "set", "The Cardigans", "https://example.org/wide.png", "--attribution", "press kit")
    assert code == 0 and result["source"] == "manual" and result["picture"] == "The Cardigans/artist.png"
    assert avatar.load_ledger(home / "Music")["The Cardigans"]["author"] == "press kit"
    first = {(k, t): f for k, t, f in cache_rows(home)}

    local = home / "portrait.jpg"
    local.write_bytes(jpg(700, 900))
    code, result = run("avatar", "set", "The Cardigans", str(local))
    assert code == 0 and result["picture"] == "The Cardigans/artist.jpg" and result["targets"][str(home / "player.sqlite")] == "replaced"
    assert not (home / "Music" / "The Cardigans" / "artist.png").exists(), "the previous format was removed"
    second = {(k, t): f for k, t, f in cache_rows(home)}
    assert set(second) == set(first) and all(second[key] != first[key] for key in first)
    assert not any((home / "images" / name).exists() for name in first.values()), "old renditions are deleted"
    assert all((home / "images" / name).exists() for name in second.values())

    code, result = run("avatar", "set", "Nobody", str(local))
    assert code == 1 and "not in the library index" in result["error"]

    code, result = run("avatar", "push")
    assert code == 0 and [p["artist"] for p in result["pushed"]] == ["The Cardigans"]
    assert result["skipped"] == [{"artist": "Type O Negative", "reason": "no picture beside the music"}]
    code, result = run("avatar", "push", "The Cardigans")
    assert code == 0 and result["pushed"][0]["targets"][str(home / "player.sqlite")] == "replaced"


def test_push_into_a_player_without_the_table_or_database(tmp_path):
    entry = {"name": "X", "sidecar": "X/artist.md", "albums": [], "mbid": None}
    (tmp_path / "X").mkdir()
    (tmp_path / "X" / "artist.jpg").write_bytes(jpg(400, 400))
    result = avatar.push(tmp_path, [entry], [("gone", tmp_path / "nowhere" / "metadata.sqlite")])
    assert result["pushed"][0]["targets"] == {"gone": "no database (the player has not run yet)"}

    other = tmp_path / "other.sqlite"
    sqlite3.connect(str(other)).execute("CREATE TABLE t (x)").connection.close()
    assert avatar.push_avatar(other, "X", tmp_path / "X" / "artist.jpg") == "not a Flaclify/Euphonica metadata database (no images table)"


def test_picture_download_retries_a_rate_limit(monkeypatch):
    import urllib.error
    import urllib.request

    attempts, pauses = [], []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b"bytes"

    def urlopen(request, timeout):
        attempts.append(request.full_url)

        if len(attempts) < 3:
            raise urllib.error.HTTPError(request.full_url, 429, "Too Many Requests", {}, None)

        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert avatar.default_fetch_bytes("https://x/y.jpg", "flacli/test", sleep=pauses.append) == b"bytes"
    assert len(attempts) == 3 and pauses == [2, 4]

    attempts.clear()
    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout: (_ for _ in ()).throw(
        urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)))

    with pytest.raises(avatar.WikiError, match="HTTP 404"):
        avatar.default_fetch_bytes("https://x/z.jpg", "flacli/test", sleep=pauses.append)

    assert avatar._strip_html('<a href="x">Someone</a>\n\n(Original text: Someone (talk))') == "Someone (Original text: Someone (talk))"


def test_player_settings_fall_back_to_the_schema_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))   # no gsettings binary
    assert avatar.player_settings(tmp_path / "flaclify" / "metadata.sqlite") == avatar.DEFAULT_SETTINGS
    assert avatar.player_settings(tmp_path / "elsewhere" / "metadata.sqlite") == avatar.DEFAULT_SETTINGS
