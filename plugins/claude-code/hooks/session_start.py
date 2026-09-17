#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""SessionStart hook: is `flacli` on PATH, is the Nicotine+ bridge reachable, where is the library?

Asks `flacli --compact doctor` and prints one JSON object with additionalContext. Never fails the session;
stdlib only.
"""

import json
import shutil
import subprocess


def main():
    flacli = shutil.which("flacli")

    if flacli is None:
        return ("flacli: the `flacli` command is not on PATH, so the plugin's MCP servers cannot start. Install it "
                "with install.sh from the repository (after a marketplace install it is at "
                "~/.claude/plugins/marketplaces/flacli/install.sh), which also puts the MCP Bridge plugin into Nicotine+.")

    try:
        proc = subprocess.run([flacli, "--compact", "doctor"], capture_output=True, text=True, timeout=20)
        report = json.loads(proc.stdout)
    except (subprocess.TimeoutExpired, ValueError, OSError) as error:
        return f"flacli: `flacli doctor` did not answer ({type(error).__name__}); run it in a shell to see why."

    if "error" in report:
        return f"flacli: doctor failed: {report['error']}"

    nicotine = report.get("nicotine") or {}
    parts = [f"flacli {report.get('version')}, library {report.get('music_dir')}"
             + ("" if report.get("music_dir_exists") else " (MISSING: flacli config set music_dir <folder>)")
             + f", {report.get('library_files', 0)} files indexed, {report.get('playlists', 0)} playlists."]

    if nicotine.get("reachable"):
        state = "online" if nicotine.get("online") else "OFFLINE (not logged in to Soulseek)"
        parts.append(f"Nicotine+ {nicotine.get('version')} bridge protocol v{nicotine.get('protocol')}, {state}, "
                     f"user {nicotine.get('username')}.")

        if nicotine.get("warning"):
            parts.append(nicotine["warning"] + ".")
    else:
        parts.append(f"Nicotine+ bridge NOT reachable at {nicotine.get('socket')}. {nicotine.get('fix', '')}")

    services = [name for name, connected in (report.get("services") or {}).items() if connected and name != "deezer"]

    if services:
        parts.append("Connected services: " + ", ".join(services) + ".")

    return "flacli: " + " ".join(parts)


if __name__ == "__main__":
    try:
        print(json.dumps({"additionalContext": main()}))
    except Exception as error:  # noqa: BLE001 - a hook must never break the session
        print(json.dumps({"additionalContext": f"flacli session hook error: {error}"}))
