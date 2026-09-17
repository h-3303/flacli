"""Bios and wikis: the BSON codec, the sidecar files, sources through a fake fetch, and the push into a
Flaclify-shaped metadata.sqlite. No network."""

import json
import sqlite3
import urllib.parse

from pathlib import Path

import pytest

from flacli import bsonlite, cli, config, server, wiki
from flacli.db import Database

from libtools import make_flac

# The albums / artists tables exactly as Flaclify 0.99 creates them (metadata.sqlite, user_version 5)
PLAYER_SCHEMA = """
CREATE TABLE `albums` (`folder_uri` VARCHAR not null, `mbid` VARCHAR null unique, `title` VARCHAR not null,
    `artist` VARCHAR null, `last_modified` DATETIME not null, `data` BLOB not null);
CREATE UNIQUE INDEX `album_name` on `albums` (`title`, `artist`);
CREATE TABLE `artists` (`name` VARCHAR not null unique, `mbid` VARCHAR null unique, `last_modified` DATETIME not null,
    `data` BLOB not null, primary key (`name`));
PRAGMA user_version = 5;
"""

def _fake_flaclify_artist_blob() -> bytes:
    """The document Flaclify writes for an artist it found nothing about (field order as serde emits it)."""
    return bsonlite.encode({"name": "Sunny Day Real Estate", "tags": [], "mbid": "86b24e8f-a4d9-4c84-83ee-fde0d14ad9fa",
                            "similar": [], "image": [], "artist_type": "Other"})


def test_bson_round_trip_matches_serde_layout():
    blob = _fake_flaclify_artist_blob()
    # length prefix, string element for name, arrays as documents with "0".."n" keys, trailing NUL
    assert blob[:4] == (len(blob)).to_bytes(4, "little") and blob[4] == 0x02 and blob[-1] == 0
    assert bsonlite.decode(blob) == {"name": "Sunny Day Real Estate", "tags": [], "mbid": "86b24e8f-a4d9-4c84-83ee-fde0d14ad9fa",
                                     "similar": [], "image": [], "artist_type": "Other"}

    document = {"name": "x", "artist": None, "tags": [{"name": "rock", "count": 12, "set_by_user": False, "url": None}],
                "image": [{"size": "Large", "#text": "http://img"}], "wiki": {"content": "t", "attribution": "a", "url": "u"},
                "big": 2**40, "ratio": 0.5, "flag": True}
    assert bsonlite.decode(bsonlite.encode(document)) == document

    with pytest.raises(ValueError):
        bsonlite.decode(blob[:-1])


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FLACLI_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("FLACLI_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("FLACLI_MUSIC_DIR", str(tmp_path / "Music"))
    monkeypatch.setenv("NICOTINE_MCP_SOCKET", str(tmp_path / "nowhere.sock"))
    monkeypatch.setenv("FLACLI_WIKI_TARGETS", str(tmp_path / "player.sqlite"))
    monkeypatch.delenv("FLACLI_CONTACT", raising=False)
    music = tmp_path / "Music"
    # A conventionally filed album with MusicBrainz ids and an albumartist tag ...
    for n, title in enumerate(["Paper Cup", "Rise & Shine", "Lovefool"], start=1):
        make_flac(music / "The Cardigans" / "Gran Turismo" / f"0{n} - {title}.flac", artist="The Cardigans",
                  albumartist="The Cardigans", album="Gran Turismo", title=title, date="1998",
                  musicbrainz_albumid="rel-gt", musicbrainz_albumartistid="art-card", tracknumber=str(n))
    # ... and a loosely filed one with no ids and no albumartist
    make_flac(music / "1999 - World Coming Down" / "01 - Skip It.flac", artist="Type O Negative", album="World Coming Down",
              title="Skip It")
    player = sqlite3.connect(str(tmp_path / "player.sqlite"))
    player.executescript(PLAYER_SCHEMA)
    player.execute("INSERT INTO artists VALUES (?, ?, ?, ?)", ("The Cardigans", "art-card", "2026-01-01T00:00:00Z",
                   bsonlite.encode({"name": "The Cardigans", "tags": [{"name": "pop", "count": 3, "set_by_user": False, "url": None}],
                                    "mbid": "art-card", "similar": [], "image": [], "artist_type": "Group"})))
    player.commit()
    player.close()
    yield tmp_path
    server.State.mb_fetch = None
    server.State.mb_sleep = None


def run(*argv):
    import io
    from contextlib import redirect_stdout

    buffer = io.StringIO()

    with redirect_stdout(buffer):
        code = cli.main(list(argv))

    text = buffer.getvalue()
    return code, (json.loads(text) if text.lstrip().startswith("{") else text)


class FakeWeb:
    """fetch(url, user_agent): MusicBrainz, Wikidata and Wikipedia answers from tables."""

    def __init__(self):
        self.urls = []

    def __call__(self, url, user_agent):
        assert user_agent.startswith("flacli/")
        self.urls.append(url)
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)

        if parsed.netloc == "musicbrainz.org":
            path = parsed.path.removeprefix("/ws/2/")

            if path == "release/rel-gt":
                return {"id": "rel-gt", "date": "1998-10-19", "country": "SE", "release-group": {"id": "rg-gt"},
                        "label-info": [{"label": {"name": "Stockholm Records"}}]}
            if path == "release-group/rg-gt":
                return {"id": "rg-gt", "title": "Gran Turismo", "primary-type": "Album", "first-release-date": "1998-10-19",
                        "artist-credit": [{"name": "The Cardigans", "joinphrase": ""}], "tags": [{"name": "pop", "count": 4}],
                        "relations": [{"type": "wikidata", "url": {"resource": "https://www.wikidata.org/wiki/Q1140440"}},
                                      {"type": "discogs", "url": {"resource": "https://www.discogs.com/master/29927"}}]}
            if path == "artist/art-card":
                return {"id": "art-card", "name": "The Cardigans", "type": "Group", "country": "SE", "area": {"name": "Sweden"},
                        "begin-area": {"name": "Jönköping"}, "life-span": {"begin": "1992", "ended": False},
                        "tags": [{"name": "pop", "count": 9}, {"name": "indie pop", "count": 4}],
                        "relations": [{"type": "official homepage", "url": {"resource": "https://cardigans.com"}}]}
            if path == "artist":
                assert "Type O Negative" in params["query"][0]
                return {"artists": [{"id": "art-ton", "name": "Type O Negative", "score": 100}]}
            if path == "artist/art-ton":
                return {"id": "art-ton", "name": "Type O Negative", "type": "Group", "country": "US", "life-span": {"begin": "1989", "end": "2010", "ended": True},
                        "annotation": "Brooklyn. Peter Steele.", "relations": []}
            if path == "release-group":
                return {"release-groups": [{"id": "rg-wcd", "title": "World Coming Down", "score": 100}]}
            if path == "release-group/rg-wcd":
                return {"id": "rg-wcd", "title": "World Coming Down", "primary-type": "Album", "first-release-date": "1999-09-21",
                        "artist-credit": [{"name": "Type O Negative", "joinphrase": ""}], "relations": []}
            raise AssertionError(f"unexpected MusicBrainz url {url}")

        if parsed.netloc == "www.wikidata.org":
            assert parsed.path.endswith("/Q1140440.json")
            return {"entities": {"Q1140440": {"sitelinks": {"enwiki": {"title": "Gran Turismo (album)"}}}}}

        if parsed.netloc == "en.wikipedia.org":
            assert params["titles"] == ["Gran Turismo (album)"] and params["explaintext"] == ["1"]
            return {"query": {"pages": [{"title": "Gran Turismo (album)",
                                         "extract": "Gran Turismo is the fourth studio album by Swedish band the Cardigans, released on 19 October 1998 "
                                                    "by Stockholm Records. It marked a darker, more electronic turn from the band's earlier pop."}]}}

        raise AssertionError(f"unexpected url {url}")


def test_missing_lists_artists_then_albums_with_sidecar_paths(home):
    code, result = run("wiki", "missing")
    assert code == 0 and result["missing"] == 4 and result["with_text"] == 0
    kinds = [(e["kind"], e.get("name") or e["title"]) for e in result["entries"]]
    assert kinds == [("artist", "The Cardigans"), ("artist", "Type O Negative"), ("album", "Gran Turismo"), ("album", "World Coming Down")]
    cardigans, ton, gt, wcd = result["entries"]
    assert cardigans["sidecar"] == "The Cardigans/artist.md" and cardigans["mbid"] == "art-card"
    assert ton["sidecar"] == ".wiki/Type O Negative.md" and ton["mbid"] is None
    assert gt["sidecar"] == "The Cardigans/Gran Turismo/wiki.md" and gt["year"] == "1998" and gt["tracks"][2] == "Lovefool"
    assert gt["mbid"] == "rel-gt" and gt["albumartist"] == "The Cardigans" and gt["formats"] == ["flac"]
    assert wcd["sidecar"] == "1999 - World Coming Down/wiki.md" and wcd["artist"] == "Type O Negative" and wcd["albumartist"] is None


def test_fill_takes_wikipedia_and_briefs_the_rest(home):
    web = FakeWeb()
    server.State.mb_fetch = web
    server.State.mb_sleep = lambda s: None

    code, result = run("wiki", "fill", "--limit", "10")
    assert code == 0 and result["errors"] == [] and result["remaining"] == 0
    assert [f["entry"] for f in result["filled"]] == ["The Cardigans - Gran Turismo"]
    assert result["filled"][0]["url"] == "https://en.wikipedia.org/wiki/Gran_Turismo_(album)"
    assert [b.get("name") or b["title"] for b in result["to_write"]] == ["The Cardigans", "Type O Negative", "World Coming Down"]

    cardigans, ton, wcd = result["to_write"]
    assert cardigans["musicbrainz"]["begin_area"] == "Jönköping" and cardigans["links"] == [{"type": "official homepage", "url": "https://cardigans.com"}]
    assert ton["musicbrainz"]["annotation"] == "Brooklyn. Peter Steele." and ton["musicbrainz"]["end"] == "2010"
    assert wcd["musicbrainz"]["first_released"] == "1999-09-21" and wcd["musicbrainz"]["artist_credit"] == "Type O Negative"

    # the sidecar: front matter + text
    sidecar = home / "Music" / "The Cardigans" / "Gran Turismo" / "wiki.md"
    text = sidecar.read_text()
    assert text.startswith("---\nkind: album\nartist: The Cardigans\ntitle: Gran Turismo\nmbid: rel-gt\n")
    assert "attribution: Wikipedia contributors, CC BY-SA 4.0" in text and text.rstrip().endswith("earlier pop.")

    # the player's cache: a new album row keyed the way Flaclify keys it, BSON with a wiki
    player = sqlite3.connect(str(home / "player.sqlite"))
    row = player.execute("SELECT folder_uri, mbid, title, artist, last_modified, data FROM albums").fetchone()
    assert row[:4] == ("The Cardigans/Gran Turismo/", "rel-gt", "Gran Turismo", "The Cardigans") and row[4].endswith("Z")
    document = bsonlite.decode(row[5])
    assert document["name"] == "Gran Turismo" and document["tags"] == [] and document["wiki"]["attribution"] == "Wikipedia contributors, CC BY-SA 4.0"
    assert document["wiki"]["content"].startswith("Gran Turismo is the fourth studio album")
    player.close()

    # second run: nothing left to fill, answers come from the cache (no new requests for the album)
    before = len(web.urls)
    code, again = run("wiki", "fill")
    assert code == 0 and again["filled"] == [] and len(again["to_write"]) == 3 and len(web.urls) == before

    code, missing = run("wiki", "missing")
    assert missing["missing"] == 3 and missing["with_text"] == 1


def test_set_writes_sidecar_and_updates_existing_player_row(home):
    code, error = run("wiki", "set", "The Cardigans", "--text", "Swedish band formed in Jönköping in 1992.")
    assert code == 1 and "attribution is required" in error["error"]

    code, result = run("wiki", "set", "The Cardigans", "--text", "Swedish band formed in Jönköping in 1992.\n\nSix albums.",
                       "--attribution", "Written by a test from MusicBrainz", "--url", "https://cardigans.com")
    assert code == 0 and result["replaced"] is False and result["targets"] == {str(home / "player.sqlite"): "updated"}
    assert (home / "Music" / "The Cardigans" / "artist.md").read_text().endswith("1992.\n\nSix albums.\n")

    player = sqlite3.connect(str(home / "player.sqlite"))
    (blob,) = player.execute("SELECT data FROM artists WHERE name = 'The Cardigans'").fetchone()
    document = bsonlite.decode(blob)
    assert document["tags"][0]["name"] == "pop" and document["artist_type"] == "Group"    # untouched fields survive
    assert document["bio"] == {"content": "Swedish band formed in Jönköping in 1992.\n\nSix albums.", "attribution": "Written by a test from MusicBrainz",
                               "url": "https://cardigans.com"}
    player.close()

    code, error = run("wiki", "set", "The Cardigans", "--text", "x", "--attribution", "y")
    assert code == 1 and "--force" in error["error"]

    code, error = run("wiki", "set", "Nobody", "--text", "x", "--attribution", "y")
    assert code == 1 and "not in the library index" in error["error"]

    # an album the player cannot key (no albumartist tag, no release id) is written as a sidecar but not pushed
    code, result = run("wiki", "set", "Type O Negative", "World Coming Down", "--text", "Fifth album, 1999.", "--attribution", "test")
    assert code == 0 and result["targets"][str(home / "player.sqlite")].startswith("skipped: the player keys albums")
    assert (home / "Music" / "1999 - World Coming Down" / "wiki.md").exists()


def test_set_json_batch_and_push(home, tmp_path):
    batch = tmp_path / "batch.json"
    batch.write_text(json.dumps([
        {"artist": "Type O Negative", "content": "Brooklyn gothic metal band, 1989 to 2010.", "attribution": "test"},
        {"artist": "The Cardigans", "album": "Gran Turismo", "content": "Fourth album, 1998.", "attribution": "test", "url": "https://x"},
        {"artist": "Nobody", "content": "x", "attribution": "test"},
        {"artist": "The Cardigans", "content": "no attribution"},
    ]))
    code, result = run("wiki", "set", "--json", str(batch))
    assert code == 0 and [w["entry"] for w in result["written"]] == ["Type O Negative", "The Cardigans - Gran Turismo"]
    assert [e["entry"] for e in result["errors"]] == ["Nobody", "The Cardigans"]
    assert "attribution is required" in result["errors"][1]["error"]

    # wipe the player cache, push everything again
    (home / "player.sqlite").unlink()
    player = sqlite3.connect(str(home / "player.sqlite"))
    player.executescript(PLAYER_SCHEMA)
    player.close()
    code, pushed = run("wiki", "push")
    assert code == 0 and [p["entry"] for p in pushed["pushed"]] == ["Type O Negative", "The Cardigans - Gran Turismo"]
    assert all(list(p["targets"].values()) == ["inserted"] for p in pushed["pushed"])
    assert [s["entry"] for s in pushed["skipped"]] == ["The Cardigans", "Type O Negative - World Coming Down"]

    player = sqlite3.connect(str(home / "player.sqlite"))
    assert player.execute("SELECT COUNT(*) FROM artists").fetchone()[0] == 1 and player.execute("SELECT COUNT(*) FROM albums").fetchone()[0] == 1
    player.close()

    # a target that is not there yet is reported, not created
    code, one = run("wiki", "push", "Type O Negative")
    assert code == 0 and one["pushed"][0]["targets"] == {str(home / "player.sqlite"): "updated"}
    assert wiki.target_paths("flaclify, euphonica") == [("flaclify", Path("~/.cache/flaclify/metadata.sqlite").expanduser()),
                                                          ("euphonica", Path("~/.cache/euphonica/metadata.sqlite").expanduser())]
    assert wiki.push_entry(tmp_path / "absent.sqlite", {"kind": "artist", "name": "x"}, {"meta": {}, "content": "t"}).startswith("no database")


def test_sources_command_and_index_migration(home):
    server.State.mb_fetch = FakeWeb()
    server.State.mb_sleep = lambda s: None
    run("scan")

    code, result = run("wiki", "sources", "The Cardigans", "Gran Turismo")
    assert code == 0 and result["release_group"] == "rg-gt" and result["musicbrainz"]["labels"] == ["Stockholm Records"]
    assert result["wikipedia"]["title"] == "Gran Turismo (album)" and result["links"][1]["type"] == "discogs"

    code, result = run("wiki", "sources", "Type O Negative")
    assert code == 0 and result["mbid"] == "art-ton" and result["wikipedia"] is None and result["musicbrainz"]["type"] == "Group"

    db = Database(config.db_path())
    assert db.schema_version == 3
    row = db.conn.execute("SELECT albumartist, mb_artist_id FROM library_files WHERE path LIKE '%Lovefool%'").fetchone()
    assert tuple(row) == ("The Cardigans", "art-card")
    db.close()


def test_fetch_backs_off_on_busy_and_never_caches_a_busy_body(monkeypatch, tmp_path):
    import io
    import urllib.error
    import urllib.request

    from flacli import musicbrainz

    calls = []
    answers = [503, 503, 200]

    def fake_urlopen(request, timeout):
        calls.append(request.full_url)
        code = answers.pop(0)

        if code != 200:
            raise urllib.error.HTTPError(request.full_url, code, "busy", {}, io.BytesIO(b""))

        class Response(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *args): return False

        return Response(b'{"id": "x"}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    pauses = []
    assert musicbrainz.default_fetch("https://musicbrainz.org/ws/2/artist/x?fmt=json", "flacli/test", sleep=pauses.append) == {"id": "x"}
    assert pauses == [2, 4] and len(calls) == 3

    answers[:] = [503, 503, 503, 503]
    with pytest.raises(musicbrainz.MusicBrainzError, match="HTTP 503 .* after 3 retries"):
        musicbrainz.default_fetch("https://musicbrainz.org/ws/2/artist/y?fmt=json", "flacli/test", sleep=pauses.append)

    answers[:] = [503, 200]
    pauses.clear()
    assert wiki.default_fetch("https://en.wikipedia.org/w/api.php?x", "flacli/test", sleep=pauses.append) == {"id": "x"}
    assert pauses == [2]

    # a 200 whose body is only an error message is raised, not cached
    db = Database(tmp_path / "state.db")
    client = musicbrainz.MusicBrainzClient(db, "flacli/test", fetch=lambda url, ua: {"error": "The MusicBrainz web server is currently busy."},
                                           sleep=lambda s: None)
    with pytest.raises(musicbrainz.MusicBrainzError, match="currently busy"):
        client.get("artist/z")
    assert db.cache_get("https://musicbrainz.org/ws/2/artist/z?fmt=json") is None
    db.close()


def test_lead_keeps_whole_paragraphs_to_about_500_chars():
    short = "One paragraph only."
    assert wiki.lead(short) == short
    long = "\n\n".join(["a" * 300, "b" * 300, "c" * 300])
    assert wiki.lead(long) == "a" * 300 + "\n\n" + "b" * 300          # crosses 500 after the second paragraph
    assert wiki.lead("x" * 900 + "\n" + "y" * 10) == "x" * 900         # a first paragraph is always kept whole
