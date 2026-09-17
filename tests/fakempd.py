"""A fake MPD on a Unix socket: enough of the protocol for flacli.mpd (config, status, update, listplaylists,
playlistclear, playlistadd, password, close). Its "database" is the set of audio files under `root` at the time of
the last `update`, so a file MPD has not been told about is "No such song" until then."""

import os
import socket
import threading

from pathlib import Path

AUDIO = {".flac", ".mp3", ".ogg", ".opus", ".m4a", ".wav"}


class FakeMpd:
    def __init__(self, root: Path, socket_path: Path, password: str | None = None, refuse_config: bool = False):
        self.root = Path(root)
        self.socket_path = Path(socket_path)
        self.password = password
        self.refuse_config = refuse_config
        self.known: set[str] = set()
        self.playlists: dict[str, list[str]] = {}
        self.commands: list[str] = []
        self.updates = 0
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(self.socket_path))
        self._server.listen(4)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._stop = False

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop = True

        try:
            socket.socket(socket.AF_UNIX, socket.SOCK_STREAM).connect(str(self.socket_path))
        except OSError:
            pass

        self._server.close()
        self.socket_path.unlink(missing_ok=True)

    # the "database"
    def scan(self, uri: str = ""):
        base = self.root / uri if uri else self.root

        if base.is_file():
            self.known.add(base.relative_to(self.root).as_posix())
            return

        for path in base.rglob("*"):
            if path.suffix.lower() in AUDIO and path.is_file():
                self.known.add(path.relative_to(self.root).as_posix())

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return

            threading.Thread(target=self._session, args=(conn,), daemon=True).start()

    def _session(self, conn: socket.socket):
        stream = conn.makefile("rwb")
        authed = self.password is None

        try:
            stream.write(b"OK MPD 0.24.0\n")
            stream.flush()
        except OSError:   # the stop() nudge, or a client that gave up
            conn.close()
            return

        while True:
            try:
                raw = stream.readline()
            except OSError:
                break

            if not raw:
                break

            line = raw.decode("utf-8").rstrip("\n")
            name, args = _parse(line)
            self.commands.append(line)
            out: list[str] = []
            ack = None

            if name == "close":
                break
            elif name == "password":
                if args and args[0] == self.password:
                    authed = True
                else:
                    ack = "ACK [3@0] {password} incorrect password"
            elif not authed:
                ack = f"ACK [4@0] {{{name}}} you don't have permission for \"{name}\""
            elif name == "config":
                if self.refuse_config:
                    ack = "ACK [4@0] {config} you don't have permission for \"config\""
                else:
                    out.append(f"music_directory: {self.root}")
            elif name == "status":
                out += ["volume: 50", "state: stop", "playlist: 1"]
            elif name == "update":
                self.updates += 1
                self.scan(args[0] if args else "")
                out.append(f"updating_db: {self.updates}")
            elif name == "listplaylists":
                for playlist in self.playlists:
                    out += [f"playlist: {playlist}", "Last-Modified: 2026-01-01T00:00:00Z"]
            elif name == "playlistclear":
                if args[0] in self.playlists:
                    self.playlists[args[0]] = []
                else:
                    ack = "ACK [50@0] {playlistclear} No such playlist"
            elif name == "playlistadd":
                uri = args[1]

                if uri in self.known:
                    self.playlists.setdefault(args[0], []).append(uri)
                else:
                    ack = "ACK [50@0] {playlistadd} No such song"
            else:
                ack = f'ACK [5@0] {{}} unknown command "{name}"'

            try:
                stream.write(("\n".join(out + [ack or "OK"]) + "\n").encode("utf-8"))
                stream.flush()
            except OSError:
                break

        conn.close()


def _parse(line: str) -> tuple[str, list[str]]:
    """MPD quoting: words, or "double quoted" with backslash escapes."""
    words, current, quoted, escaped = [], "", False, False
    started = False

    for char in line:
        if escaped:
            current += char
            escaped = False
        elif char == "\\" and quoted:
            escaped = True
        elif char == '"':
            quoted = not quoted
            started = True
        elif char == " " and not quoted:
            if started or current:
                words.append(current)

            current, started = "", False
        else:
            current += char
            started = True

    if started or current:
        words.append(current)

    return (words[0] if words else ""), words[1:]
