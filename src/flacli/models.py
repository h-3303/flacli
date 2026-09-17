# SPDX-License-Identifier: GPL-3.0-or-later
"""Plain data carriers shared by importers, resolver, matcher and server."""

from dataclasses import dataclass, field

MATCH_STATUSES = (
    "pending", "in_library", "searching", "candidates", "approved", "queued", "downloading", "done",
    "not_found", "failed", "skipped",
)


@dataclass
class Track:
    title: str
    artist: str = ""
    album: str = ""
    duration_ms: int | None = None
    isrc: str | None = None
    source_uri: str | None = None
    mb_recording_id: str | None = None
    mb_release_id: str | None = None
    mb_release_track_count: int | None = None
    position: int = 0
    local_path: str | None = None       # set by M3U / JSPF imports that already point at files


@dataclass
class ImportedPlaylist:
    name: str
    source: str                          # spotify-export, exportify, csv, m3u, jspf, xspf
    tracks: list[Track]
    source_ref: str | None = None        # playlist URI / original file name
    warnings: list[str] = field(default_factory=list)
    has_durations: bool = True
