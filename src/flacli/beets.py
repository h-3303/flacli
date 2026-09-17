# SPDX-License-Identifier: GPL-3.0-or-later
"""Optional beets integration: hand finished downloads to `beet import` with MusicBrainz ids as hints.

Nothing runs unless `beet` is on PATH. The plan is always a dry run first (`beet import --pretend`), and the
real import only happens with confirm=True. Whether files are copied or moved is beets' own configuration
unless move=True is passed explicitly; flacli never changes the beets config.

Per folder of finished tracks:
  album folder (one MusicBrainz release id dominates)  -> beet import -q --search-id <release id> <folder>
  anything else                                        -> beet import -q -s --search-id <recording id>... <folder>
After the import each track's new path is looked up in beets by mb_trackid so M3U writing keeps working.
"""

import os
import shutil
import subprocess

from collections import Counter
from pathlib import Path

from . import config

TIMEOUT_S = 900


class BeetsError(Exception):
    pass


class Beets:

    def __init__(self, run=subprocess.run, which=shutil.which, env=None):
        self._run = run
        self._which = which
        self._env = env

    # Detection #

    def executable(self) -> str | None:
        return self._which("beet")

    def _beet(self, *args, timeout=60) -> subprocess.CompletedProcess:
        beet = self.executable()

        if not beet:
            raise BeetsError("beets is not installed (no `beet` on PATH); pipx install beets, or skip this step")

        try:
            return self._run([beet, *args], capture_output=True, text=True, timeout=timeout, env=self._env,
                             stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise BeetsError(f"`beet {args[0]}` did not finish within {timeout}s") from None
        except OSError as error:
            raise BeetsError(f"cannot run beet: {error}") from None

    def status(self) -> dict:
        beet = self.executable()

        if not beet:
            return {"installed": False, "note": "no `beet` on PATH; beets_import is unavailable"}

        info = {"installed": True, "executable": beet}
        version = self._beet("version")
        info["version"] = (version.stdout.strip().splitlines() or [""])[0]
        config_path = self._beet("config", "-p")
        info["config"] = config_path.stdout.strip() or None
        info["library_directory"] = None
        info["import"] = {}

        # `beet config` prints only what the user set; `-d` prints the defaults. Defaults first, user on top.
        for flags in (("config", "-d"), ("config",)):
            self._read_config(self._beet(*flags).stdout, info)

        return info

    @staticmethod
    def _read_config(dump: str, info: dict):
        section = None

        for line in dump.splitlines():
            stripped = line.strip()

            if not line.startswith(" "):
                section = stripped.rstrip(":") if stripped.endswith(":") else None

                if stripped.startswith("directory:"):
                    info["library_directory"] = stripped.partition(":")[2].strip() or None
            elif section == "import" and ":" in stripped:
                key, _, value = stripped.partition(":")

                if key in ("copy", "move", "write", "autotag", "incremental", "quiet_fallback"):
                    info["import"][key] = value.strip()

    # Planning #

    @staticmethod
    def plan(rows) -> list[dict]:
        """Group finished tracks by folder and decide album vs singleton import per folder."""
        folders: dict[str, list] = {}

        for row in rows:
            if not row["local_path"]:
                continue

            folders.setdefault(os.path.dirname(row["local_path"]), []).append(row)

        plan = []

        for folder, tracks in sorted(folders.items()):
            releases = Counter(r["mb_release_id"] for r in tracks if r["mb_release_id"])
            release_id, share = (releases.most_common(1)[0] if releases else (None, 0))
            album = release_id is not None and share >= max(2, len(tracks) * 0.6)
            entry = {
                "folder": folder, "mode": "album" if album else "singletons", "tracks": len(tracks),
                "track_ids": [r["id"] for r in tracks],
                "files": [os.path.basename(r["local_path"]) for r in tracks],
                "release_id": release_id if album else None,
                "recording_ids": [r["mb_recording_id"] for r in tracks if r["mb_recording_id"]] if not album else [],
                "missing_files": [r["local_path"] for r in tracks if not os.path.exists(r["local_path"])],
            }
            plan.append(entry)

        return plan

    @staticmethod
    def _args(entry, move: bool, pretend: bool, log: Path | None) -> list[str]:
        args = ["import", "-q"]

        if pretend:
            args.append("--pretend")
        if move:
            args.append("-m")
        if entry["mode"] == "singletons":
            args.append("-s")
        if log is not None and not pretend:
            args += ["-l", str(log)]
        for hint in ([entry["release_id"]] if entry["release_id"] else entry["recording_ids"]):
            args += ["--search-id", hint]

        args.append(entry["folder"])
        return args

    def dry_run(self, plan, move=False) -> list[dict]:
        for entry in plan:
            entry["command"] = "beet " + " ".join(self._args(entry, move, True, None))

            if entry["missing_files"]:
                entry["pretend"] = ["skipped: files missing on disk"]
                continue

            proc = self._beet(*self._args(entry, move, True, None), timeout=120)
            lines = [line for line in (proc.stdout + proc.stderr).splitlines() if line.strip()]
            entry["pretend"] = lines[:40]
            entry["pretend_exit"] = proc.returncode

        return plan

    # Import #

    def import_folders(self, plan, move=False) -> list[dict]:
        log = config.data_dir() / "beets-import.log"
        results = []

        for entry in plan:
            if entry["missing_files"]:
                results.append({**entry, "imported": False, "output": ["skipped: files missing on disk"]})
                continue

            args = self._args(entry, move, False, log)
            proc = self._beet(*args, timeout=TIMEOUT_S)
            lines = [line for line in (proc.stdout + proc.stderr).splitlines() if line.strip()]
            results.append({**entry, "command": "beet " + " ".join(args), "exit": proc.returncode,
                            "imported": proc.returncode == 0, "output": lines[-25:]})

        return results

    def path_for_recording(self, recording_id: str) -> str | None:
        """Where beets keeps the item tagged with this MusicBrainz recording id, if it imported it."""
        if not recording_id:
            return None

        proc = self._beet("ls", "-f", "$path", f"mb_trackid:{recording_id}")
        paths = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        return paths[0] if proc.returncode == 0 and paths else None
