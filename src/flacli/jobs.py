# SPDX-License-Identifier: GPL-3.0-or-later
"""Background jobs as detached worker processes.

A CLI call has to return within an agent's tool timeout, but matching a playlist on Soulseek takes minutes.
So `flacli sync` and `flacli get` create the job row, spawn `python -m flacli.worker`, and return at once;
progress lives in the jobs table and `flacli status` reads it. The worker's pid is stored in the job's
progress so a later call can tell a live job from one whose worker died.
"""

import json
import os
import signal
import subprocess
import sys
import time

from . import config
from .db import Database

STALE_AFTER_S = 15 * 60   # a running job without a pid (in-process MCP job) and no update for this long is dead


def alive(pid) -> bool:
    if not pid:
        return False

    try:
        os.kill(int(pid), 0)
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True

    return True


def spawn(db: Database, job_id: int, kind: str, playlist_id: int, payload: dict) -> int:
    """Start a worker for an existing job row; returns its pid."""
    logs = config.logs_dir()
    logs.mkdir(parents=True, exist_ok=True)
    log = open(logs / f"job-{job_id}.log", "ab")
    command = [sys.executable, "-m", "flacli.worker", kind, str(playlist_id), str(job_id), json.dumps(payload)]
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                               start_new_session=True, close_fds=True)
    log.close()
    row = db.job(job_id)
    progress = json.loads(row["progress_json"] or "{}")
    progress["pid"] = process.pid
    db.update_job(job_id, progress=progress)
    return process.pid


def reap_stale(db: Database) -> int:
    """Mark running jobs whose worker is gone as interrupted. Returns how many were marked."""
    marked = 0

    for row in db.conn.execute("SELECT * FROM jobs WHERE status IN ('running', 'waiting')").fetchall():
        progress = json.loads(row["progress_json"] or "{}")
        pid = progress.get("pid")

        if pid:
            dead = not alive(pid)
        else:
            updated = row["updated_at"] or row["started_at"] or ""
            dead = _age_seconds(updated) > STALE_AFTER_S

        if dead:
            db.update_job(row["id"], status="interrupted", progress=progress, error="worker process is gone")
            marked += 1

    return marked


def _age_seconds(iso: str) -> float:
    try:
        from datetime import datetime, timezone
        stamp = datetime.fromisoformat(iso.replace("Z", "+00:00"))

        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)

        return time.time() - stamp.timestamp()
    except ValueError:
        return 0.0


def cancel(db: Database, playlist_id: int) -> dict:
    """Stop the active job of a playlist: signal its worker, or mark an orphaned job cancelled."""
    row = db.active_job(playlist_id)

    if row is None:
        raise ValueError(f"no running job for playlist {playlist_id}")

    progress = json.loads(row["progress_json"] or "{}")
    pid = progress.get("pid")

    if pid and alive(pid):
        os.kill(int(pid), signal.SIGTERM)
        return {"cancelled": True, "playlist_id": playlist_id, "job_id": row["id"], "pid": pid}

    db.update_job(row["id"], status="cancelled", progress=progress)
    return {"cancelled": True, "playlist_id": playlist_id, "job_id": row["id"], "note": "worker was already gone"}


def describe(row) -> dict | None:
    """A job row as the status commands report it."""
    if row is None:
        return None

    progress = json.loads(row["progress_json"] or "{}")
    result = {"job_id": row["id"], "kind": row["kind"], "status": row["status"], "started_at": row["started_at"],
              "updated_at": row["updated_at"], **progress}

    if row["error"]:
        result["error"] = row["error"]
    if progress.get("waiting_rate_limit_until"):
        result["note"] = f"waiting on the Soulseek search rate limit, ~{max(0, round(progress['waiting_rate_limit_until'] - time.time()))} s"
    if row["status"] in ("running", "waiting") and progress.get("pid"):
        result["worker_alive"] = alive(progress["pid"])

    return result
