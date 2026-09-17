# SPDX-License-Identifier: GPL-3.0-or-later
"""SQLite state store with versioned migrations. Everything the pipeline does is resumable from here."""

import json
import sqlite3
import time

from datetime import datetime, timezone
from pathlib import Path

from .models import MATCH_STATUSES, Track

MIGRATIONS = [
    # 1: initial schema
    """
    CREATE TABLE playlists (
        id INTEGER PRIMARY KEY,
        source TEXT NOT NULL,
        source_ref TEXT,
        name TEXT NOT NULL,
        imported_at TEXT NOT NULL,
        jspf_path TEXT,
        track_count INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE tracks (
        id INTEGER PRIMARY KEY,
        playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
        position INTEGER NOT NULL,
        title TEXT NOT NULL,
        artist TEXT NOT NULL DEFAULT '',
        album TEXT NOT NULL DEFAULT '',
        duration_ms INTEGER,
        duration_source TEXT,
        isrc TEXT,
        mb_recording_id TEXT,
        mb_release_id TEXT,
        mb_release_track_count INTEGER,
        source_uri TEXT
    );
    CREATE INDEX tracks_playlist ON tracks(playlist_id, position);
    CREATE TABLE matches (
        track_id INTEGER PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE,
        status TEXT NOT NULL DEFAULT 'pending',
        local_path TEXT,
        candidate_json TEXT,
        confidence REAL,
        bridge_search_id INTEGER,
        download_id TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE mb_cache (
        key TEXT PRIMARY KEY,
        json TEXT NOT NULL,
        fetched_at TEXT NOT NULL
    );
    CREATE TABLE library_files (
        path TEXT PRIMARY KEY,
        mtime REAL NOT NULL,
        size INTEGER NOT NULL,
        title TEXT,
        artist TEXT,
        album TEXT,
        duration_ms INTEGER,
        isrc TEXT,
        mb_recording_id TEXT,
        mb_release_id TEXT,
        format TEXT,
        bitrate INTEGER,
        sample_rate INTEGER,
        bit_depth INTEGER,
        title_norm TEXT,
        artist_norm TEXT,
        scanned_at TEXT NOT NULL
    );
    CREATE INDEX library_isrc ON library_files(isrc);
    CREATE INDEX library_mbid ON library_files(mb_recording_id);
    CREATE INDEX library_norm ON library_files(artist_norm, title_norm);
    CREATE TABLE jobs (
        id INTEGER PRIMARY KEY,
        playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
        kind TEXT NOT NULL,
        status TEXT NOT NULL,
        started_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        progress_json TEXT,
        error TEXT
    );
    CREATE TABLE user_penalties (
        job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        username TEXT NOT NULL,
        failures INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (job_id, username)
    );
    """,
]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:

    def __init__(self, path: str | Path):
        self.path = Path(path)

        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)

        self.conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.migrate()

    def close(self):
        self.conn.close()

    # Migrations #

    def migrate(self):
        self.conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        row = self.conn.execute("SELECT version FROM schema_version").fetchone()
        current = row["version"] if row else 0

        for number, script in enumerate(MIGRATIONS, start=1):
            if number <= current:
                continue

            with self.conn:
                self.conn.execute("BEGIN")
                self.conn.executescript(script)
                self.conn.execute("DELETE FROM schema_version")
                self.conn.execute("INSERT INTO schema_version VALUES (?)", (number,))

    @property
    def schema_version(self) -> int:
        return self.conn.execute("SELECT version FROM schema_version").fetchone()["version"]

    # Playlists and tracks #

    def add_playlist(self, name, source, tracks: list[Track], source_ref=None, jspf_path=None) -> int:
        with self.conn:
            self.conn.execute("BEGIN")
            cursor = self.conn.execute(
                "INSERT INTO playlists (source, source_ref, name, imported_at, jspf_path, track_count) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (source, source_ref, name, utcnow(), str(jspf_path) if jspf_path else None, len(tracks)),
            )
            playlist_id = cursor.lastrowid
            now = utcnow()

            for position, track in enumerate(tracks):
                cursor = self.conn.execute(
                    "INSERT INTO tracks (playlist_id, position, title, artist, album, duration_ms, duration_source, "
                    "isrc, mb_recording_id, mb_release_id, mb_release_track_count, source_uri) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (playlist_id, position, track.title, track.artist or "", track.album or "", track.duration_ms,
                     "import" if track.duration_ms else None, track.isrc, track.mb_recording_id,
                     track.mb_release_id, track.mb_release_track_count, track.source_uri),
                )
                status, local_path = ("in_library", track.local_path) if track.local_path else ("pending", None)
                self.conn.execute(
                    "INSERT INTO matches (track_id, status, local_path, updated_at) VALUES (?, ?, ?, ?)",
                    (cursor.lastrowid, status, local_path, now),
                )

        return playlist_id

    def find_playlist(self, name, source=None) -> sqlite3.Row | None:
        """A playlist by (case-insensitive) name, optionally restricted to one source; the oldest wins."""
        sql, params = "SELECT * FROM playlists WHERE lower(name) = lower(?)", [name]

        if source:
            sql += " AND source = ?"
            params.append(source)

        return self.conn.execute(sql + " ORDER BY id LIMIT 1", params).fetchone()

    def append_tracks(self, playlist_id, tracks: list[Track]) -> list[int]:
        """Add tracks after the playlist's last position; returns the new track ids."""
        with self.conn:
            self.conn.execute("BEGIN")
            start = self.conn.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM tracks WHERE playlist_id = ?",
                                      (playlist_id,)).fetchone()[0]
            now = utcnow()
            ids = []

            for offset, track in enumerate(tracks):
                cursor = self.conn.execute(
                    "INSERT INTO tracks (playlist_id, position, title, artist, album, duration_ms, duration_source, "
                    "isrc, mb_recording_id, mb_release_id, mb_release_track_count, source_uri) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (playlist_id, start + offset, track.title, track.artist or "", track.album or "", track.duration_ms,
                     "import" if track.duration_ms else None, track.isrc, track.mb_recording_id,
                     track.mb_release_id, track.mb_release_track_count, track.source_uri),
                )
                status, local_path = ("in_library", track.local_path) if track.local_path else ("pending", None)
                self.conn.execute(
                    "INSERT INTO matches (track_id, status, local_path, updated_at) VALUES (?, ?, ?, ?)",
                    (cursor.lastrowid, status, local_path, now),
                )
                ids.append(cursor.lastrowid)

            self.conn.execute("UPDATE playlists SET track_count = (SELECT COUNT(*) FROM tracks WHERE playlist_id = ?) "
                              "WHERE id = ?", (playlist_id, playlist_id))

        return ids

    def relocate(self, moves: dict) -> int:
        """Point every match at the new path after files were moved ({old_path: new_path}); returns rows changed."""
        changed = 0

        for old, new in moves.items():
            changed += self.conn.execute("UPDATE matches SET local_path = ?, updated_at = ? WHERE local_path = ?",
                                         (new, utcnow(), old)).rowcount

        return changed

    def set_playlist_jspf(self, playlist_id, jspf_path):
        self.conn.execute("UPDATE playlists SET jspf_path = ? WHERE id = ?", (str(jspf_path), playlist_id))

    def get_playlist(self, playlist_id) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM playlists WHERE id = ?", (playlist_id,)).fetchone()

        if row is None:
            raise LookupError(f"unknown playlist_id {playlist_id}")

        return row

    def list_playlists(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM playlists ORDER BY id").fetchall()

    def delete_playlist(self, playlist_id):
        self.conn.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))

    def tracks(self, playlist_id, statuses=None) -> list[sqlite3.Row]:
        sql = ("SELECT t.*, m.status, m.local_path, m.candidate_json, m.confidence, m.bridge_search_id, "
               "m.download_id, m.attempts, m.last_error, m.updated_at FROM tracks t JOIN matches m ON m.track_id = t.id "
               "WHERE t.playlist_id = ?")
        params: list = [playlist_id]

        if statuses:
            sql += " AND m.status IN (%s)" % ",".join("?" * len(statuses))
            params.extend(statuses)

        return self.conn.execute(sql + " ORDER BY t.position", params).fetchall()

    def track(self, track_id) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT t.*, m.status, m.local_path, m.candidate_json, m.confidence, m.bridge_search_id, m.download_id, "
            "m.attempts, m.last_error FROM tracks t JOIN matches m ON m.track_id = t.id WHERE t.id = ?", (track_id,)
        ).fetchone()

        if row is None:
            raise LookupError(f"unknown track_id {track_id}")

        return row

    def update_track(self, track_id, **fields):
        allowed = {"duration_ms", "duration_source", "isrc", "mb_recording_id", "mb_release_id",
                   "mb_release_track_count", "title", "artist", "album"}
        bad = set(fields) - allowed

        if bad:
            raise ValueError(f"cannot update {bad}")

        if fields:
            assignments = ", ".join(f"{k} = ?" for k in fields)
            self.conn.execute(f"UPDATE tracks SET {assignments} WHERE id = ?", (*fields.values(), track_id))

    def status_counts(self, playlist_id) -> dict:
        rows = self.conn.execute(
            "SELECT m.status, COUNT(*) AS n FROM matches m JOIN tracks t ON t.id = m.track_id "
            "WHERE t.playlist_id = ? GROUP BY m.status", (playlist_id,)
        ).fetchall()
        counts = {status: 0 for status in MATCH_STATUSES}
        counts.update({row["status"]: row["n"] for row in rows})
        return counts

    # Matches #

    def set_match(self, track_id, status, *, local_path=..., candidates=..., confidence=..., bridge_search_id=...,
                  download_id=..., last_error=..., bump_attempts=False):
        if status not in MATCH_STATUSES:
            raise ValueError(f"bad status {status!r}")

        sets = ["status = ?", "updated_at = ?"]
        params: list = [status, utcnow()]

        for column, value in (("local_path", local_path), ("confidence", confidence),
                              ("bridge_search_id", bridge_search_id), ("download_id", download_id),
                              ("last_error", last_error)):
            if value is not ...:
                sets.append(f"{column} = ?")
                params.append(value)

        if candidates is not ...:
            sets.append("candidate_json = ?")
            params.append(json.dumps(candidates) if candidates is not None else None)

        if bump_attempts:
            sets.append("attempts = attempts + 1")

        params.append(track_id)
        self.conn.execute(f"UPDATE matches SET {', '.join(sets)} WHERE track_id = ?", params)

    @staticmethod
    def candidates(row) -> list[dict]:
        return json.loads(row["candidate_json"]) if row["candidate_json"] else []

    # MusicBrainz cache #

    def cache_get(self, key, max_age_s=None):
        row = self.conn.execute("SELECT json, fetched_at FROM mb_cache WHERE key = ?", (key,)).fetchone()

        if row is None:
            return None

        if max_age_s is not None:
            fetched = datetime.fromisoformat(row["fetched_at"])

            if (datetime.now(timezone.utc) - fetched).total_seconds() > max_age_s:
                return None

        return json.loads(row["json"])

    def cache_put(self, key, value):
        self.conn.execute("INSERT OR REPLACE INTO mb_cache (key, json, fetched_at) VALUES (?, ?, ?)",
                          (key, json.dumps(value), utcnow()))

    # Library #

    def library_file(self, path):
        return self.conn.execute("SELECT * FROM library_files WHERE path = ?", (path,)).fetchone()

    def library_upsert(self, **fields):
        columns = ", ".join(fields)
        placeholders = ", ".join("?" * len(fields))
        self.conn.execute(f"INSERT OR REPLACE INTO library_files ({columns}) VALUES ({placeholders})",
                          tuple(fields.values()))

    def library_prune(self, existing_paths: set[str], root: str):
        rows = self.conn.execute("SELECT path FROM library_files WHERE path LIKE ?", (root.rstrip("/") + "/%",)).fetchall()
        gone = [r["path"] for r in rows if r["path"] not in existing_paths]

        for path in gone:
            self.conn.execute("DELETE FROM library_files WHERE path = ?", (path,))

        return len(gone)

    def library_count(self):
        return self.conn.execute("SELECT COUNT(*) FROM library_files").fetchone()[0]

    def library_find(self, column, value):
        return self.conn.execute(f"SELECT * FROM library_files WHERE {column} = ?", (value,)).fetchall()

    def library_find_norm(self, artist_norm, title_norm):
        return self.conn.execute(
            "SELECT * FROM library_files WHERE artist_norm = ? AND title_norm = ?", (artist_norm, title_norm)
        ).fetchall()

    # Jobs #

    def create_job(self, playlist_id, kind, progress=None) -> int:
        now = utcnow()
        cursor = self.conn.execute(
            "INSERT INTO jobs (playlist_id, kind, status, started_at, updated_at, progress_json) VALUES (?, ?, ?, ?, ?, ?)",
            (playlist_id, kind, "running", now, now, json.dumps(progress or {})),
        )
        return cursor.lastrowid

    def update_job(self, job_id, status=None, progress=None, error=...):
        sets, params = ["updated_at = ?"], [utcnow()]

        if status is not None:
            sets.append("status = ?")
            params.append(status)

        if progress is not None:
            sets.append("progress_json = ?")
            params.append(json.dumps(progress))

        if error is not ...:
            sets.append("error = ?")
            params.append(error)

        params.append(job_id)
        self.conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id = ?", params)

    def job(self, job_id):
        return self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()

    def active_job(self, playlist_id):
        return self.conn.execute(
            "SELECT * FROM jobs WHERE playlist_id = ? AND status IN ('running', 'waiting') ORDER BY id DESC LIMIT 1",
            (playlist_id,),
        ).fetchone()

    def interrupt_running_jobs(self):
        self.conn.execute("UPDATE jobs SET status = 'interrupted', updated_at = ? WHERE status IN ('running', 'waiting')",
                          (utcnow(),))

    def penalise_user(self, job_id, username):
        self.conn.execute(
            "INSERT INTO user_penalties (job_id, username, failures) VALUES (?, ?, 1) "
            "ON CONFLICT(job_id, username) DO UPDATE SET failures = failures + 1", (job_id, username)
        )

    def user_failures(self, job_id) -> dict:
        rows = self.conn.execute("SELECT username, failures FROM user_penalties WHERE job_id = ?", (job_id,)).fetchall()
        return {r["username"]: r["failures"] for r in rows}
