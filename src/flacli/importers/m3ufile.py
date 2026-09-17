# SPDX-License-Identifier: GPL-3.0-or-later
"""M3U / M3U8 with #EXTINF lines. Local paths that exist are kept as already-owned tracks."""

import re

from pathlib import Path

from ..models import ImportedPlaylist, Track

_EXTINF = re.compile(r"^#EXTINF:\s*(-?\d+(?:\.\d+)?)\s*(?:[^,]*)?,\s*(.*)$")


def parse(path: Path) -> ImportedPlaylist:
    tracks = []
    warnings: list[str] = []
    name = path.stem
    pending: tuple[int | None, str] | None = None

    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = line.strip()

        if not line:
            continue

        if line.startswith("#PLAYLIST:"):
            name = line[len("#PLAYLIST:"):].strip() or name
            continue

        if line.startswith("#EXTINF"):
            match = _EXTINF.match(line)

            if match:
                seconds = float(match.group(1))
                pending = (int(seconds * 1000) if seconds > 0 else None, match.group(2).strip())

            continue

        if line.startswith("#"):
            continue

        duration_ms, label = pending or (None, "")
        pending = None
        artist, title = "", label

        if " - " in label:
            artist, title = (part.strip() for part in label.split(" - ", 1))

        location = line
        local_path = None

        if location.startswith("file://"):
            from urllib.parse import unquote, urlparse
            location = unquote(urlparse(location).path)

        if not re.match(r"^[a-z]+://", location):
            candidate = Path(location)

            if not candidate.is_absolute():
                candidate = path.parent / candidate

            if candidate.is_file():
                local_path = str(candidate.resolve())

        if not title:
            title = Path(location).stem
            warnings.append(f"no #EXTINF for {location}; used the file name as title")

        tracks.append(Track(title=title, artist=artist, duration_ms=duration_ms, source_uri=line,
                            local_path=local_path, position=len(tracks)))

    if not tracks:
        raise ValueError(f"{path.name} contains no entries")

    return ImportedPlaylist(name=name, source="m3u", tracks=tracks, warnings=warnings)
