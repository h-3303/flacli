# SPDX-License-Identifier: GPL-3.0-or-later
"""YouTube Music through ytmusicapi (unofficial; browser-header auth). Treated as fragile: the library is
imported lazily, its version is reported, and every failure is wrapped with a plain explanation.

Auth file: ytmusicapi.setup(filepath, headers_raw) writes browser.json; kept 0600 under the auth dir.
"""

import importlib.metadata
import os
import re
import shutil
import urllib.parse

from pathlib import Path

from .. import config
from ..models import ImportedPlaylist, Track
from . import Connector, ConnectorError

MIN_VERSION = (1, 8)
PLAYLIST_ID = re.compile(r"^(?:VL)?([A-Za-z0-9_-]{2,})$")     # "LM" is Liked Music


def playlist_id_from_ref(ref: str) -> str:
    text = (ref or "").strip()

    if "list=" in text:
        text = (urllib.parse.parse_qs(urllib.parse.urlparse(text).query).get("list") or [""])[0]

    match = PLAYLIST_ID.match(text)

    if not match:
        raise ConnectorError(f"{ref!r} is not a YouTube Music playlist id or URL (expected PL…/VL… or a music.youtube.com/playlist?list=… link)")

    return match.group(1)


def _library():
    try:
        import ytmusicapi
    except ImportError:
        raise ConnectorError("ytmusicapi is not installed in the library server's environment; run `uv sync` "
                             "in flacli's environment (uv tool install --force .) and try again") from None

    try:
        version = importlib.metadata.version("ytmusicapi")
    except importlib.metadata.PackageNotFoundError:
        version = "0"

    parts = tuple(int(p) for p in re.findall(r"\d+", version)[:2]) or (0,)

    if parts < MIN_VERSION:
        raise ConnectorError(f"ytmusicapi {version} is too old; {'.'.join(map(str, MIN_VERSION))}+ is needed")

    return ytmusicapi, version


class YouTubeMusicConnector(Connector):
    key = "youtube-music"
    label = "YouTube Music"

    def __init__(self, store_dir: Path | None = None, client_factory=None):
        self.auth_path = (store_dir or config.auth_dir()) / "youtube-music.json"
        self._client_factory = client_factory
        self._client = None

    def status(self) -> dict:
        info = {"service": self.key, "connected": self.auth_path.is_file(), "auth_file": str(self.auth_path)}

        try:
            _, info["ytmusicapi_version"] = _library()
        except ConnectorError as error:
            info["ytmusicapi_version"] = None
            info["problem"] = str(error)

        if not info["connected"]:
            info["how_to_connect"] = (
                "In a logged-in browser on music.youtube.com open the developer tools, Network tab, filter "
                "'/browse', pick a POST request with status 200 and copy its request headers (Firefox: right-click "
                "→ Copy Request Headers; Chrome: Copy → Copy as cURL, Firefox format). Then call "
                "connect_service('youtube-music', headers_raw=<that text>). Alternatively pass auth_file=<path to an "
                "existing ytmusicapi browser.json>. Cookies stay local, 0600, under the flacli data dir."
            )

        return info

    def connect(self, headers_raw: str | None = None, auth_file: str | None = None, **kwargs) -> dict:
        if auth_file:
            source = Path(auth_file).expanduser()

            if not source.is_file():
                raise ConnectorError(f"{source} does not exist")

            shutil.copyfile(source, self.auth_path)
        elif headers_raw and headers_raw.strip():
            ytmusicapi, _ = _library()

            try:
                ytmusicapi.setup(filepath=str(self.auth_path), headers_raw=headers_raw)
            except Exception as error:
                raise ConnectorError(f"ytmusicapi could not read those headers: {error}") from None
        elif self.auth_path.is_file():
            return {"service": self.key, "connected": True, "note": "already connected; pass headers_raw to replace the cookies"}
        else:
            raise ConnectorError(self.status()["how_to_connect"])

        os.chmod(self.auth_path, 0o600)
        self._client = None
        return {"service": self.key, "connected": True, "auth_file": str(self.auth_path)}

    def disconnect(self) -> dict:
        self._client = None

        try:
            self.auth_path.unlink()
            return {"service": self.key, "removed": True}
        except FileNotFoundError:
            return {"service": self.key, "removed": False}

    def _api(self):
        if self._client is None:
            if not self.auth_path.is_file():
                raise ConnectorError("YouTube Music is not connected; call connect_service('youtube-music', headers_raw=...) first")

            if self._client_factory:
                self._client = self._client_factory(self.auth_path)
            else:
                ytmusicapi, _ = _library()

                try:
                    self._client = ytmusicapi.YTMusic(auth=str(self.auth_path))
                except Exception as error:
                    raise ConnectorError(f"ytmusicapi refused the stored headers ({error}); reconnect with fresh ones") from None

        return self._client

    @staticmethod
    def _call(function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except ConnectorError:
            raise
        except Exception as error:
            raise ConnectorError(f"YouTube Music request failed ({type(error).__name__}: {error}). ytmusicapi breaks when "
                                 "YouTube changes its pages or the cookies expire; update ytmusicapi or reconnect.") from None

    def list_playlists(self, **kwargs) -> list[dict]:
        items = self._call(self._api().get_library_playlists, limit=None) or []
        return [{"id": playlist_id_from_ref(p.get("playlistId") or ""), "name": p.get("title") or "Untitled playlist",
                 "tracks": p.get("count"), "url": f"https://music.youtube.com/playlist?list={p.get('playlistId')}"}
                for p in items if p.get("playlistId")]

    def fetch_playlist(self, ref: str) -> ImportedPlaylist:
        playlist_id = playlist_id_from_ref(ref)
        data = self._call(self._api().get_playlist, playlist_id, limit=None) or {}
        warnings: list[str] = []
        tracks: list[Track] = []

        for position, item in enumerate(data.get("tracks") or [], start=1):
            title = (item.get("title") or "").strip()

            if not title:
                warnings.append(f"position {position}: skipped entry without a title")
                continue

            if item.get("isAvailable") is False:
                warnings.append(f"position {position}: {title!r} is unavailable on YouTube Music (kept)")

            artists = ", ".join((a.get("name") or "").strip() for a in item.get("artists") or [] if a.get("name"))
            album = ((item.get("album") or {}).get("name") or "").strip() if isinstance(item.get("album"), dict) else ""
            seconds = item.get("duration_seconds")
            video_id = item.get("videoId")
            tracks.append(Track(
                title=title, artist=artists, album=album,
                duration_ms=int(seconds) * 1000 if isinstance(seconds, (int, float)) and seconds > 0 else None,
                source_uri=f"https://music.youtube.com/watch?v={video_id}" if video_id else None, position=len(tracks),
            ))

        return ImportedPlaylist(
            name=(data.get("title") or "Untitled playlist").strip(), source="youtube-music", tracks=tracks,
            source_ref=f"https://music.youtube.com/playlist?list={playlist_id}", warnings=warnings,
            has_durations=any(t.duration_ms for t in tracks),
        )
