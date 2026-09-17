"""Pure helpers of the Soulseek MCP server."""

import pytest


@pytest.fixture(scope="module")
def server():
    from flacli import soulseek_mcp
    return soulseek_mcp


def result(path, user="u", size=1_000_000, free_slot=True, speed=100_000, queue=0, **attrs):
    base = {"id": 0, "user": user, "path": path, "size": size, "bitrate": None, "duration": None, "vbr": None,
            "sample_rate": None, "bit_depth": None, "free_slot": free_slot, "speed": speed, "queue": queue,
            "private": False}
    base.update(attrs)
    return base


@pytest.mark.parametrize("fields, expected", [
    (dict(path="a\\b.flac", sample_rate=44100, bit_depth=16), "flac 16bit 44.1kHz"),
    (dict(path="a\\b.flac", sample_rate=96000, bit_depth=24), "flac 24bit 96kHz"),
    (dict(path="a\\b.FLAC"), "flac"),
    (dict(path="a\\b.mp3", bitrate=320), "mp3 320kbps"),
    (dict(path="a\\b.mp3", bitrate=245, vbr=True), "mp3 245vkbps"),
    (dict(path="a\\b.mp3"), "mp3"),
    (dict(path="a\\noext"), "?"),
])
def test_quality_labels(server, fields, expected):
    assert server._quality(result(**fields)) == expected


def test_ext_and_mb(server):
    assert server._ext("x\\y\\Song.Name.FLAC") == "flac"
    assert server._ext("x\\y\\noext") == ""
    assert server._mb(None) == 0
    assert server._mb(1048576 * 3.25) == 3.2


def test_group_folders_ranks_and_paginates(server):
    results = [
        result("@@m\\A\\Album\\01.flac", user="busy", free_slot=False, queue=9, speed=9_000_000, sample_rate=44100, bit_depth=16),
        result("@@m\\A\\Album\\02.flac", user="busy", free_slot=False, queue=9, speed=9_000_000, sample_rate=44100, bit_depth=16),
        result("@@m\\A\\Album\\03.flac", user="busy", free_slot=False, queue=9, speed=9_000_000, sample_rate=44100, bit_depth=16),
        result("@@m\\B\\Album\\01.mp3", user="free", free_slot=True, queue=0, speed=100_000, bitrate=320, duration=125),
        result("@@m\\B\\Album\\02.mp3", user="free", free_slot=True, queue=0, speed=100_000, bitrate=320),
        result("@@m\\C\\Single\\01.mp3", user="slow", free_slot=True, queue=2, speed=10, bitrate=128, private=True),
    ]
    for index, r in enumerate(results):
        r["id"] = index

    groups, total = server._group_folders(results, max_folders=2, files_per_folder=1)
    assert total == 3
    assert [g["user"] for g in groups] == ["free", "slow"]

    free = groups[0]
    assert free["folder"] == "@@m\\B\\Album"
    assert free["files_matched"] == 2
    assert free["quality"] == "mp3 320kbps"
    assert free["speed_kbps"] == 800
    assert free["files_not_shown"] == 1
    assert free["files"] == [{"id": 3, "name": "01.mp3", "mb": 1.0, "quality": "mp3 320kbps", "length": "2:05"}]
    assert groups[1]["files"][0]["private"] is True

    groups, _ = server._group_folders(results, max_folders=10, files_per_folder=25)
    assert [g["user"] for g in groups] == ["free", "slow", "busy"]
    assert groups[2]["total_mb"] == 2.9
