# SPDX-License-Identifier: GPL-3.0-or-later
"""Optional Troi (ListenBrainz) content resolver: find playlist tracks in a MusicBrainz-tagged collection.

Troi keeps its own SQLite index of the collection (<data>/troi.db). `troi db scan` reads MusicBrainz ids from
tags, so this only helps for a library tagged by Picard or beets. Resolution is by recording MBID first, then
fuzzy artist + title (needs nmslib). flacli feeds it a JSPF of the tracks still missing and reads the
M3U it writes back; matched files become `in_library` rows. Nothing is written to the collection.

Command: `troi` on PATH. Install it in its own environment (`pipx install troi && pipx inject troi nmslib`):
troi pins an older mutagen than this server uses, so it cannot live in the library venv. Without nmslib
Troi resolves nothing at all, not even exact MBIDs (its fuzzy index short-circuits the whole match list).
"""

import json
import os
import shutil
import subprocess

from pathlib import Path

from . import config

TRACK_EXT = "https://musicbrainz.org/doc/jspf#track"
SCAN_TIMEOUT_S = 3600
RESOLVE_TIMEOUT_S = 600


class TroiError(Exception):
    pass


class Troi:

    def __init__(self, run=subprocess.run, which=shutil.which, db_file: Path | None = None):
        self._run = run
        self._which = which
        self.db_file = db_file or config.data_dir() / "troi.db"

    # Detection #

    def command(self) -> list[str] | None:
        executable = self._which("troi")
        return [executable] if executable else None

    def status(self) -> dict:
        command = self.command()
        info = {"installed": command is not None, "db_file": str(self.db_file), "indexed": self.db_file.is_file()}

        if command is None:
            info["note"] = ("Troi is not installed: `pipx install troi && pipx inject troi nmslib`. "
                            "It only helps when the collection carries MusicBrainz tags.")
            return info

        info["command"] = " ".join(command)

        if self.db_file.is_file():
            info["indexed_at"] = os.path.getmtime(self.db_file)

        return info

    def _troi(self, *args, timeout) -> subprocess.CompletedProcess:
        command = self.command()

        if command is None:
            raise TroiError(self.status()["note"])

        try:
            return self._run([*command, *args], capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise TroiError(f"`troi {args[0]}` did not finish within {timeout}s") from None
        except OSError as error:
            raise TroiError(f"cannot run troi: {error}") from None

    @staticmethod
    def _check(proc, what):
        if proc.returncode != 0:
            tail = [line for line in (proc.stderr + proc.stdout).splitlines() if line.strip()][-5:]
            raise TroiError(f"troi {what} failed (exit {proc.returncode}): " + " | ".join(tail))

    # Index #

    def scan(self, music_dir: Path, force=False) -> dict:
        if self.command() is None:
            raise TroiError(self.status()["note"])

        if not music_dir.is_dir():
            raise TroiError(f"{music_dir} is not a directory")

        self.db_file.parent.mkdir(parents=True, exist_ok=True)
        created = False

        if not self.db_file.is_file():
            self._check(self._troi("db", "create", "-d", str(self.db_file), "-q", timeout=120), "db create")
            created = True

        args = ["db", "scan", "-d", str(self.db_file), "-q"] + (["-f"] if force else []) + [str(music_dir)]
        proc = self._troi(*args, timeout=SCAN_TIMEOUT_S)
        self._check(proc, "db scan")
        return {"db_file": str(self.db_file), "created": created, "music_dir": str(music_dir),
                "output": [line for line in proc.stdout.splitlines() if line.strip()][-5:]}

    # Resolve #

    @staticmethod
    def write_query(path: Path, rows) -> int:
        """JSPF in Troi's dialect: every track carries the MusicBrainz extension and a recording identifier when known."""
        tracks = []

        for row in rows:
            item = {"title": row["title"], "creator": row["artist"], "extension": {TRACK_EXT: {}}}

            if row["mb_recording_id"]:
                item["identifier"] = [f"https://musicbrainz.org/recording/{row['mb_recording_id']}"]
            else:
                item["identifier"] = []
            if row["album"]:
                item["album"] = row["album"]

            tracks.append(item)

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"playlist": {"title": "flacli query", "track": tracks}}, ensure_ascii=False))
        return len(tracks)

    @staticmethod
    def parse_m3u(text: str) -> list[tuple[str, str]]:
        """(title, path) pairs from Troi's M3U: '#EXTINF 0,Title' lines followed by an absolute path."""
        pairs = []
        title = None

        for line in text.splitlines():
            line = line.rstrip("\r")

            if line.startswith("#EXTINF"):
                title = line.partition(",")[2]
            elif line and not line.startswith("#"):
                pairs.append((title or "", line))
                title = None

        return pairs

    def resolve(self, rows, threshold: float) -> dict:
        """rows: track rows to look for. Returns {track_id: local_path} for the ones Troi found on disk."""
        if not self.db_file.is_file():
            raise TroiError("Troi has not indexed the collection yet; call troi_scan first")

        if not 0.0 <= threshold <= 1.0:
            raise ValueError("threshold must be between 0 and 1")

        work = config.data_dir() / "troi"
        query = work / "query.jspf"
        output = work / "resolved.m3u"
        self.write_query(query, rows)

        if output.exists():
            output.unlink()

        proc = self._troi("resolve", "-d", str(self.db_file), "-t", str(threshold), "-m", str(output), "-y", "-q", str(query),
                          timeout=RESOLVE_TIMEOUT_S)

        if proc.returncode != 0 and not output.exists():
            if "no tracks could be resolved" in (proc.stdout + proc.stderr).lower() or "empty playlist" in (proc.stdout + proc.stderr).lower():
                return {}

            self._check(proc, "resolve")

        if not output.exists():
            return {}

        pairs = self.parse_m3u(output.read_text(encoding="utf-8", errors="replace"))
        pending = list(rows)
        found = {}

        for title, path in pairs:
            match = next((r for r in pending if r["title"] == title), None) or next((r for r in pending), None)

            if match is None:
                break

            pending.remove(match)

            if os.path.isfile(path):
                found[match["id"]] = path

        return found
