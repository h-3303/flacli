"""Query building and the documented confidence / score table."""

import pytest

from flacli.matcher import MatchPrefs, best_candidates, build_queries, download_id, score_folder, score_result

TRACK = {"id": 1, "title": "Lonely Song - 2011 Remaster", "artist": "Solo C, Guest", "album": "Alone", "duration_ms": 185000}


def result(path, user="peer", duration=185, bitrate=None, free_slot=True, queue=0, speed=500_000, **extra):
    return {"user": user, "path": path, "size": 30_000_000, "duration": duration, "bitrate": bitrate,
            "sample_rate": None, "bit_depth": None, "vbr": None, "free_slot": free_slot, "queue": queue,
            "speed": speed, **extra}


def test_build_queries_in_fallback_order():
    assert build_queries("Lonely Song - 2011 Remaster", "Solo C, Guest", "Alone") == [
        ("solo c lonely song", False),
        ("lonely song alone", True),
        ("lonely song", True),
    ]
    assert build_queries("Plain", "", "") == [("plain", True)]


def test_download_id_matches_bridge():
    # same value the harness bridge test observed for this user/path pair
    assert download_id("peer1", "@@music\\Test Artist\\Album\\01 - Song.flac") == "796b2ed45f74"


# (description, result, expected confidence, expected breakdown keys)
CASES = [
    ("exact flac, right folder, duration ok",
     result("@@m\\Solo C\\Alone\\03 - Lonely Song.flac"), 1.0),
    ("exact mp3, no album in folder name",
     result("@@m\\Solo C\\Best Of\\Lonely Song.mp3", bitrate=320), 0.9),
    ("duration off by 5 s on a lossy file (within twice the 3 s tolerance -> 0.5 duration)",
     result("@@m\\Solo C\\Alone\\03 - Lonely Song.mp3", duration=190, bitrate=320), 0.9),
    ("duration off by 8 s on a lossy file (beyond twice the tolerance) caps at 0.4",
     result("@@m\\Solo C\\Alone\\03 - Lonely Song.mp3", duration=193, bitrate=320), 0.4),
    ("duration off by 8 s on FLAC (within the 10 s lossless tolerance)",
     result("@@m\\Solo C\\Alone\\03 - Lonely Song.flac", duration=193), 1.0),
    ("duration off by 30 s caps confidence at 0.4",
     result("@@m\\Solo C\\Alone\\03 - Lonely Song (live).flac", duration=215), 0.4),
    ("half the title words present is weak but uncapped",
     result("@@m\\Solo C\\Alone\\03 - Lonely.flac"), 0.775),
    ("title absent caps confidence at 0.3 even with artist, album and duration right",
     result("@@m\\Solo C\\Alone\\03 - Nothing Here.flac"), 0.3),
    ("no artist anywhere in the path, album folder wrong",
     result("@@m\\Unknown\\Random\\Lonely Song.flac"), 0.65),
    ("duration unknown on the peer side: duration component skipped",
     result("@@m\\Solo C\\Alone\\03 - Lonely Song.flac", duration=None), 1.0),
]


@pytest.mark.parametrize("description, peer_result, expected", CASES, ids=[c[0] for c in CASES])
def test_confidence_table(description, peer_result, expected):
    candidate = score_result(TRACK, peer_result, MatchPrefs())
    assert candidate is not None, description
    assert candidate["confidence"] == pytest.approx(expected, abs=0.01), description


def test_rejections_and_strict_artist():
    prefs = MatchPrefs()
    assert score_result(TRACK, result("@@m\\x\\Lonely Song.jpg"), prefs) is None
    assert score_result(TRACK, result("@@m\\x\\Lonely Song.wma"), prefs) is None          # not in allow_formats
    assert score_result(TRACK, result("@@m\\x\\Lonely Song.mp3", bitrate=128), MatchPrefs(min_bitrate=256)) is None
    assert score_result(TRACK, result("@@m\\Other\\Lonely Song.flac"), prefs, strict_artist=True) is None
    assert score_result(TRACK, result("@@m\\Solo C\\Lonely Song.flac"), prefs, strict_artist=True) is not None


def test_ranking_prefers_format_availability_and_penalises_users():
    prefs = MatchPrefs()
    peers = [
        result("@@m\\Solo C\\Alone\\03 - Lonely Song.mp3", user="fast-mp3", bitrate=320, free_slot=True, queue=0, speed=2_000_000),
        result("@@m\\Solo C\\Alone\\03 - Lonely Song.flac", user="busy-flac", free_slot=False, queue=20, speed=100_000),
        result("@@m\\Solo C\\Alone\\03 - Lonely Song.flac", user="free-flac", free_slot=True, queue=0, speed=800_000),
        result("@@m\\Solo C\\Alone\\03 - Lonely Song.flac", user="free-flac", free_slot=True, queue=0, speed=800_000),
    ]
    ranked = best_candidates(TRACK, peers, prefs)
    assert [c["user"] for c in ranked] == ["free-flac", "busy-flac", "fast-mp3"]   # duplicates collapsed
    assert all(c["confidence"] == 1.0 for c in ranked)

    penalised = best_candidates(TRACK, peers, prefs, user_failures={"free-flac": 2})
    assert penalised[0]["user"] == "busy-flac"

    lossy_ok = best_candidates(TRACK, peers, MatchPrefs(prefer_formats=["mp3"]))
    assert lossy_ok[0]["user"] == "fast-mp3"


def test_score_folder_checks_track_count_and_coverage():
    prefs = MatchPrefs()
    tracks = [{"id": 1, "title": "Track 1"}, {"id": 2, "title": "Track 2"}, {"id": 3, "title": "Track 3"}]
    folder = "@@m\\Band B\\B Album"
    listing = [{"name": f"0{i} - Track {i}.flac", "path": f"{folder}\\0{i} - Track {i}.flac", "size": 10} for i in (1, 2, 3)]
    listing.append({"name": "cover.jpg", "path": f"{folder}\\cover.jpg", "size": 1})

    exact = score_folder(tracks, listing, 3, "peerB", folder, prefs, {"free_slot": True, "queue": 0})
    assert exact["track_count"] == 3 and exact["confidence"] == 1.0
    assert exact["assignments"] == {1: listing[0]["path"], 2: listing[1]["path"], 3: listing[2]["path"]}
    assert exact["formats"] == ["flac"] and exact["size"] == 30

    off_by_one = score_folder(tracks, listing, 4, "peerB", folder, prefs)
    assert off_by_one["confidence"] == pytest.approx(0.88, abs=0.01)

    wrong = score_folder(tracks, listing, 12, "peerB", folder, prefs)
    assert wrong["confidence"] == pytest.approx(0.76, abs=0.01)

    assert score_folder(tracks, [{"name": "cover.jpg", "path": f"{folder}\\cover.jpg", "size": 1}], 3, "p", folder, prefs) is None
