"""Local Spotify PKCE transport and loopback callback handling."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import BinaryIO, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from smarthome.spotify_oauth import (
    SpotifyAuthorizationGrant,
    SpotifyOAuthConfig,
    SpotifyOAuthError,
    SpotifyTokenSet,
)


SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
MAX_TOKEN_RESPONSE_BYTES = 64 * 1024


class SpotifyTransportError(SpotifyOAuthError):
    """Controlled network or callback failure without credential details."""


class HttpResponse(Protocol):
    def read(self, amount: int = -1) -> bytes:
        """Read response bytes."""


HttpRequester = Callable[
    [Request, float], AbstractContextManager[HttpResponse]
]


def _open_request(
    request: Request,
    timeout: float,
) -> AbstractContextManager[BinaryIO]:
    return urlopen(request, timeout=timeout)


class SpotifyTokenEndpoint:
    """Exchange and refresh PKCE grants without a client secret."""

    def __init__(
        self,
        client_id: str,
        *,
        requester: HttpRequester = _open_request,
        timeout_seconds: float = 15.0,
    ) -> None:
        if not isinstance(client_id, str) or not client_id.strip():
            raise SpotifyTransportError("Die Spotify-Client-ID fehlt.")
        if not isinstance(timeout_seconds, (int, float)) or not 1 <= timeout_seconds <= 60:
            raise SpotifyTransportError("Das Spotify-Netzwerkzeitlimit ist ungültig.")
        self.client_id = client_id
        self._requester = requester
        self._timeout_seconds = float(timeout_seconds)

    def exchange(
        self,
        grant: SpotifyAuthorizationGrant,
        *,
        now: datetime | None = None,
    ) -> SpotifyTokenSet:
        """Exchange one validated authorization grant for local tokens."""

        if grant.client_id != self.client_id:
            raise SpotifyTransportError(
                "Die Spotify-Freigabe gehört zu einer anderen Anwendung."
            )
        payload = self._request(
            {
                "grant_type": "authorization_code",
                "code": grant.code,
                "redirect_uri": grant.redirect_uri,
                "client_id": self.client_id,
                "code_verifier": grant.code_verifier,
            }
        )
        return SpotifyTokenSet.from_initial_response(
            payload,
            now=now or datetime.now(UTC),
        )

    def refresh(self, refresh_token: str) -> Mapping[str, object]:
        """Return a validated-input refresh response for SpotifyTokenManager."""

        if not isinstance(refresh_token, str) or not refresh_token:
            raise SpotifyTransportError("Das Spotify-Refresh-Token fehlt.")
        return self._request(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self.client_id,
            }
        )

    def _request(self, fields: Mapping[str, str]) -> Mapping[str, object]:
        request = Request(
            SPOTIFY_TOKEN_URL,
            data=urlencode(fields).encode("ascii"),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        try:
            with self._requester(request, self._timeout_seconds) as response:
                payload = response.read(MAX_TOKEN_RESPONSE_BYTES + 1)
        except (HTTPError, URLError, TimeoutError, OSError):
            raise SpotifyTransportError(
                "Der Spotify-Token-Dienst ist derzeit nicht erreichbar."
            ) from None
        if len(payload) > MAX_TOKEN_RESPONSE_BYTES:
            raise SpotifyTransportError("Die Spotify-Token-Antwort ist zu groß.")
        try:
            decoded = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise SpotifyTransportError(
                "Spotify lieferte keine gültige Token-Antwort."
            ) from None
        if not isinstance(decoded, dict):
            raise SpotifyTransportError(
                "Spotify lieferte keine gültige Token-Antwort."
            )
        return decoded


class _LoopbackServer(HTTPServer):
    callback_target: str | None = None
    expected_path: str


class _LoopbackHandler(BaseHTTPRequestHandler):
    server: _LoopbackServer

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = urlsplit(self.path).path
        if path != self.server.expected_path:
            self.send_error(404)
            return
        self.server.callback_target = self.path
        body = (
            b"<!doctype html><meta charset=utf-8>"
            b"<title>Spotify verbunden</title>"
            b"<h1>Spotify-Freigabe empfangen</h1>"
            b"<p>Dieses Fenster kann geschlossen werden.</p>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *args: object) -> None:
        """Never log callback paths because they contain authorization codes."""


def receive_loopback_callback(
    config: SpotifyOAuthConfig,
    *,
    timeout_seconds: float | None = None,
) -> str:
    """Receive exactly one Spotify redirect on the configured loopback URI."""

    parsed = urlsplit(config.redirect_uri)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.port is None:
        raise SpotifyTransportError(
            "Der lokale Spotify-Callback benötigt 127.0.0.1 und einen Port."
        )
    timeout = (
        float(config.authorization_timeout_seconds)
        if timeout_seconds is None
        else timeout_seconds
    )
    if not 1 <= timeout <= config.authorization_timeout_seconds:
        raise SpotifyTransportError("Das Spotify-Callback-Zeitlimit ist ungültig.")

    server = _LoopbackServer((parsed.hostname, parsed.port), _LoopbackHandler)
    server.expected_path = parsed.path
    deadline = time.monotonic() + timeout
    try:
        while server.callback_target is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SpotifyTransportError(
                    "Die Spotify-Anmeldung wurde nicht rechtzeitig abgeschlossen."
                )
            server.timeout = min(remaining, 1.0)
            server.handle_request()
    finally:
        server.server_close()

    return f"{config.redirect_uri}?{urlsplit(server.callback_target).query}"
