# SPDX-License-Identifier: GPL-3.0-or-later
"""TIDAL: official JSON:API at https://openapi.tidal.com/v2, Authorization Code + PKCE, loopback redirect.

Endpoints and scopes per the published OpenAPI spec (tidal-api-reference, checked 2026-09-17):
  authorize  https://login.tidal.com/authorize        token  https://auth.tidal.com/v1/oauth2/token
  GET /users/me                                    -> attributes.country (scope user.read)
  GET /playlists?filter[owners.id]=me&countryCode= -> the user's playlists, cursor paged (scope playlists.read)
  GET /playlists/{id}/relationships/items?include=items,items.artists,items.albums&countryCode=
Playlist items arrive as resource identifiers; the tracks (title, isrc, duration ISO 8601) and their artists
and albums come back in `included`. Videos in a playlist are skipped with a warning.
"""

import re
import secrets
import time
import urllib.parse
import webbrowser

from .. import config
from ..models import ImportedPlaylist, Track
from . import Connector, ConnectorError, http_json, iso_duration_ms
from .oauth import LoopbackListener, TokenStore, code_challenge, code_verifier

AUTHORIZE_URL = "https://login.tidal.com/authorize"
TOKEN_URL = "https://auth.tidal.com/v1/oauth2/token"
API_URL = "https://openapi.tidal.com/v2"
SCOPES = "playlists.read user.read"
DEFAULT_COUNTRY = "US"
MAX_RETRIES = 3
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)

_pending: dict[str, dict] = {}     # one in-flight login per process; keyed by client id


def playlist_id_from_ref(ref: str) -> str:
    """Accepts a playlist UUID or any tidal.com / listen.tidal.com playlist URL."""
    match = UUID.search(ref or "")

    if not match:
        raise ConnectorError(f"{ref!r} is not a TIDAL playlist id or URL (expected a UUID or a tidal.com/playlist/... link)")

    return match.group(0).lower()


class TidalConnector(Connector):
    key = "tidal"
    label = "TIDAL"

    def __init__(self, fetch=None, store_dir=None, client_id=None, redirect_uri=None, open_browser=True,
                 sleep=time.sleep, now=time.time):
        self._fetch = fetch or http_json
        self.store = TokenStore("tidal", store_dir)
        self.client_id = client_id if client_id is not None else config.tidal_client_id()
        self.redirect_uri = redirect_uri or config.tidal_redirect_uri()
        self.open_browser = open_browser
        self._sleep = sleep
        self._now = now

    # Auth #

    def status(self) -> dict:
        tokens = self.store.load()
        pending = _pending.get(self.client_id)
        info = {
            "service": self.key, "connected": bool(tokens and tokens.get("refresh_token")),
            "client_id_configured": bool(self.client_id), "redirect_uri": self.redirect_uri,
            "token_file": str(self.store.path),
        }

        if tokens:
            info["expires_in_s"] = max(0, int(tokens.get("expires_at", 0) - self._now()))
            info["country"] = tokens.get("country")
        if pending and not pending["listener"].expired:
            info["login_pending"] = True
            info["authorize_url"] = pending["url"]
        if not self.client_id:
            info["how_to_connect"] = (
                "Create an app at https://developer.tidal.com/dashboard, add the redirect URI "
                f"{self.redirect_uri} to it, enable the playlists.read and user.read scopes, then set the "
                "tidal_client_id setting (flacli config set tidal_client_id ...) or FLACLI_TIDAL_CLIENT_ID."
            )

        return info

    def connect(self, **kwargs) -> dict:
        """Start (or finish) the PKCE login. Non-blocking: returns the URL to open, then 'connected' once the
        browser has come back to the loopback listener and the code has been exchanged."""
        if not self.client_id:
            raise ConnectorError(self.status()["how_to_connect"])

        tokens = self.store.load()

        if tokens and tokens.get("refresh_token") and not kwargs.get("force"):
            return {"service": self.key, "connected": True, "note": "already connected; pass force=True to log in again"}

        pending = _pending.get(self.client_id)

        if pending:
            result = pending["listener"].result()

            if result and "code" in result:
                _pending.pop(self.client_id, None)
                tokens = self._exchange(result["code"], pending["verifier"])
                tokens["country"] = self._country(tokens["access_token"])
                self.store.save(tokens)
                return {"service": self.key, "connected": True, "country": tokens["country"]}

            if result and "error" in result:
                _pending.pop(self.client_id, None)
                raise ConnectorError(f"TIDAL login failed: {result['error']}. Call connect_service again to retry.")

            if not pending["listener"].expired:
                return {"service": self.key, "connected": False, "authorize_url": pending["url"],
                        "note": "still waiting for the browser to come back; open the URL, log in, then call connect_service again"}

            _pending.pop(self.client_id, None)

        verifier = code_verifier()
        state = secrets.token_urlsafe(16)

        try:
            listener = LoopbackListener(self.redirect_uri, state)
        except OSError as error:
            raise ConnectorError(f"cannot listen on {self.redirect_uri} for the login redirect: {error}") from None

        url = AUTHORIZE_URL + "?" + urllib.parse.urlencode({
            "client_id": self.client_id, "code_challenge": code_challenge(verifier), "code_challenge_method": "S256",
            "redirect_uri": self.redirect_uri, "response_type": "code", "scope": SCOPES, "state": state,
        })
        _pending[self.client_id] = {"verifier": verifier, "listener": listener, "url": url}
        opened = False

        if self.open_browser:
            try:
                opened = bool(webbrowser.open(url, new=2))
            except Exception:
                opened = False

        return {
            "service": self.key, "connected": False, "authorize_url": url, "browser_opened": opened,
            "note": "log in to TIDAL in the browser and approve access; then call connect_service('tidal') again "
                    "to finish. The listener waits ten minutes.",
        }

    def disconnect(self) -> dict:
        pending = _pending.pop(self.client_id, None)

        if pending:
            pending["listener"].close()

        return {"service": self.key, "removed": self.store.delete()}

    def _token_request(self, form: dict) -> dict:
        body = urllib.parse.urlencode(form).encode("ascii")
        status, _, data = self._fetch(TOKEN_URL, method="POST", data=body,
                                      headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})

        if status != 200 or not isinstance(data, dict) or not data.get("access_token"):
            detail = (data or {}).get("error_description") or (data or {}).get("error") or data
            raise ConnectorError(f"TIDAL token endpoint returned HTTP {status}: {detail}")

        return {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token") or form.get("refresh_token"),
            "expires_at": self._now() + int(data.get("expires_in") or 0),
            "scope": data.get("scope"),
            "user_id": data.get("user_id"),
        }

    def _exchange(self, code: str, verifier: str) -> dict:
        return self._token_request({
            "client_id": self.client_id, "code": code, "code_verifier": verifier, "grant_type": "authorization_code",
            "redirect_uri": self.redirect_uri, "scope": SCOPES,
        })

    def _refresh(self, tokens: dict) -> dict:
        fresh = self._token_request({
            "client_id": self.client_id, "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
            "scope": SCOPES,
        })
        fresh["country"] = tokens.get("country")
        self.store.save(fresh)
        return fresh

    def _access_token(self) -> tuple[str, dict]:
        tokens = self.store.load()

        if not tokens or not tokens.get("refresh_token"):
            raise ConnectorError("TIDAL is not connected; call connect_service('tidal') first")

        if tokens.get("expires_at", 0) - 60 < self._now():
            tokens = self._refresh(tokens)

        return tokens["access_token"], tokens

    # API #

    def _get(self, path_or_url: str, **params) -> dict:
        token, tokens = self._access_token()
        url = path_or_url if path_or_url.startswith("http") else API_URL + path_or_url

        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)

        for attempt in range(MAX_RETRIES + 1):
            status, headers, data = self._fetch(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.api+json"})

            if status == 401 and attempt == 0:
                token = self._refresh(tokens)["access_token"]
                continue

            if status == 429 and attempt < MAX_RETRIES:
                retry_after = headers.get("Retry-After") or headers.get("retry-after") or "2"
                self._sleep(min(float(retry_after) if retry_after.replace(".", "", 1).isdigit() else 2.0, 30.0))
                continue

            if status >= 400:
                errors = (data or {}).get("errors") if isinstance(data, dict) else None
                detail = "; ".join(f"{e.get('code')}: {e.get('detail')}" for e in errors) if errors else data
                raise ConnectorError(f"TIDAL returned HTTP {status} for {url.split('?')[0]}: {detail}")

            return data or {}

        raise ConnectorError("TIDAL kept rate-limiting the request; try again in a minute")

    def _country(self, access_token: str) -> str:
        status, _, data = self._fetch(f"{API_URL}/users/me", headers={"Authorization": f"Bearer {access_token}",
                                                                      "Accept": "application/vnd.api+json"})
        country = (((data or {}).get("data") or {}).get("attributes") or {}).get("country") if status == 200 else None
        return country or DEFAULT_COUNTRY

    def _country_code(self, tokens) -> str:
        return tokens.get("country") or DEFAULT_COUNTRY

    def _pages(self, first_path: str, **params):
        """Yield every page of a cursor-paged JSON:API collection, re-adding include/countryCode when the
        server's `next` link drops them (it did until late 2025)."""
        page = self._get(first_path, **params)

        while True:
            yield page
            next_link = (page.get("links") or {}).get("next")

            if not next_link:
                return

            parsed = urllib.parse.urlparse(next_link)
            query = dict(urllib.parse.parse_qsl(parsed.query))

            for key, value in params.items():
                query.setdefault(key, value)

            path = parsed.path if parsed.path.startswith("/") else "/" + parsed.path

            if path.startswith("/v2/"):
                path = path[3:]

            page = self._get(path + "?" + urllib.parse.urlencode(query))

    def list_playlists(self, **kwargs) -> list[dict]:
        _, tokens = self._access_token()
        playlists = []

        for page in self._pages("/playlists", **{"countryCode": self._country_code(tokens), "filter[owners.id]": "me"}):
            for item in page.get("data") or []:
                attributes = item.get("attributes") or {}
                playlists.append({
                    "id": item.get("id"), "name": attributes.get("name") or "Untitled playlist",
                    "tracks": attributes.get("numberOfItems"), "type": attributes.get("playlistType"),
                    "url": f"https://tidal.com/browse/playlist/{item.get('id')}",
                })

        return playlists

    def fetch_playlist(self, ref: str) -> ImportedPlaylist:
        playlist_id = playlist_id_from_ref(ref)
        _, tokens = self._access_token()
        country = self._country_code(tokens)
        meta = self._get(f"/playlists/{playlist_id}", countryCode=country)
        name = ((meta.get("data") or {}).get("attributes") or {}).get("name") or "Untitled playlist"
        warnings: list[str] = []
        tracks: list[Track] = []
        position = 0

        for page in self._pages(f"/playlists/{playlist_id}/relationships/items", countryCode=country,
                                include="items,items.artists,items.albums"):
            included = {(r.get("type"), r.get("id")): r for r in page.get("included") or []}

            for item in page.get("data") or []:
                position += 1
                kind, item_id = item.get("type"), item.get("id")

                if kind != "tracks":
                    warnings.append(f"position {position}: skipped {kind or 'unknown item'} {item_id}")
                    continue

                resource = included.get(("tracks", item_id))

                if not resource:
                    warnings.append(f"position {position}: track {item_id} missing from the response; skipped")
                    continue

                attributes = resource.get("attributes") or {}
                relationships = resource.get("relationships") or {}
                artists = [included.get(("artists", a.get("id"))) for a in (relationships.get("artists") or {}).get("data") or []]
                albums = [included.get(("albums", a.get("id"))) for a in (relationships.get("albums") or {}).get("data") or []]
                artist = ", ".join((a.get("attributes") or {}).get("name", "") for a in artists if a).strip(", ")
                album = next(((a.get("attributes") or {}).get("title", "") for a in albums if a), "")
                title = (attributes.get("title") or "").strip()

                if not title:
                    warnings.append(f"position {position}: skipped track {item_id} without a title")
                    continue

                tracks.append(Track(
                    title=title, artist=artist, album=album, duration_ms=iso_duration_ms(attributes.get("duration")),
                    isrc=(attributes.get("isrc") or None), source_uri=f"tidal:track:{item_id}", position=len(tracks),
                ))

        return ImportedPlaylist(
            name=name.strip(), source="tidal", tracks=tracks, source_ref=f"https://tidal.com/browse/playlist/{playlist_id}",
            warnings=warnings, has_durations=any(t.duration_ms for t in tracks),
        )
