# SPDX-License-Identifier: GPL-3.0-or-later
"""JSPF (JSON XSPF) with the MusicBrainz extension: the interchange format for imported playlists."""

import json

from pathlib import Path

from .models import Track

TRACK_EXT = "https://musicbrainz.org/doc/jspf#track"
PLAYLIST_EXT = "https://musicbrainz.org/doc/jspf#playlist"
RECORDING_PREFIX = "https://musicbrainz.org/recording/"
RELEASE_PREFIX = "https://musicbrainz.org/release/"


def track_to_jspf(track: Track) -> dict:
    item: dict = {"title": track.title}

    if track.artist:
        item["creator"] = track.artist
    if track.album:
        item["album"] = track.album
    if track.duration_ms:
        item["duration"] = int(track.duration_ms)
    if track.mb_recording_id:
        item["identifier"] = [RECORDING_PREFIX + track.mb_recording_id]
    if track.local_path:
        item["location"] = [Path(track.local_path).as_uri()]

    extension = {}
    metadata = {}

    if track.mb_release_id:
        extension["release_identifier"] = RELEASE_PREFIX + track.mb_release_id
    if track.isrc:
        metadata["isrc"] = track.isrc
    if track.source_uri:
        metadata["source_uri"] = track.source_uri
    if track.mb_release_track_count:
        metadata["release_track_count"] = track.mb_release_track_count
    if metadata:
        extension["additional_metadata"] = metadata
    if extension:
        item["extension"] = {TRACK_EXT: extension}

    return item


def write_jspf(path: Path, name: str, tracks: list[Track], source: str, source_ref=None) -> Path:
    playlist = {
        "title": name,
        "creator": "flacli",
        "track": [track_to_jspf(t) for t in tracks],
        "extension": {PLAYLIST_EXT: {"public": False, "additional_metadata": {"source": source, "source_ref": source_ref}}},
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"playlist": playlist}, indent=2, ensure_ascii=False))
    return path


def _first(value):
    if isinstance(value, list):
        return value[0] if value else None

    return value


def track_from_jspf(item: dict, position: int) -> Track:
    extension = (item.get("extension") or {}).get(TRACK_EXT) or {}
    metadata = extension.get("additional_metadata") or {}
    recording = next((i[len(RECORDING_PREFIX):] for i in (item.get("identifier") or [])
                      if isinstance(i, str) and i.startswith(RECORDING_PREFIX)), None)
    release = extension.get("release_identifier")
    location = _first(item.get("location"))
    local_path = None

    if isinstance(location, str) and location.startswith("file://"):
        from urllib.parse import unquote, urlparse
        local_path = unquote(urlparse(location).path)

    duration = item.get("duration")

    return Track(
        title=str(item.get("title") or "").strip(),
        artist=str(item.get("creator") or "").strip(),
        album=str(item.get("album") or "").strip(),
        duration_ms=int(duration) if duration else None,
        isrc=metadata.get("isrc"),
        source_uri=metadata.get("source_uri"),
        mb_recording_id=recording,
        mb_release_id=release[len(RELEASE_PREFIX):] if isinstance(release, str) and release.startswith(RELEASE_PREFIX) else None,
        mb_release_track_count=metadata.get("release_track_count"),
        position=position,
        local_path=local_path,
    )
