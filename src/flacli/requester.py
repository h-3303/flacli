# SPDX-License-Identifier: GPL-3.0-or-later
"""Direct requests: named songs and albums become tracks of a persistent request playlist.

Items are either dicts ({"artist", "title"} for a song, {"artist", "album"} for a whole album) or strings:
"Artist - Title", "Artist - Album (album)", "album: Artist - Album". An album is expanded into its tracklist
through MusicBrainz so that the matcher's album mode can fetch the whole folder and check the track count.
"""

import re

from .models import Track

ALBUM_MARK = re.compile(r"^\s*album\s*:\s*(.+)$|^(.+?)\s*[\(\[]\s*album\s*[\)\]]\s*$", re.IGNORECASE)
SEPARATORS = (" - ", " – ", " — ")


def parse_item(item) -> dict:
    """Normalise one request item to {"kind": "track", "artist", "title", "album"} or {"kind": "album", "artist", "album"}."""
    if isinstance(item, dict):
        artist = str(item.get("artist") or "").strip()
        title = str(item.get("title") or "").strip()
        album = str(item.get("album") or "").strip()
        kind = str(item.get("kind") or ("track" if title else "album")).lower()

        if kind == "track":
            if not title:
                raise ValueError(f"a track item needs a title: {item!r}")

            return {"kind": "track", "artist": artist, "title": title, "album": album}

        if kind == "album":
            if not album:
                raise ValueError(f"an album item needs an album: {item!r}")

            return {"kind": "album", "artist": artist, "album": album}

        raise ValueError(f"kind must be 'track' or 'album': {item!r}")

    text = str(item).strip()
    kind = "track"
    marked = ALBUM_MARK.match(text)

    if marked:
        kind = "album"
        text = (marked.group(1) or marked.group(2)).strip()

    artist, name = "", text

    for separator in SEPARATORS:
        if separator in text:
            artist, name = (part.strip() for part in text.split(separator, 1))
            break

    if not name:
        raise ValueError(f"empty request item: {item!r}")

    if kind == "album":
        return {"kind": "album", "artist": artist, "album": name}

    return {"kind": "track", "artist": artist, "title": name, "album": ""}


def parse_items(items) -> list[dict]:
    if not items:
        raise ValueError("no items requested")

    return [parse_item(item) for item in items]


def expand(client, parsed: list[dict]) -> tuple[list[Track], list[dict]]:
    """Tracks for the playlist plus one report entry per item (albums carry the release that was chosen)."""
    tracks: list[Track] = []
    report: list[dict] = []

    for item in parsed:
        if item["kind"] == "track":
            tracks.append(Track(title=item["title"], artist=item["artist"], album=item["album"], source_uri="request:track"))
            report.append({"kind": "track", "artist": item["artist"], "title": item["title"], "tracks": 1})
            continue

        release = client.find_release(item["artist"], item["album"])

        if release is None:
            report.append({"kind": "album", "artist": item["artist"], "album": item["album"], "tracks": 0,
                           "error": "no matching release on MusicBrainz; name the tracks instead, or check the spelling"})
            continue

        listing = client.release_tracks(release["release_id"])

        for entry in listing:
            tracks.append(Track(
                title=entry["title"], artist=entry["artist"] or release["artist"] or item["artist"], album=release["title"],
                duration_ms=entry["duration_ms"], mb_recording_id=entry["recording_id"], mb_release_id=release["release_id"],
                mb_release_track_count=release["track_count"] or len(listing),
                source_uri=f"request:album:{release['release_id']}",
            ))

        report.append({"kind": "album", "artist": item["artist"], "album": item["album"], "tracks": len(listing),
                       "release": {"id": release["release_id"], "title": release["title"], "artist": release["artist"],
                                   "date": release["date"], "track_count": release["track_count"] or len(listing)}})

    return tracks, report
