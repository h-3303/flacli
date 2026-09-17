# SPDX-License-Identifier: GPL-3.0-or-later
"""M3U8 writer: original playlist order, one #EXTINF per owned track, missing tracks reported."""

import os

from pathlib import Path


def write_m3u(path: Path, name: str, entries: list[dict], relative_to: Path | None = None) -> dict:
    """entries: [{position, title, artist, duration_ms, local_path or None}] in playlist order."""
    lines = ["#EXTM3U", f"#PLAYLIST:{name}"]
    written, missing = [], []

    for entry in entries:
        local_path = entry.get("local_path")

        if not local_path:
            missing.append({"position": entry["position"], "title": entry["title"], "artist": entry["artist"],
                            "status": entry.get("status")})
            continue

        seconds = round((entry.get("duration_ms") or 0) / 1000) if entry.get("duration_ms") else -1
        label = f"{entry['artist']} - {entry['title']}" if entry["artist"] else entry["title"]
        lines.append(f"#EXTINF:{seconds},{label}")
        location = local_path

        if relative_to is not None:
            try:
                location = os.path.relpath(local_path, relative_to)
            except ValueError:
                location = local_path

        lines.append(location)
        written.append(location)

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"path": str(path), "written": len(written), "missing": missing}
