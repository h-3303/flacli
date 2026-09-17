# SPDX-License-Identifier: GPL-3.0-or-later
"""Detached job worker: `python -m flacli.worker <kind> <playlist_id> <job_id> <payload json>`.

kind "match": search Soulseek for the playlist's pending tracks and store candidates (nothing queued).
kind "request": match the given track ids, approve every candidate at or above min_confidence, queue them.
The job row is created by the caller; this process only runs it and records progress. SIGTERM cancels.
"""

import asyncio
import json
import signal
import sys

from . import config, server
from .bridge import BridgeClient
from .db import Database
from .matcher import MatchJob, MatchPrefs

PREF_FIELDS = ("prefer_formats", "allow_formats", "min_bitrate", "duration_tolerance_s", "album_mode", "harvest_seconds",
               "max_tracks", "poll_interval_s")


def prefs_from(payload: dict) -> MatchPrefs:
    prefs = MatchPrefs()

    for key, value in (payload.get("prefs") or {}).items():
        if key in PREF_FIELDS and value is not None:
            setattr(prefs, key, value)

    prefs.harvest_seconds = max(0.1, float(prefs.harvest_seconds))
    prefs.poll_interval_s = min(prefs.poll_interval_s, prefs.harvest_seconds)
    return prefs


def prefs_to(prefs: MatchPrefs) -> dict:
    return {key: getattr(prefs, key) for key in PREF_FIELDS}


async def run(kind: str, playlist_id: int, job_id: int, payload: dict):
    server.State.db = Database(config.db_path())
    server.State.bridge = BridgeClient()
    prefs = prefs_from(payload)

    if kind == "request":
        coroutine = server._request_job(job_id, playlist_id, payload["track_ids"], prefs, float(payload.get("min_confidence", 0.85)))
    elif kind == "match":
        coroutine = MatchJob(server.db(), server.bridge(), prefs).run(job_id, playlist_id, payload.get("track_ids"))
    else:
        raise SystemExit(f"unknown job kind {kind!r}")

    task = asyncio.ensure_future(coroutine)
    loop = asyncio.get_running_loop()

    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, task.cancel)

    try:
        await task
    except asyncio.CancelledError:
        pass
    finally:
        server.State.db.close()


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)

    if len(args) != 4:
        raise SystemExit("usage: python -m flacli.worker <match|request> <playlist_id> <job_id> <payload json>")

    kind, playlist_id, job_id, payload = args[0], int(args[1]), int(args[2]), json.loads(args[3])
    asyncio.run(run(kind, playlist_id, job_id, payload))


if __name__ == "__main__":
    main()
