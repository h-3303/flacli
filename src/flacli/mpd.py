# SPDX-License-Identifier: GPL-3.0-or-later
"""Tell the player's MPD what flacli changed, so new files and playlists show up without a manual rescan.

Two things, both best effort: a scoped `update` of the folders that just received files, and a playlist saved as an
MPD stored playlist (`playlistclear` + `playlistadd`) so it appears in Flaclify, or any MPD client, under its own
name. Every public function returns a dict saying what happened and never raises: an unreachable MPD is a
`skipped` field, not a failure of the download or the tidy that called it.

The `mpd` setting picks the server: "" (auto: $MPD_HOST / $MPD_PORT, then the usual local sockets, then
localhost:6600), "off", a socket path, or [password@]host[:port]. Stdlib only.
"""

import os
import socket
import time

from pathlib import Path

from . import config

DEFAULT_PORT = 6600
CONNECT_TIMEOUT_S = 3.0
UPDATE_WAIT_S = 30.0
MAX_SCOPED_UPDATES = 25          # more folders than this: one update of the whole library instead
SOCKET_CANDIDATES = ("{runtime}/mpd/socket", "~/.config/mpd/socket", "/run/mpd/socket")


class MpdError(Exception):
    pass


# Addresses #

def parse_address(text: str) -> tuple[str, str | None]:
    """'[password@]target' -> (target, password); target is a socket path or host[:port]."""
    password = None

    if "@" in text:
        password, _, text = text.rpartition("@")

    return text.strip(), password or None


def candidates() -> list[tuple[str, str | None]]:
    """Addresses to try, in order, from the setting or the environment. Empty when MPD is off."""
    setting = (config.get("mpd") or "").strip()

    if setting.lower() == "off":
        return []

    if setting:
        return [parse_address(setting)]

    host = os.environ.get("MPD_HOST")

    if host:
        target, password = parse_address(host)

        if not target.startswith(("/", "~")) and os.environ.get("MPD_PORT"):
            target = f"{target}:{os.environ['MPD_PORT']}"

        return [(target, password)]

    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    found = [(os.path.expanduser(c.format(runtime=runtime)), None) for c in SOCKET_CANDIDATES]
    found = [(path, None) for path, _ in found if os.path.exists(path)]
    return found + [(f"localhost:{DEFAULT_PORT}", None)]


def _quote(value: str) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


# The protocol #

class Mpd:
    """One MPD connection. Commands return the response as a list of (key, value) pairs; ACK raises MpdError."""

    def __init__(self, sock: socket.socket, address: str, version: str):
        self.sock = sock
        self.address = address
        self.version = version
        self._file = sock.makefile("rwb")

    @classmethod
    def connect(cls, address: str, password: str | None = None, timeout: float = CONNECT_TIMEOUT_S) -> "Mpd":
        target = os.path.expanduser(address)

        try:
            if target.startswith("/"):
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(timeout)
                sock.connect(target)
            else:
                host, _, port = target.rpartition(":") if ":" in target and not target.endswith("]") else (target, "", "")
                sock = socket.create_connection((host or target, int(port or DEFAULT_PORT)), timeout=timeout)
                sock.settimeout(timeout)

            greeting = sock.makefile("rb").readline().decode("utf-8", "replace").strip()
        except (OSError, ValueError) as error:
            raise MpdError(f"cannot connect to MPD at {address}: {error}") from None

        if not greeting.startswith("OK MPD"):
            sock.close()
            raise MpdError(f"{address} is not MPD (greeting {greeting!r})")

        client = cls(sock, address, greeting[len("OK MPD "):])

        if password:
            client.command("password", password)

        return client

    @classmethod
    def connect_any(cls) -> "Mpd":
        """The first address in candidates() that answers. MpdError names every attempt when none does."""
        errors = []

        for address, password in candidates():
            try:
                return cls.connect(address, password)
            except MpdError as error:
                errors.append(str(error))

        raise MpdError("; ".join(errors) if errors else "mpd is off")

    def command(self, name: str, *args) -> list[tuple[str, str]]:
        line = " ".join([name] + [_quote(a) for a in args])
        self.sock.settimeout(max(CONNECT_TIMEOUT_S, UPDATE_WAIT_S))

        try:
            self._file.write((line + "\n").encode("utf-8"))
            self._file.flush()
            pairs = []

            while True:
                raw = self._file.readline()

                if not raw:
                    raise MpdError("MPD closed the connection")

                text = raw.decode("utf-8", "replace").rstrip("\n")

                if text == "OK":
                    return pairs
                if text.startswith("ACK "):
                    raise MpdError(text[4:])

                key, _, value = text.partition(": ")
                pairs.append((key, value))
        except OSError as error:
            raise MpdError(f"MPD connection error: {error}") from None

    def close(self):
        try:
            self._file.write(b"close\n")
            self._file.flush()
        except (OSError, ValueError):
            pass

        self.sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    # Queries #

    def music_directory(self) -> Path | None:
        """MPD's music_directory, only known over a local socket (the `config` command is refused on TCP)."""
        try:
            pairs = dict(self.command("config"))
        except MpdError:
            return None

        value = pairs.get("music_directory")
        return Path(value).expanduser() if value else None

    def status(self) -> dict:
        return dict(self.command("status"))

    def playlist_names(self) -> list[str]:
        return [value for key, value in self.command("listplaylists") if key == "playlist"]

    def update(self, uris: list[str]) -> int | None:
        """`update` each uri ("" = the whole library); returns the last job id."""
        job = None

        for uri in uris or [""]:
            pairs = dict(self.command("update", uri) if uri else self.command("update"))
            job = int(pairs.get("updating_db", job or 0)) or job

        return job

    def wait_update(self, timeout: float = UPDATE_WAIT_S) -> bool:
        """True once no database update is running (or none was), False on timeout."""
        deadline = time.monotonic() + timeout

        while "updating_db" in self.status():
            if time.monotonic() > deadline:
                return False

            time.sleep(0.25)

        return True


# Mapping paths to MPD uris #

def _resolved(path) -> Path:
    return Path(os.path.expanduser(str(path))).resolve()


def playlist_name(name: str) -> str:
    """MPD forbids '/', newlines and the names '.' and '..'."""
    cleaned = "".join("_" if c in "/\n\r" else c for c in name).strip()
    return cleaned if cleaned not in ("", ".", "..") else "playlist"


def map_uris(client: Mpd, paths: list[str]) -> tuple[Path, dict[str, str], list[str]]:
    """(root, {path: uri}, outside): uris relative to MPD's music_directory, else to flacli's music_dir."""
    root = client.music_directory() or config.music_dir()
    root = _resolved(root)
    inside, outside = {}, []

    for path in paths:
        try:
            uri = _resolved(path).relative_to(root).as_posix()
        except ValueError:
            outside.append(str(path))
            continue

        inside[str(path)] = "" if uri == "." else uri

    return root, inside, outside


def _folders(paths: list[str]) -> list[str]:
    folders = set()

    for path in paths:
        folders.add(str(path) if os.path.isdir(path) else os.path.dirname(str(path)))

    return sorted(folders)


# What the rest of flacli calls #

def probe() -> dict:
    """Reachability and the music directory agreement, for doctor and `flacli mpd`."""
    if not candidates():
        return {"reachable": False, "skipped": "mpd is off (config set mpd '' to detect it again)"}

    try:
        with Mpd.connect_any() as client:
            directory = client.music_directory()
            status = client.status()
            result = {"reachable": True, "address": client.address, "version": client.version,
                      "music_directory": str(directory) if directory else None,
                      "playlists": len(client.playlist_names()), "updating": "updating_db" in status}
    except MpdError as error:
        return {"reachable": False, "error": str(error), "tried": [address for address, _ in candidates()],
                "fix": "start MPD, or config set mpd <socket path | host:port>, or config set mpd off"}

    music_dir = _resolved(config.music_dir())

    if directory is None:
        result["note"] = "music_directory unknown over TCP; flacli assumes it is its own music_dir"
    elif _resolved(directory) == music_dir:
        result["same_library"] = True
    else:
        try:
            music_dir.relative_to(_resolved(directory))
            result["same_library"] = True
            result["note"] = f"flacli's music_dir is inside MPD's library at {music_dir.relative_to(_resolved(directory))}"
        except ValueError:
            result["same_library"] = False
            result["note"] = f"MPD serves {directory}, flacli files into {music_dir}: updates and playlists are skipped"

    return result


def notify_paths(paths: list[str], wait: bool = False) -> dict:
    """Ask MPD to update the folders holding these files (or these folders). Returns what was sent."""
    if not candidates():
        return {"skipped": "mpd is off"}

    if not paths:
        return {"skipped": "nothing to update"}

    try:
        with Mpd.connect_any() as client:
            _, inside, outside = map_uris(client, _folders(paths))
            uris = sorted(set(inside.values()))

            if not uris:
                return {"skipped": f"outside MPD's music directory: {outside[:5]}"}

            if len(uris) > MAX_SCOPED_UPDATES:
                uris = [""]

            job = client.update(uris)
            result = {"address": client.address, "updated": uris or [""], "job": job}

            if wait:
                result["finished"] = client.wait_update()

            if outside:
                result["outside"] = outside[:20]

            return result
    except MpdError as error:
        return {"skipped": str(error)}


def save_playlist(name: str, paths: list[str]) -> dict:
    """Store the playlist in MPD as `name` with these files, in order. Files MPD does not know yet get their
    folders updated first, then one retry; the rest are reported in not_in_db."""
    if not candidates():
        return {"skipped": "mpd is off"}

    if not paths:
        return {"skipped": "no local files yet"}

    stored = playlist_name(name)

    try:
        with Mpd.connect_any() as client:
            _, inside, outside = map_uris(client, paths)

            if not inside:
                return {"skipped": f"outside MPD's music directory: {outside[:5]}"}

            def add_all(items: list[tuple[str, str]]) -> list[tuple[str, str]]:
                missing = []

                for path, uri in items:
                    try:
                        client.command("playlistadd", stored, uri)
                    except MpdError as error:
                        if "not found" in str(error).lower() or "no such" in str(error).lower():
                            missing.append((path, uri))
                        else:
                            raise

                return missing

            if stored in client.playlist_names():
                client.command("playlistclear", stored)

            items = [(path, uri) for path, uri in inside.items()]
            missing = add_all(items)
            updated = False

            if missing:
                client.update(sorted({os.path.dirname(uri) for _, uri in missing}))
                updated = client.wait_update()

                # start again so the order stays the playlist's own
                if stored in client.playlist_names():
                    client.command("playlistclear", stored)

                missing = add_all(items)

            result = {"address": client.address, "playlist": stored, "added": len(items) - len(missing),
                      "not_in_db": [path for path, _ in missing][:50], "updated": updated}

            if outside:
                result["outside"] = outside[:20]

            return result
    except MpdError as error:
        return {"skipped": str(error)}
