# SPDX-License-Identifier: GPL-3.0-or-later
"""Streaming-service connectors: list a user's playlists and import one as an ImportedPlaylist.

Common interface (see Connector): status() / connect(...) / disconnect() / list_playlists() / fetch_playlist(ref).
TIDAL uses the official API with PKCE; Deezer reads public playlists without auth; YouTube Music goes
through ytmusicapi with browser headers. Tokens live 0600 under the flacli data dir.
"""

import json
import re
import urllib.error
import urllib.request

from ..models import ImportedPlaylist


class ConnectorError(Exception):
    """Expected failure (not connected, bad reference, service said no). Reaches the model verbatim."""


class Connector:
    key = ""
    label = ""

    def status(self) -> dict:
        raise NotImplementedError

    def connect(self, **kwargs) -> dict:
        raise NotImplementedError

    def disconnect(self) -> dict:
        raise NotImplementedError

    def list_playlists(self) -> list[dict]:
        raise NotImplementedError

    def fetch_playlist(self, ref: str) -> ImportedPlaylist:
        raise NotImplementedError


def http_json(url, method="GET", headers=None, data=None, timeout=30):
    """One HTTP request; returns (status, headers_dict, parsed_json_or_None). Never raises on HTTP status."""
    request = urllib.request.Request(url, method=method, headers=headers or {}, data=data)

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers), _decode(response.read())
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), _decode(error.read())
    except (urllib.error.URLError, TimeoutError) as error:
        raise ConnectorError(f"{url.split('/')[2]} unreachable: {getattr(error, 'reason', error)}") from None


def _decode(raw: bytes):
    if not raw:
        return None

    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"_raw": raw[:500].decode("utf-8", "replace")}


def iso_duration_ms(value) -> int | None:
    """ISO 8601 duration (PT3M12S, PT1H2M3.5S) to milliseconds; None when unparseable."""
    if not isinstance(value, str):
        return None

    match = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?", value)

    if not match or not any(match.groups()):
        return None

    days, hours, minutes, seconds = (float(g) if g else 0.0 for g in match.groups())
    return int(round(((days * 24 + hours) * 3600 + minutes * 60 + seconds) * 1000))


def get_connector(service: str) -> Connector:
    from . import deezer, tidal, ytmusic

    registry = {tidal.TidalConnector.key: tidal.TidalConnector, deezer.DeezerConnector.key: deezer.DeezerConnector,
                ytmusic.YouTubeMusicConnector.key: ytmusic.YouTubeMusicConnector}
    aliases = {"youtube": "youtube-music", "ytmusic": "youtube-music", "yt-music": "youtube-music"}
    key = aliases.get(service.strip().lower(), service.strip().lower())

    if key not in registry:
        raise ConnectorError(f"unknown service {service!r}; choose one of {', '.join(sorted(registry))}")

    return registry[key]()


SERVICES = ("tidal", "deezer", "youtube-music")
