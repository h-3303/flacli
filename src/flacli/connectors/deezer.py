# SPDX-License-Identifier: GPL-3.0-or-later
"""Deezer: public REST API, no auth for public playlists (https://api.deezer.com/playlist/{id}).

Track rows carry title, artist.name, album.title, duration (seconds) and isrc; pages follow `next`.
OAuth for private playlists is deliberately not implemented (roadmap: only if asked).
"""

import re
import time

from ..models import ImportedPlaylist, Track
from . import Connector, ConnectorError, http_json

API_URL = "https://api.deezer.com"
PAGE_SIZE = 100
MAX_RETRIES = 3
PLAYLIST_REF = re.compile(r"(?:^|/playlist/)(\d+)(?:[/?#]|$)")
USER_REF = re.compile(r"(?:^|/profile/)(\d+)(?:[/?#]|$)")


def playlist_id_from_ref(ref: str) -> str:
    match = PLAYLIST_REF.search((ref or "").strip())

    if not match:
        raise ConnectorError(f"{ref!r} is not a Deezer playlist id or URL (expected digits or a deezer.com/.../playlist/<id> link)")

    return match.group(1)


class DeezerConnector(Connector):
    key = "deezer"
    label = "Deezer"

    def __init__(self, fetch=None, sleep=time.sleep):
        self._fetch = fetch or http_json
        self._sleep = sleep

    def status(self) -> dict:
        return {"service": self.key, "connected": True,
                "note": "no login: public playlists by id or URL; list_remote_playlists needs a Deezer user id or profile URL"}

    def connect(self, **kwargs) -> dict:
        return {"service": self.key, "connected": True, "note": "Deezer needs no login for public playlists"}

    def disconnect(self) -> dict:
        return {"service": self.key, "removed": False, "note": "nothing stored for Deezer"}

    def _get(self, path: str) -> dict:
        url = path if path.startswith("http") else API_URL + path

        for attempt in range(MAX_RETRIES + 1):
            status, _, data = self._fetch(url, headers={"Accept": "application/json"})
            error = (data or {}).get("error") if isinstance(data, dict) else None

            if error and error.get("code") == 4 and attempt < MAX_RETRIES:      # quota exceeded
                self._sleep(2.0 * (attempt + 1))
                continue

            if status >= 400 or error:
                detail = f"{error.get('type')}: {error.get('message')}" if error else data
                raise ConnectorError(f"Deezer returned {detail} for {url.split('?')[0]} (private or missing playlist?)")

            return data or {}

        raise ConnectorError("Deezer kept reporting quota exceeded; try again in a minute")

    def list_playlists(self, user=None, **kwargs) -> list[dict]:
        match = USER_REF.search((user or "").strip())

        if not match:
            raise ConnectorError("Deezer has no login here: pass user=<Deezer user id or deezer.com/profile/<id> URL> "
                                 "to list that user's public playlists, or import a playlist by id/URL directly")

        playlists = []
        page = self._get(f"/user/{match.group(1)}/playlists?limit={PAGE_SIZE}")

        while True:
            for item in page.get("data") or []:
                playlists.append({"id": str(item.get("id")), "name": item.get("title") or "Untitled playlist",
                                  "tracks": item.get("nb_tracks"), "public": item.get("public"), "url": item.get("link")})

            if not page.get("next"):
                return playlists

            page = self._get(page["next"])

    def fetch_playlist(self, ref: str) -> ImportedPlaylist:
        playlist_id = playlist_id_from_ref(ref)
        meta = self._get(f"/playlist/{playlist_id}")
        warnings: list[str] = []
        tracks: list[Track] = []
        position = 0
        page = self._get(f"/playlist/{playlist_id}/tracks?index=0&limit={PAGE_SIZE}")

        while True:
            for item in page.get("data") or []:
                position += 1
                title = (item.get("title") or "").strip()

                if not title:
                    warnings.append(f"position {position}: skipped entry without a title")
                    continue

                if item.get("readable") is False:
                    warnings.append(f"position {position}: {title!r} is not streamable on Deezer here (kept)")

                duration = item.get("duration")
                tracks.append(Track(
                    title=title, artist=((item.get("artist") or {}).get("name") or "").strip(),
                    album=((item.get("album") or {}).get("title") or "").strip(),
                    duration_ms=int(duration) * 1000 if isinstance(duration, (int, float)) and duration > 0 else None,
                    isrc=(item.get("isrc") or None), source_uri=f"deezer:track:{item.get('id')}", position=len(tracks),
                ))

            if not page.get("next"):
                break

            page = self._get(page["next"])

        return ImportedPlaylist(
            name=(meta.get("title") or "Untitled playlist").strip(), source="deezer", tracks=tracks,
            source_ref=meta.get("link") or f"https://www.deezer.com/playlist/{playlist_id}", warnings=warnings,
            has_durations=any(t.duration_ms for t in tracks),
        )
