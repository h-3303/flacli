# SPDX-License-Identifier: GPL-3.0-or-later
"""Local library index (mutagen) and playlist diff."""

import os
import time

from pathlib import Path

from .db import Database, utcnow
from .textnorm import clean_title, normalize

AUDIO_EXTENSIONS = {".flac", ".mp3", ".ogg", ".oga", ".opus", ".m4a", ".mp4", ".aac", ".wav", ".wv", ".ape",
                    ".aif", ".aiff", ".dsf", ".tta", ".mpc", ".wma"}
DURATION_TOLERANCE_S = 5


def _first(value):
    if isinstance(value, (list, tuple)):
        return str(value[0]) if value else None

    return str(value) if value is not None else None


def read_tags(path: Path) -> dict | None:
    from mutagen import File, MutagenError

    try:
        audio = File(str(path), easy=True)
    except MutagenError:
        return None
    except Exception:
        return None

    if audio is None:
        return None

    tags = audio.tags or {}
    info = audio.info

    def tag(*names):
        for name in names:
            value = _first(tags.get(name))
            if value:
                return value.strip()
        return None

    # EasyID3/EasyMP4 expose lowercase keys; Vorbis comments keep their own names
    return {
        "title": tag("title"),
        "artist": tag("artist", "albumartist"),
        "album": tag("album"),
        "isrc": (tag("isrc") or "").upper() or None,
        "mb_recording_id": tag("musicbrainz_trackid", "musicbrainz_recordingid"),
        "mb_release_id": tag("musicbrainz_albumid"),
        "duration_ms": int(info.length * 1000) if getattr(info, "length", None) else None,
        "format": type(audio).__name__.lower(),
        "bitrate": getattr(info, "bitrate", None),
        "sample_rate": getattr(info, "sample_rate", None),
        "bit_depth": getattr(info, "bits_per_sample", None),
    }


def scan_library(db: Database, root: Path, rescan=False, progress=None, collect_new=False) -> dict:
    root = Path(root).expanduser().resolve()

    if not root.is_dir():
        raise FileNotFoundError(f"music directory {root} does not exist")

    seen: set[str] = set()
    new_paths: list[str] = []
    counts = {"root": str(root), "files": 0, "indexed": 0, "unchanged": 0, "unreadable": 0, "removed": 0}
    started = time.monotonic()

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))

        for filename in sorted(filenames):
            path = Path(dirpath) / filename

            if path.suffix.lower() not in AUDIO_EXTENSIONS:
                continue

            counts["files"] += 1
            key = str(path)
            seen.add(key)

            try:
                stat = path.stat()
            except OSError:
                counts["unreadable"] += 1
                continue

            existing = db.library_file(key)

            if existing and not rescan and existing["mtime"] == stat.st_mtime and existing["size"] == stat.st_size:
                counts["unchanged"] += 1
                continue

            if not index_file(db, path, stat):
                counts["unreadable"] += 1
                continue

            counts["indexed"] += 1

            if existing is None:
                new_paths.append(key)

            if progress and counts["indexed"] % 200 == 0:
                progress(counts)

    counts["removed"] = db.library_prune(seen, str(root))
    counts["seconds"] = round(time.monotonic() - started, 1)
    counts["library_total"] = db.library_count()

    if collect_new:
        counts["new_paths"] = new_paths

    return counts


def index_file(db: Database, path: Path, stat=None) -> bool:
    """(Re)index one audio file; False when it cannot be read."""
    path = Path(path)
    stat = stat or path.stat()
    tags = read_tags(path)

    if tags is None:
        return False

    title = tags["title"] or path.stem
    db.library_upsert(
        path=str(path), mtime=stat.st_mtime, size=stat.st_size, title=title, artist=tags["artist"],
        album=tags["album"], duration_ms=tags["duration_ms"], isrc=tags["isrc"],
        mb_recording_id=tags["mb_recording_id"], mb_release_id=tags["mb_release_id"], format=tags["format"],
        bitrate=tags["bitrate"], sample_rate=tags["sample_rate"], bit_depth=tags["bit_depth"],
        title_norm=normalize(clean_title(title)), artist_norm=normalize(clean_title(tags["artist"] or "")),
        scanned_at=utcnow(),
    )
    return True


def reindex_moved(db: Database, moves: dict) -> int:
    """After files moved ({old_path: new_path}): drop the old index rows, index the new paths."""
    indexed = 0

    for old, new in moves.items():
        db.conn.execute("DELETE FROM library_files WHERE path = ?", (old,))

        if Path(new).is_file() and index_file(db, Path(new)):
            indexed += 1

    return indexed


def find_local(db: Database, track, tolerance_s=DURATION_TOLERANCE_S) -> tuple[str, str] | None:
    """(local_path, method) for a track row / Track-like object, or None."""
    def field(name):
        try:
            return track[name]
        except (TypeError, KeyError, IndexError):
            return getattr(track, name, None)

    if field("mb_recording_id"):
        rows = db.library_find("mb_recording_id", field("mb_recording_id"))
        if rows:
            return rows[0]["path"], "mbid"

    if field("isrc"):
        rows = db.library_find("isrc", field("isrc"))
        if rows:
            return rows[0]["path"], "isrc"

    title_norm = normalize(clean_title(field("title") or ""))
    artist_norm = normalize(clean_title(field("artist") or ""))

    if not title_norm:
        return None

    rows = db.library_find_norm(artist_norm, title_norm) if artist_norm else db.library_find("title_norm", title_norm)
    duration = field("duration_ms")

    for row in rows:
        if duration and row["duration_ms"]:
            if abs(row["duration_ms"] - duration) / 1000 > tolerance_s:
                continue

        return row["path"], "text+duration" if duration and row["duration_ms"] else "text"

    return None


def diff_playlist(db: Database, playlist_id, tolerance_s=DURATION_TOLERANCE_S) -> dict:
    found, methods = 0, {}
    considered = 0

    for row in db.tracks(playlist_id):
        if row["status"] in ("done", "skipped"):
            continue

        if row["status"] == "in_library" and row["local_path"] and Path(row["local_path"]).is_file():
            continue

        considered += 1
        hit = find_local(db, row, tolerance_s)

        if hit:
            path, method = hit
            db.set_match(row["id"], "in_library", local_path=path, last_error=None)
            found += 1
            methods[method] = methods.get(method, 0) + 1
        elif row["status"] == "in_library":
            db.set_match(row["id"], "pending", local_path=None)  # file went away

    return {"playlist_id": playlist_id, "checked": considered, "in_library": found, "by_method": methods,
            "counts": db.status_counts(playlist_id)}
