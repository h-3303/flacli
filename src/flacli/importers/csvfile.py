# SPDX-License-Identifier: GPL-3.0-or-later
"""Exportify CSV and generic CSV (header synonyms, or an explicit column mapping)."""

import csv
import re

from pathlib import Path

from ..models import ImportedPlaylist, Track

SYNONYMS = {
    "title": ("track name", "title", "name", "song", "track", "track title"),
    "artist": ("artist name(s)", "artist", "artists", "artist name", "artist(s)"),
    "album": ("album name", "album"),
    "duration_ms": ("track duration (ms)", "duration (ms)", "duration_ms", "duration", "length", "time"),
    "isrc": ("isrc",),
    "source_uri": ("track uri", "uri", "spotify uri", "url", "link"),
}

_TIME = re.compile(r"^(\d+):(\d{1,2})(?::(\d{1,2}))?$")


def resolve_columns(header: list[str], column_mapping: dict | None) -> dict:
    lowered = {h.strip().lower(): h for h in header}
    mapping = {}

    for field, names in SYNONYMS.items():
        if column_mapping and field in column_mapping:
            column = column_mapping[field]

            if column not in header:
                raise ValueError(f"column {column!r} for {field} not in CSV header {header}")

            mapping[field] = column
            continue

        for name in names:
            if name in lowered:
                mapping[field] = lowered[name]
                break

    if "title" not in mapping:
        raise ValueError(f"cannot find a title column in {header}; pass column_mapping={{'title': ...}}")

    return mapping


def parse_duration(value: str | None) -> int | None:
    if not value:
        return None

    value = value.strip()
    match = _TIME.match(value)

    if match:
        parts = [int(p) for p in match.groups() if p is not None]
        seconds = parts[0] * 60 + parts[1] if len(parts) == 2 else parts[0] * 3600 + parts[1] * 60 + parts[2]
        return seconds * 1000

    try:
        number = float(value)
    except ValueError:
        return None

    return int(number if number > 10_000 else number * 1000)  # values under 10 000 are seconds


def parse(path: Path, column_mapping=None, source="csv") -> ImportedPlaylist:
    warnings: list[str] = []
    tracks = []

    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)

        if not reader.fieldnames:
            raise ValueError(f"{path.name} has no header row")

        columns = resolve_columns(reader.fieldnames, column_mapping)

        for row_number, row in enumerate(reader, start=2):
            title = (row.get(columns["title"]) or "").strip()

            if not title:
                warnings.append(f"line {row_number}: skipped row without a title")
                continue

            def cell(field):
                column = columns.get(field)
                return (row.get(column) or "").strip() if column else ""

            artist = cell("artist")

            if source == "exportify" and "," in artist and ";" not in artist:
                artist = artist  # Exportify joins multiple artists with ", "; clean_artist() handles it

            tracks.append(Track(
                title=title, artist=artist, album=cell("album"),
                duration_ms=parse_duration(cell("duration_ms")),
                isrc=cell("isrc").upper() or None, source_uri=cell("source_uri") or None, position=len(tracks),
            ))

    if not tracks:
        raise ValueError(f"{path.name} contains no tracks")

    return ImportedPlaylist(name=path.stem, source=source, tracks=tracks, warnings=warnings)
