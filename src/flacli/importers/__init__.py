# SPDX-License-Identifier: GPL-3.0-or-later
"""Playlist file importers. import_file() detects the format and returns one ImportedPlaylist per playlist."""

import json

from pathlib import Path

from ..models import ImportedPlaylist
from . import csvfile, m3ufile, spotify_export, xspf

FORMATS = ("auto", "spotify-export", "exportify", "csv", "m3u", "jspf", "xspf")


def detect_format(path: Path) -> str:
    suffix = path.suffix.lower()

    if suffix in (".m3u", ".m3u8"):
        return "m3u"
    if suffix == ".xspf":
        return "xspf"
    if suffix == ".jspf":
        return "jspf"
    if suffix == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as handle:
            header = handle.readline().lower()
        return "exportify" if "track uri" in header and "track name" in header else "csv"
    if suffix == ".json":
        with path.open(encoding="utf-8") as handle:
            head = handle.read(4096)
        if '"playlist"' in head and ('"track"' in head or '"title"' in head) and '"playlists"' not in head:
            return "jspf"
        return "spotify-export"

    raise ValueError(f"cannot detect playlist format of {path.name}; pass format explicitly")


def import_file(path, format="auto", column_mapping=None, playlist_name=None) -> list[ImportedPlaylist]:
    path = Path(path).expanduser()

    if not path.is_file():
        raise FileNotFoundError(f"{path} does not exist")

    if format not in FORMATS:
        raise ValueError(f"format must be one of {', '.join(FORMATS)}")

    if format == "auto":
        format = detect_format(path)

    if format == "spotify-export":
        playlists = spotify_export.parse(path)
    elif format in ("exportify", "csv"):
        playlists = [csvfile.parse(path, column_mapping=column_mapping, source=format)]
    elif format == "m3u":
        playlists = [m3ufile.parse(path)]
    elif format == "jspf":
        playlists = [xspf.parse_jspf(path)]
    else:
        playlists = [xspf.parse_xspf(path)]

    if playlist_name:
        wanted = [p for p in playlists if p.name == playlist_name]

        if not wanted:
            names = ", ".join(repr(p.name) for p in playlists)
            raise LookupError(f"no playlist named {playlist_name!r} in {path.name}; found: {names}")

        playlists = wanted

    for playlist in playlists:
        playlist.source_ref = playlist.source_ref or path.name
        playlist.has_durations = any(t.duration_ms for t in playlist.tracks)

    return playlists
