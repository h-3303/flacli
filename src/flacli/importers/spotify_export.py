# SPDX-License-Identifier: GPL-3.0-or-later
"""Spotify "Download your data" export: Playlist1.json (playlists) and YourLibrary.json (liked tracks).

These files carry names only (no durations, no ISRCs); tracks are flagged so MusicBrainz fills durations in.
"""

import json

from pathlib import Path

from ..models import ImportedPlaylist, Track


def _track_from_item(item, position, warnings):
    track = item.get("track") if isinstance(item, dict) else None

    if not track:
        kind = "episode" if item.get("episode") else "local track" if item.get("localTrack") else "unknown item"
        warnings.append(f"position {position + 1}: skipped {kind}")
        return None

    title = (track.get("trackName") or "").strip()

    if not title:
        warnings.append(f"position {position + 1}: skipped track without a name")
        return None

    return Track(
        title=title,
        artist=(track.get("artistName") or "").strip(),
        album=(track.get("albumName") or "").strip(),
        source_uri=track.get("trackUri"),
        position=position,
    )


def parse(path: Path) -> list[ImportedPlaylist]:
    data = json.loads(path.read_text(encoding="utf-8"))
    playlists = []

    if isinstance(data, dict) and isinstance(data.get("playlists"), list):
        for entry in data["playlists"]:
            warnings: list[str] = []
            tracks = []

            for position, item in enumerate(entry.get("items") or []):
                track = _track_from_item(item, position, warnings)

                if track is not None:
                    track.position = len(tracks)
                    tracks.append(track)

            playlists.append(ImportedPlaylist(
                name=(entry.get("name") or "Untitled playlist").strip(), source="spotify-export",
                tracks=tracks, warnings=warnings, has_durations=False,
            ))

    elif isinstance(data, dict) and isinstance(data.get("tracks"), list):
        warnings = []
        tracks = []

        for position, item in enumerate(data["tracks"]):
            title = (item.get("track") or "").strip()

            if not title:
                warnings.append(f"position {position + 1}: skipped entry without a track name")
                continue

            tracks.append(Track(
                title=title, artist=(item.get("artist") or "").strip(), album=(item.get("album") or "").strip(),
                source_uri=item.get("uri"), position=len(tracks),
            ))

        playlists.append(ImportedPlaylist(name="Liked Songs", source="spotify-export", tracks=tracks,
                                          warnings=warnings, has_durations=False))
    else:
        raise ValueError(f"{path.name} is not a Spotify playlist export (expected a 'playlists' or 'tracks' list)")

    if not playlists:
        raise ValueError(f"{path.name} contains no playlists")

    return playlists
