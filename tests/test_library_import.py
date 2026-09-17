"""Importers, JSPF round trip, M3U writer and text normalisation."""

import json

from pathlib import Path

import pytest

from flacli import textnorm
from flacli.importers import detect_format, import_file
from flacli.jspf import track_from_jspf, track_to_jspf, write_jspf
from flacli.m3u import write_m3u
from flacli.models import Track

FIXTURES = Path(__file__).parent / "fixtures"


def test_spotify_export_imports_every_playlist():
    playlists = import_file(FIXTURES / "Playlist1.json")
    assert [p.name for p in playlists] == ["Road Trip", "Second List"]
    road = playlists[0]
    assert road.source == "spotify-export"
    assert road.has_durations is False
    assert len(road.tracks) == 7
    assert road.tracks[0].title == "Song One" and road.tracks[0].artist == "Artist A"
    assert road.tracks[0].album == "Album A" and road.tracks[0].source_uri == "spotify:track:aaa1"
    assert [t.position for t in road.tracks] == list(range(7))
    assert road.warnings == ["position 8: skipped episode"]


def test_spotify_export_pick_by_name():
    (only,) = import_file(FIXTURES / "Playlist1.json", playlist_name="Second List")
    assert only.name == "Second List" and len(only.tracks) == 1

    with pytest.raises(LookupError, match="no playlist named"):
        import_file(FIXTURES / "Playlist1.json", playlist_name="Nope")


def test_spotify_your_library(tmp_path):
    path = tmp_path / "YourLibrary.json"
    path.write_text(json.dumps({"tracks": [{"artist": "A", "album": "B", "track": "C", "uri": "spotify:track:1"},
                                           {"artist": "A", "album": "B", "track": "", "uri": "x"}]}))
    (liked,) = import_file(path)
    assert liked.name == "Liked Songs" and len(liked.tracks) == 1 and liked.tracks[0].title == "C"
    assert liked.warnings


def test_exportify_csv():
    (playlist,) = import_file(FIXTURES / "exportify.csv")
    assert playlist.source == "exportify"
    assert playlist.name == "exportify"
    assert playlist.has_durations is True
    one, lonely = playlist.tracks
    assert (one.title, one.artist, one.album, one.duration_ms, one.isrc) == ("Song One", "Artist A", "Album A", 200000, "USAAA0100001")
    assert lonely.artist == "Solo C, Guest" and lonely.duration_ms == 185000


def test_generic_csv_with_synonyms_and_mapping(tmp_path):
    (playlist,) = import_file(FIXTURES / "generic.csv", column_mapping={"title": "Song", "artist": "Band", "album": "Record"})
    assert playlist.source == "csv"
    assert [t.title for t in playlist.tracks] == ["Song One", "Track 1"]
    assert playlist.tracks[0].duration_ms == 200000      # 3:20
    assert playlist.tracks[1].duration_ms == 201000      # 201 seconds
    assert playlist.warnings == ["line 3: skipped row without a title"]

    bad = tmp_path / "bad.csv"
    bad.write_text("Foo,Bar\n1,2\n")

    with pytest.raises(ValueError, match="cannot find a title column"):
        import_file(bad)


def test_m3u_import_keeps_existing_local_files(tmp_path):
    folder = tmp_path / "Artist A" / "Album A"
    folder.mkdir(parents=True)
    (folder / "01 - Song One.flac").write_bytes(b"x")
    m3u = tmp_path / "list.m3u8"
    m3u.write_text((FIXTURES / "list.m3u8").read_text())

    (playlist,) = import_file(m3u)
    assert playlist.name == "From M3U" and playlist.source == "m3u"
    one, lonely, bare = playlist.tracks
    assert one.local_path == str((folder / "01 - Song One.flac").resolve())
    assert one.duration_ms == 200000 and one.artist == "Artist A"
    assert lonely.local_path is None and lonely.duration_ms is None
    assert bare.title == "no-extinf" and playlist.warnings


def test_xspf_and_jspf_round_trip(tmp_path):
    (playlist,) = import_file(FIXTURES / "list.xspf")
    assert playlist.name == "From XSPF" and playlist.source == "xspf"
    assert len(playlist.tracks) == 2
    assert playlist.tracks[0].mb_recording_id == "11111111-1111-1111-1111-111111111111"
    assert playlist.warnings == ["track 3: skipped entry without a title"]

    tracks = [
        Track(title="Song One", artist="Artist A", album="Album A", duration_ms=200000, isrc="USAAA0100001",
              source_uri="spotify:track:aaa1", mb_recording_id="11111111-1111-1111-1111-111111111111",
              mb_release_id="22222222-2222-2222-2222-222222222222", mb_release_track_count=10, position=0),
        Track(title="Bare", position=1, local_path=str(tmp_path / "bare.flac")),
    ]
    path = write_jspf(tmp_path / "out.jspf", "Round Trip", tracks, "spotify-export", "Playlist1.json")
    data = json.loads(path.read_text())
    item = data["playlist"]["track"][0]
    assert item["identifier"] == ["https://musicbrainz.org/recording/11111111-1111-1111-1111-111111111111"]
    ext = item["extension"]["https://musicbrainz.org/doc/jspf#track"]
    assert ext["release_identifier"].endswith("22222222-2222-2222-2222-222222222222")
    assert ext["additional_metadata"] == {"isrc": "USAAA0100001", "source_uri": "spotify:track:aaa1", "release_track_count": 10}

    (again,) = import_file(path)
    assert again.source == "jspf" and again.name == "Round Trip"
    assert again.tracks[0] == Track(**{**tracks[0].__dict__})
    assert again.tracks[1].local_path == str(tmp_path / "bare.flac")
    assert detect_format(path) == "jspf"


def test_detect_format_rejects_unknown(tmp_path):
    with pytest.raises(ValueError, match="cannot detect"):
        detect_format(tmp_path / "x.txt")


def test_write_m3u_reports_missing(tmp_path):
    (tmp_path / "a.flac").write_bytes(b"x")
    result = write_m3u(tmp_path / "Playlists" / "p.m3u8", "P", [
        {"position": 1, "title": "A", "artist": "X", "duration_ms": 200000, "local_path": str(tmp_path / "a.flac")},
        {"position": 2, "title": "B", "artist": "", "duration_ms": None, "local_path": None, "status": "not_found"},
    ], relative_to=tmp_path / "Playlists")
    text = Path(result["path"]).read_text()
    assert text == "#EXTM3U\n#PLAYLIST:P\n#EXTINF:200,X - A\n../a.flac\n"
    assert result["written"] == 1
    assert result["missing"] == [{"position": 2, "title": "B", "artist": "", "status": "not_found"}]


@pytest.mark.parametrize("raw, expected", [
    ("Lonely Song - 2011 Remaster", "Lonely Song"),
    ("Song Two (feat. Guest)", "Song Two"),
    ("Song [Live at Wembley]", "Song"),
    ("Plain", "Plain"),
    ("(Everything Bracketed)", "(Everything Bracketed)"),
])
def test_clean_title(raw, expected):
    assert textnorm.clean_title(raw) == expected


def test_clean_artist_and_query_words():
    assert textnorm.clean_artist("Solo C, Guest") == "Solo C"
    assert textnorm.clean_artist("Artist A feat. B") == "Artist A"
    assert textnorm.query_words("Björk & The Band", "Jóga (Live)") == "bjork and the band joga live"
    assert textnorm.normalize("Sigur Rós – Ágætis byrjun") == "sigur ros agaetis byrjun"
    assert textnorm.token_overlap("the song one", "01 - Song One.flac") == 1.0
    assert textnorm.token_overlap("song two", "01 - Song One.flac") == 0.5
    assert textnorm.token_overlap("", "anything") == 0.0
