"""Streaming-service connectors against recorded fixtures: TIDAL (PKCE + JSON:API), Deezer, YouTube Music.

No network: HTTP is a fake keyed by URL; the OAuth loopback listener is exercised on 127.0.0.1 only.
"""

import json
import os
import stat
import urllib.parse
import urllib.request

from pathlib import Path

import pytest

from flacli.connectors import ConnectorError, get_connector, iso_duration_ms
from flacli.connectors import deezer, tidal, ytmusic
from flacli.connectors.oauth import LoopbackListener, TokenStore, code_challenge, code_verifier

FIXTURES = Path(__file__).parent / "fixtures" / "connectors"


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


class FakeHTTP:
    """fetch(url, method=, headers=, data=) -> (status, headers, json); routes by URL prefix (query included)."""

    def __init__(self):
        self.routes = {}
        self.calls = []

    def add(self, url, body, status=200, headers=None):
        self.routes[url] = (status, headers or {}, body)

    def __call__(self, url, method="GET", headers=None, data=None, timeout=30):
        self.calls.append({"url": url, "method": method, "headers": headers or {}, "data": data})

        for key in sorted(self.routes, key=len, reverse=True):
            if url == key or url.startswith(key + "&") or url.startswith(key + "?"):
                status, hdrs, body = self.routes[key]
                return status, hdrs, body() if callable(body) else body

        raise AssertionError(f"unexpected request {method} {url}")

    def urls(self, contains):
        return [c["url"] for c in self.calls if contains in c["url"]]


# Shared helpers #

def test_iso_duration():
    assert iso_duration_ms("PT3M20S") == 200000
    assert iso_duration_ms("PT1H0M5.5S") == 3605500
    assert iso_duration_ms("P30M5S") == 1805000
    assert iso_duration_ms("PT") is None and iso_duration_ms(None) is None and iso_duration_ms("3:20") is None


def test_pkce_challenge_is_s256_of_verifier():
    verifier = code_verifier()
    assert 43 <= len(verifier) <= 128 and "=" not in verifier
    assert code_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_token_store_is_private_and_atomic(tmp_path):
    store = TokenStore("tidal", tmp_path)
    assert store.load() is None
    store.save({"access_token": "x"})
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert store.load() == {"access_token": "x"}
    assert not list(tmp_path.glob("*.tmp"))
    assert store.delete() is True and store.delete() is False


def test_loopback_listener_checks_state_and_captures_code():
    listener = LoopbackListener("http://127.0.0.1:0/callback", "good-state", lifetime_s=30)
    base = f"http://127.0.0.1:{listener.port}"

    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(f"{base}/callback?code=abc&state=bad", timeout=5)
    assert error.value.code == 400 and listener.result() is None

    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(f"{base}/elsewhere", timeout=5)
    assert error.value.code == 404

    with urllib.request.urlopen(f"{base}/callback?code=abc&state=good-state", timeout=5) as response:
        assert response.status == 200 and b"close this tab" in response.read()

    assert listener.result() == {"code": "abc"}
    listener.close()


def test_loopback_listener_reports_denied_login():
    listener = LoopbackListener("http://127.0.0.1:0/callback", "s", lifetime_s=30)

    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(f"http://127.0.0.1:{listener.port}/callback?error=access_denied&state=s", timeout=5)

    assert listener.result() == {"error": "access_denied"}
    listener.close()


def test_loopback_listener_refuses_non_loopback():
    with pytest.raises(ValueError, match="loopback"):
        LoopbackListener("https://example.com/callback", "s")


# TIDAL #

@pytest.fixture
def tidal_http():
    http = FakeHTTP()
    http.add(tidal.TOKEN_URL, fixture("tidal_token.json"))
    http.add(f"{tidal.API_URL}/users/me", fixture("tidal_users_me.json"))
    http.add(f"{tidal.API_URL}/playlists?countryCode=NO&filter%5Bowners.id%5D=me", fixture("tidal_playlists_p1.json"))
    http.add(f"{tidal.API_URL}/playlists?filter%5Bowners.id%5D=me&page%5Bcursor%5D=abc&countryCode=NO", fixture("tidal_playlists_p2.json"))
    pid = "11111111-1111-4111-8111-111111111111"
    http.add(f"{tidal.API_URL}/playlists/{pid}?countryCode=NO", fixture("tidal_playlist_meta.json"))
    http.add(f"{tidal.API_URL}/playlists/{pid}/relationships/items?countryCode=NO&include=items%2Citems.artists%2Citems.albums",
             fixture("tidal_items_p1.json"))
    http.add(f"{tidal.API_URL}/playlists/{pid}/relationships/items?page%5Bcursor%5D=zyx&countryCode=NO&include=items%2Citems.artists%2Citems.albums",
             fixture("tidal_items_p2.json"))
    return http


def make_tidal(tmp_path, http, client_id="cid", now=lambda: 1_000_000.0):
    tidal._pending.clear()
    return tidal.TidalConnector(fetch=http, store_dir=tmp_path, client_id=client_id, redirect_uri="http://127.0.0.1:0/callback",
                                open_browser=False, sleep=lambda s: None, now=now)


def connected_tidal(tmp_path, http, expires_at=1_000_000.0 + 3600):
    TokenStore("tidal", tmp_path).save({"access_token": "AT1", "refresh_token": "RT1", "expires_at": expires_at, "country": "NO"})
    return make_tidal(tmp_path, http)


def test_tidal_needs_a_client_id(tmp_path, tidal_http):
    connector = make_tidal(tmp_path, tidal_http, client_id="")

    with pytest.raises(ConnectorError, match="developer.tidal.com"):
        connector.connect()

    assert connector.status()["client_id_configured"] is False


def test_tidal_login_round_trip(tmp_path, tidal_http):
    connector = make_tidal(tmp_path, tidal_http)
    first = connector.connect()
    assert first["connected"] is False
    url = urllib.parse.urlparse(first["authorize_url"])
    query = dict(urllib.parse.parse_qsl(url.query))
    assert f"{url.scheme}://{url.netloc}{url.path}" == tidal.AUTHORIZE_URL
    assert query["client_id"] == "cid" and query["code_challenge_method"] == "S256" and query["response_type"] == "code"
    assert query["scope"] == "playlists.read user.read" and query["state"]
    assert connector.status()["login_pending"] is True

    waiting = connector.connect()
    assert waiting["connected"] is False and waiting["authorize_url"] == first["authorize_url"]

    listener = tidal._pending["cid"]["listener"]
    urllib.request.urlopen(f"http://127.0.0.1:{listener.port}/callback?code=CODE&state={query['state']}", timeout=5).close()

    done = connector.connect()
    assert done == {"service": "tidal", "connected": True, "country": "NO"}
    exchange = [c for c in tidal_http.calls if c["url"] == tidal.TOKEN_URL][0]
    body = dict(urllib.parse.parse_qsl(exchange["data"].decode()))
    assert body["grant_type"] == "authorization_code" and body["code"] == "CODE" and body["client_id"] == "cid"
    assert code_challenge(body["code_verifier"]) == query["code_challenge"]
    assert body["redirect_uri"] == "http://127.0.0.1:0/callback"
    assert exchange["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    assert "client_secret" not in body

    saved = TokenStore("tidal", tmp_path).load()
    assert saved["refresh_token"] == "RT1" and saved["country"] == "NO" and saved["expires_at"] == 1_000_000.0 + 3600
    assert stat.S_IMODE((tmp_path / "tidal.json").stat().st_mode) == 0o600
    assert connector.connect()["note"].startswith("already connected")
    assert connector.disconnect() == {"service": "tidal", "removed": True}
    assert connector.status()["connected"] is False


def test_tidal_denied_login_is_reported(tmp_path, tidal_http):
    connector = make_tidal(tmp_path, tidal_http)
    state = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(connector.connect()["authorize_url"]).query))["state"]
    listener = tidal._pending["cid"]["listener"]

    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(f"http://127.0.0.1:{listener.port}/callback?error=access_denied&state={state}", timeout=5)

    with pytest.raises(ConnectorError, match="access_denied"):
        connector.connect()

    assert "cid" not in tidal._pending


def test_tidal_list_playlists_follows_cursor(tmp_path, tidal_http):
    playlists = connected_tidal(tmp_path, tidal_http).list_playlists()
    assert [(p["name"], p["tracks"]) for p in playlists] == [("Road Trip", 3), ("Quiet", 0)]
    assert playlists[0]["url"] == "https://tidal.com/browse/playlist/11111111-1111-4111-8111-111111111111"
    assert all(c["headers"]["Authorization"] == "Bearer AT1" for c in tidal_http.calls)


def test_tidal_fetch_playlist(tmp_path, tidal_http):
    playlist = connected_tidal(tmp_path, tidal_http).fetch_playlist("https://tidal.com/browse/playlist/11111111-1111-4111-8111-111111111111?u=1")
    assert playlist.name == "Road Trip" and playlist.source == "tidal" and playlist.has_durations is True
    assert playlist.source_ref == "https://tidal.com/browse/playlist/11111111-1111-4111-8111-111111111111"
    assert [t.title for t in playlist.tracks] == ["Song One", "Lonely"]
    one, lonely = playlist.tracks
    assert (one.artist, one.album, one.isrc, one.duration_ms, one.source_uri) == ("Artist A, Guest B", "Album A", "USAAA0100001", 200000, "tidal:track:1001")
    assert (lonely.artist, lonely.album, lonely.duration_ms) == ("Solo C", "Second", 3605500)
    assert [t.position for t in playlist.tracks] == [0, 1]
    assert playlist.warnings == ["position 2: skipped videos 9", "position 4: track 1003 missing from the response; skipped"]
    # the second page's next link lacked include/countryCode; the connector re-added them
    second = tidal_http.urls("page%5Bcursor%5D=zyx")
    assert second and "include=items%2Citems.artists%2Citems.albums" in second[0] and "countryCode=NO" in second[0]


def test_tidal_refreshes_expired_token(tmp_path, tidal_http):
    tidal_http.add(tidal.TOKEN_URL, fixture("tidal_token_refreshed.json"))
    connector = connected_tidal(tmp_path, tidal_http, expires_at=1_000_000.0 + 10)
    connector.list_playlists()
    refresh = [c for c in tidal_http.calls if c["url"] == tidal.TOKEN_URL]
    body = dict(urllib.parse.parse_qsl(refresh[0]["data"].decode()))
    assert body == {"client_id": "cid", "grant_type": "refresh_token", "refresh_token": "RT1", "scope": "playlists.read user.read"}
    saved = TokenStore("tidal", tmp_path).load()
    assert saved["access_token"] == "AT2" and saved["refresh_token"] == "RT1" and saved["country"] == "NO"
    assert tidal_http.urls("/playlists?")[0] and tidal_http.calls[-1]["headers"]["Authorization"] == "Bearer AT2"


def test_tidal_retries_once_on_401_and_backs_off_on_429(tmp_path, tidal_http):
    tidal_http.add(tidal.TOKEN_URL, fixture("tidal_token_refreshed.json"))
    answers = iter([(401, {}, {"errors": [{"code": "UNAUTHORIZED", "detail": "expired"}]}),
                    (429, {"Retry-After": "1"}, None),
                    (200, {}, fixture("tidal_playlists_p2.json"))])
    url = f"{tidal.API_URL}/playlists?countryCode=NO&filter%5Bowners.id%5D=me"
    original = tidal_http.__call__
    slept = []

    def fetch(u, **kwargs):
        if u == url:
            tidal_http.calls.append({"url": u, "method": "GET", "headers": kwargs.get("headers", {}), "data": None})
            return next(answers)
        return original(u, **kwargs)

    connector = tidal.TidalConnector(fetch=fetch, store_dir=tmp_path, client_id="cid", redirect_uri="http://127.0.0.1:0/callback",
                                     open_browser=False, sleep=slept.append, now=lambda: 1_000_000.0)
    TokenStore("tidal", tmp_path).save({"access_token": "AT1", "refresh_token": "RT1", "expires_at": 1_000_000.0 + 3600, "country": "NO"})
    assert [p["name"] for p in connector.list_playlists()] == ["Quiet"]
    assert slept == [1.0]
    assert [c["headers"]["Authorization"] for c in tidal_http.calls if c["url"] == url] == ["Bearer AT1", "Bearer AT2", "Bearer AT2"]


def test_tidal_api_errors_are_explained(tmp_path, tidal_http):
    connector = connected_tidal(tmp_path, tidal_http)
    tidal_http.add(f"{tidal.API_URL}/playlists/33333333-3333-4333-8333-333333333333?countryCode=NO",
                   {"errors": [{"code": "NOT_FOUND", "detail": "Playlist not found"}]}, status=404)

    with pytest.raises(ConnectorError, match="HTTP 404.*NOT_FOUND: Playlist not found"):
        connector.fetch_playlist("33333333-3333-4333-8333-333333333333")

    with pytest.raises(ConnectorError, match="not a TIDAL playlist"):
        connector.fetch_playlist("https://tidal.com/browse/album/1234")

    with pytest.raises(ConnectorError, match="not connected"):
        make_tidal(tmp_path, tidal_http).disconnect() and make_tidal(tmp_path, tidal_http).list_playlists()


def test_tidal_bad_token_response(tmp_path, tidal_http):
    tidal_http.add(tidal.TOKEN_URL, {"error": "invalid_grant", "error_description": "code expired"}, status=400)
    connector = connected_tidal(tmp_path, tidal_http, expires_at=0)

    with pytest.raises(ConnectorError, match="HTTP 400: code expired"):
        connector.list_playlists()


# Deezer #

@pytest.fixture
def deezer_http():
    http = FakeHTTP()
    http.add(f"{deezer.API_URL}/playlist/3155776842", fixture("deezer_playlist.json"))
    http.add(f"{deezer.API_URL}/playlist/3155776842/tracks?index=0&limit=100", fixture("deezer_tracks_p1.json"))
    http.add(f"{deezer.API_URL}/playlist/3155776842/tracks?limit=100&index=2", fixture("deezer_tracks_p2.json"))
    http.add(f"{deezer.API_URL}/playlist/1", fixture("deezer_error.json"))
    http.add(f"{deezer.API_URL}/user/637006841/playlists?limit=100", fixture("deezer_user_playlists.json"))
    return http


def test_deezer_fetch_playlist(deezer_http):
    connector = deezer.DeezerConnector(fetch=deezer_http)
    assert connector.status()["connected"] is True
    playlist = connector.fetch_playlist("https://www.deezer.com/en/playlist/3155776842?utm_source=x")
    assert playlist.name == "Top Worldwide" and playlist.source == "deezer" and playlist.source_ref == "https://www.deezer.com/playlist/3155776842"
    assert [t.title for t in playlist.tracks] == ["Song One", "Ghost", "Lonely"]
    one, ghost, lonely = playlist.tracks
    assert (one.artist, one.album, one.duration_ms, one.isrc, one.source_uri) == ("Artist A", "Album A", 200000, "USAAA0100001", "deezer:track:1")
    assert ghost.duration_ms is None and ghost.isrc is None
    assert lonely.duration_ms == 185000
    assert playlist.warnings == ["position 2: 'Ghost' is not streamable on Deezer here (kept)"]
    assert playlist.has_durations is True


def test_deezer_errors_and_refs(deezer_http):
    connector = deezer.DeezerConnector(fetch=deezer_http)

    with pytest.raises(ConnectorError, match="DataException: no data"):
        connector.fetch_playlist("1")

    with pytest.raises(ConnectorError, match="not a Deezer playlist"):
        connector.fetch_playlist("https://www.deezer.com/album/12")

    with pytest.raises(ConnectorError, match="pass user="):
        connector.list_playlists()

    playlists = connector.list_playlists(user="https://www.deezer.com/en/profile/637006841")
    assert playlists == [{"id": "3155776842", "name": "Top Worldwide", "tracks": 3, "public": True,
                          "url": "https://www.deezer.com/playlist/3155776842"}]


def test_deezer_backs_off_on_quota():
    calls = []
    answers = iter([{"error": {"type": "Exception", "message": "Quota limit exceeded", "code": 4}}, fixture("deezer_playlist.json")])
    slept = []

    def fetch(url, **kwargs):
        calls.append(url)
        return 200, {}, next(answers) if "/tracks" not in url else fixture("deezer_tracks_p2.json")

    playlist = deezer.DeezerConnector(fetch=fetch, sleep=slept.append).fetch_playlist("3155776842")
    assert playlist.name == "Top Worldwide" and slept == [2.0] and len(playlist.tracks) == 1


# YouTube Music #

class FakeYTMusic:
    instances = []

    def __init__(self, auth_path):
        self.auth_path = auth_path
        FakeYTMusic.instances.append(self)

    def get_library_playlists(self, limit=25):
        assert limit is None
        return fixture("ytmusic_library_playlists.json")

    def get_playlist(self, playlist_id, limit=100, related=False, suggestions_limit=0):
        assert limit is None and playlist_id == "PLxxxxxxxxxxxxxxxx"
        return fixture("ytmusic_playlist.json")


def test_ytmusic_connect_with_auth_file_and_fetch(tmp_path):
    connector = ytmusic.YouTubeMusicConnector(store_dir=tmp_path, client_factory=FakeYTMusic)
    status = connector.status()
    assert status["connected"] is False and "music.youtube.com" in status["how_to_connect"]
    assert status["ytmusicapi_version"]

    with pytest.raises(ConnectorError, match="not connected"):
        connector.list_playlists()

    with pytest.raises(ConnectorError, match="headers_raw"):
        connector.connect()

    browser = tmp_path / "browser.json"
    browser.write_text('{"Cookie": "x"}')
    assert connector.connect(auth_file=str(browser))["connected"] is True
    assert stat.S_IMODE(connector.auth_path.stat().st_mode) == 0o600

    playlists = connector.list_playlists()
    assert playlists[0] == {"id": "PLxxxxxxxxxxxxxxxx", "name": "Road Trip", "tracks": 2,
                            "url": "https://music.youtube.com/playlist?list=VLPLxxxxxxxxxxxxxxxx"}
    assert playlists[1]["name"] == "Liked Music"

    playlist = connector.fetch_playlist("https://music.youtube.com/playlist?list=PLxxxxxxxxxxxxxxxx&si=abc")
    assert playlist.name == "Road Trip" and playlist.source == "youtube-music"
    assert [t.title for t in playlist.tracks] == ["Song One", "Lonely"]
    one, lonely = playlist.tracks
    assert (one.artist, one.album, one.duration_ms, one.isrc, one.source_uri) == ("Artist A, Guest B", "Album A", 200000, None, "https://music.youtube.com/watch?v=v1")
    assert lonely.album == "" and lonely.duration_ms == 185000
    assert playlist.warnings == ["position 2: 'Lonely' is unavailable on YouTube Music (kept)", "position 3: skipped entry without a title"]
    assert connector.disconnect()["removed"] is True and connector.status()["connected"] is False


def test_ytmusic_connect_with_headers_writes_private_auth_file(tmp_path):
    headers = "\n".join([
        "POST /youtubei/v1/browse?prettyPrint=false HTTP/2", "Host: music.youtube.com", "User-Agent: Mozilla/5.0",
        "Accept: */*", "Content-Type: application/json", "X-Goog-AuthUser: 0", "x-origin: https://music.youtube.com",
        "Authorization: SAPISIDHASH 1700000000_abcdef", "Cookie: __Secure-3PAPISID=abc/def; SAPISID=abc/def; SID=x; HSID=y; SSID=z",
    ])
    connector = ytmusic.YouTubeMusicConnector(store_dir=tmp_path)
    result = connector.connect(headers_raw=headers)
    assert result["connected"] is True
    assert stat.S_IMODE(connector.auth_path.stat().st_mode) == 0o600
    saved = json.loads(connector.auth_path.read_text())
    assert "cookie" in {k.lower() for k in saved} and connector.status()["connected"] is True

    with pytest.raises(ConnectorError, match="could not read those headers"):
        ytmusic.YouTubeMusicConnector(store_dir=tmp_path / "other").connect(headers_raw="garbage")


def test_ytmusic_wraps_library_failures(tmp_path):
    class Broken(FakeYTMusic):
        def get_playlist(self, *args, **kwargs):
            raise KeyError("contents")

    connector = ytmusic.YouTubeMusicConnector(store_dir=tmp_path, client_factory=Broken)
    connector.auth_path.write_text("{}")

    with pytest.raises(ConnectorError, match="KeyError: 'contents'.*update ytmusicapi"):
        connector.fetch_playlist("PLxxxxxxxxxxxxxxxx")

    with pytest.raises(ConnectorError, match="not a YouTube Music playlist"):
        connector.fetch_playlist("https://music.youtube.com/watch?v=abc")


def test_registry_and_aliases():
    assert get_connector("TIDAL").key == "tidal"
    assert get_connector("ytmusic").key == "youtube-music"

    with pytest.raises(ConnectorError, match="unknown service"):
        get_connector("napster")


# Server tools #

@pytest.fixture
def library(tmp_path, monkeypatch):
    import flacli.server as server

    monkeypatch.setenv("FLACLI_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("FLACLI_TIDAL_CLIENT_ID", "")
    server.State.db = None
    yield server

    if server.State.db is not None:
        server.State.db.close()
        server.State.db = None


@pytest.mark.anyio
async def test_import_remote_playlist_tool(library, deezer_http, monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError

    monkeypatch.setattr(library, "get_connector", lambda service: deezer.DeezerConnector(fetch=deezer_http))
    (imported,) = (await library.import_remote_playlist("deezer", "https://www.deezer.com/playlist/3155776842"))["imported"]
    assert imported["name"] == "Top Worldwide" and imported["tracks"] == 3 and imported["detected_format"] == "deezer"
    assert imported["source_ref"] == "https://www.deezer.com/playlist/3155776842" and imported["needs_durations"] is False
    assert Path(imported["jspf_path"]).is_file()
    jspf = json.loads(Path(imported["jspf_path"]).read_text())["playlist"]
    assert jspf["track"][0]["extension"]["https://musicbrainz.org/doc/jspf#track"]["additional_metadata"]["isrc"] == "USAAA0100001"

    (row,) = (await library.list_playlists())["playlists"]
    assert row["source"] == "deezer" and row["tracks"] == 3 and row["counts"] == {"pending": 3}

    listed = await library.list_remote_playlists("deezer", user="637006841")
    assert listed["service"] == "deezer" and listed["playlists"][0]["name"] == "Top Worldwide"

    with pytest.raises(ToolError, match="not a Deezer playlist"):
        await library.import_remote_playlist("deezer", "nonsense")


@pytest.mark.anyio
async def test_service_status_and_unknown_service(library, tmp_path, monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError

    status = await library.service_status()
    assert [s["service"] for s in status["services"]] == ["tidal", "deezer", "youtube-music"]
    assert status["services"][0]["connected"] is False and "developer.tidal.com" in status["services"][0]["how_to_connect"]
    assert status["services"][0]["token_file"].startswith(str(tmp_path / "data" / "auth"))
    assert stat.S_IMODE((tmp_path / "data" / "auth").stat().st_mode) == 0o700

    with pytest.raises(ToolError, match="developer.tidal.com"):
        await library.connect_service("tidal")

    with pytest.raises(ToolError, match="unknown service"):
        await library.connect_service("napster")
