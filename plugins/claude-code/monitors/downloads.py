#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Download-completion monitor: polls the Nicotine+ MCP Bridge and prints one line per transfer that finishes
or fails after the monitor started. Silent otherwise. Stdlib only; never exits on its own.

Started by Claude Code as a plugin monitor (monitors/monitors.json) the first time playlist-sync is invoked.
Every stdout line becomes a notification in the session, so lines are rare and self-contained. When the
transfer belongs to a flacli playlist (its download id is in state.db) the line names the playlist so the
session knows to call sync_downloads.

The data dir and the bridge socket come from `flacli --compact config` (the same settings the servers use);
$NICOTINE_MCP_SOCKET still wins, and the default socket locations are the fallback.
"""

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time

POLL_S = float(os.environ.get("FLACLI_MONITOR_POLL_S") or 20)
# Mirrors flacli.server.LIVE_TRANSFER_STATUSES: anything else is a failure, a peer's refusal ("File not shared.",
# "Banned", ...) included, since Nicotine+ stores the reason as the status and never retries it.
LIVE = {"Queued", "Getting status", "Transferring", "Paused", "Finished"}


def settings():
    """{'data_dir': ..., 'bridge_socket': ...} from flacli, or {} when it cannot answer."""
    flacli = shutil.which("flacli")

    if flacli is None:
        return {}

    try:
        proc = subprocess.run([flacli, "--compact", "config"], capture_output=True, text=True, timeout=15)
        values = json.loads(proc.stdout)["settings"]
        return {key: values[key]["value"] for key in ("data_dir", "bridge_socket") if key in values}
    except (subprocess.TimeoutExpired, ValueError, KeyError, OSError):
        return {}


def socket_candidates(configured):
    if os.environ.get("NICOTINE_MCP_SOCKET"):
        return [os.environ["NICOTINE_MCP_SOCKET"]]

    candidates = [configured] if configured else []
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/nicotine-mcp-{os.getuid()}"
    candidates.append(os.path.join(runtime_dir, "nicotine-mcp.sock"))
    candidates.append(os.path.join(runtime_dir, "app", "org.nicotine_plus.Nicotine", "nicotine-mcp.sock"))
    return list(dict.fromkeys(candidates))


def rpc(path, method, **params):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(10)
        conn.connect(path)
        conn.sendall((json.dumps({"method": method, "params": params}) + "\n").encode("utf-8"))
        data = b""

        while b"\n" not in data:
            chunk = conn.recv(1 << 20)

            if not chunk:
                break

            data += chunk

    response = json.loads(data.split(b"\n", 1)[0])

    if not response.get("ok"):
        raise RuntimeError(response.get("error") or "bridge error")

    return response["result"]


def list_downloads(candidates):
    last_error = None

    for path in candidates:
        try:
            return path, rpc(path, "list_downloads", limit=2000)["downloads"]
        except (OSError, RuntimeError, ValueError) as error:
            last_error = error

    raise OSError(str(last_error) if last_error else "no socket")


def playlist_of(data_dir, download_id):
    """Playlist id and name for a flacli transfer, or None. Read-only; tolerant of a missing db."""
    if not data_dir:
        return None

    db = os.path.join(data_dir, "state.db")

    if not os.path.exists(db):
        return None

    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)

        try:
            row = conn.execute(
                "SELECT p.id, p.name FROM matches m JOIN tracks t ON t.id = m.track_id JOIN playlists p ON p.id = t.playlist_id "
                "WHERE m.download_id = ? LIMIT 1", (download_id,)
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None

    return {"id": row[0], "name": row[1]} if row else None


def emit(line):
    print(line, flush=True)


def describe(transfer, data_dir):
    name = transfer["path"].rpartition("\\")[2]
    playlist = playlist_of(data_dir, transfer["download_id"])

    if playlist:
        suffix = f" (playlist {playlist['id']} \"{playlist['name']}\": call sync_downloads)"
    elif transfer["status"] == "Finished":
        suffix = " (not from a playlist: tidy_new files it)"
    else:
        suffix = ""

    return name, suffix


class Monitor:
    """One poll at a time; remembers every transfer's last status so only changes are printed."""

    def __init__(self, data_dir, candidates):
        self.data_dir = data_dir
        self.candidates = candidates
        self.known = None            # download_id -> status, seeded silently on the first successful poll
        self.reachable = None
        self.announced_idle = True

    def poll(self):
        try:
            path, downloads = list_downloads(self.candidates)
        except OSError:
            if self.reachable:
                emit("flacli: Nicotine+ bridge no longer reachable; download monitor waiting for it to come back")

            self.reachable = False
            return

        if self.reachable is False:
            emit(f"flacli: Nicotine+ bridge back at {path}")

        self.reachable = True
        current = {d["download_id"]: d for d in downloads}

        if self.known is not None:
            for download_id, transfer in current.items():
                before = self.known.get(download_id)
                status = transfer["status"]

                if status == before:
                    continue

                if status == "Finished":
                    name, suffix = describe(transfer, self.data_dir)
                    emit(f"flacli: finished {name!r} from {transfer['user']} -> {transfer.get('folder') or 'download folder'}{suffix}")
                elif status not in LIVE and (before is None or before in LIVE):
                    name, suffix = describe(transfer, self.data_dir)
                    emit(f"flacli: {status.lower()}: {name!r} from {transfer['user']}{suffix}")

        active = [d for d in current.values() if d["status"] in LIVE and d["status"] != "Finished"]

        if self.known is not None and not active and not self.announced_idle:
            emit("flacli: no downloads in progress; sync_downloads will settle the playlist state")
            self.announced_idle = True
        elif active:
            self.announced_idle = False

        self.known = {download_id: d["status"] for download_id, d in current.items()}


def run(poll_s=POLL_S):
    conf = settings()
    monitor = Monitor(conf.get("data_dir"), socket_candidates(conf.get("bridge_socket")))

    while True:
        monitor.poll()
        time.sleep(poll_s)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        pass
    except Exception as error:  # noqa: BLE001 - a monitor must not spam; one line and stop
        emit(f"flacli: download monitor stopped: {error}")
        sys.exit(1)
