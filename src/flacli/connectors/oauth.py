# SPDX-License-Identifier: GPL-3.0-or-later
"""PKCE helpers, a loopback redirect listener and a 0600 token store shared by OAuth connectors."""

import base64
import hashlib
import http.server
import json
import os
import secrets
import threading
import time
import urllib.parse

from pathlib import Path

from .. import config


# PKCE #

def code_verifier() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode("ascii")


def code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


# Token store #

class TokenStore:
    """One JSON file per service under the auth dir, created 0600 and rewritten atomically."""

    def __init__(self, service: str, directory: Path | None = None):
        self.path = (directory or config.auth_dir()) / f"{service}.json"

    def load(self) -> dict | None:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError):
            return None

    def save(self, tokens: dict):
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)

        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(tokens, handle, indent=2)

        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def delete(self) -> bool:
        try:
            self.path.unlink()
            return True
        except FileNotFoundError:
            return False


# Loopback listener #

_PAGE = """<!doctype html><meta charset="utf-8"><title>flacli</title>
<body style="font:16px/1.5 system-ui;margin:3em auto;max-width:36em;color:#222">
<h1 style="font-weight:600">{title}</h1><p>{body}</p><p>You can close this tab.</p></body>"""


class LoopbackListener:
    """Serves one redirect on http://127.0.0.1:<port><path>, captures ?code= and ?state=, then stops.

    Runs in a daemon thread for at most `lifetime_s`. result() is None until the redirect arrives.
    """

    def __init__(self, redirect_uri: str, expected_state: str, lifetime_s: float = 600.0):
        parsed = urllib.parse.urlparse(redirect_uri)

        if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError(f"redirect URI must be a loopback http:// address, got {redirect_uri}")

        self.redirect_uri = redirect_uri
        self.path = parsed.path or "/"
        self.expected_state = expected_state
        self.lifetime_s = lifetime_s
        self.started_at = time.monotonic()
        self._result: dict | None = None
        self._lock = threading.Lock()
        listener = self

        class Handler(http.server.BaseHTTPRequestHandler):

            def log_message(self, *args):
                pass

            def do_GET(self):
                url = urllib.parse.urlparse(self.path)

                if url.path != listener.path:
                    self._reply(404, "Not found", "Nothing is served here.")
                    return

                query = urllib.parse.parse_qs(url.query)
                state = (query.get("state") or [""])[0]
                code = (query.get("code") or [""])[0]
                error = (query.get("error") or [""])[0]

                if state != listener.expected_state:
                    self._reply(400, "Login rejected", "The state parameter did not match this login attempt.")
                    return

                if error or not code:
                    description = (query.get("error_description") or [error or "no code returned"])[0]
                    listener._set({"error": description})
                    self._reply(400, "Login failed", description)
                    return

                listener._set({"code": code})
                self._reply(200, "Connected", "flacli received the authorisation code.")

            def _reply(self, status, title, body):
                payload = _PAGE.format(title=title, body=body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self._server = http.server.HTTPServer((parsed.hostname, parsed.port if parsed.port is not None else 80), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._serve, name="flacli-oauth", daemon=True)
        self._thread.start()

    def _serve(self):
        self._server.timeout = 1.0

        while self._result is None and time.monotonic() - self.started_at < self.lifetime_s:
            self._server.handle_request()

        self._server.server_close()

    def _set(self, result):
        with self._lock:
            self._result = result

    def result(self) -> dict | None:
        with self._lock:
            return self._result

    @property
    def expired(self) -> bool:
        return self._result is None and time.monotonic() - self.started_at >= self.lifetime_s

    def close(self):
        if self._result is None:
            self._set({"error": "cancelled"})

        self._thread.join(timeout=3)
