"""Authenticated local HTTP boundary for structured Spotify commands."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Protocol

from smarthome.alexa_spotify import AlexaVoiceIdentity, SpotifySkillSession
from smarthome.spotify_alexa_commands import parse_spotify_alexa_intent


MAX_BODY_BYTES = 16 * 1024
MAX_CLOCK_SKEW_SECONDS = 300
BRIDGE_PATH = "/api/spotify/command"


class SpotifyBridgeError(ValueError):
    """Raised for an unauthenticated or malformed bridge request."""


class SpotifyCommandDispatcher(Protocol):
    def dispatch(
        self,
        command: object,
        *,
        voice_identity: AlexaVoiceIdentity,
        session: SpotifySkillSession,
        now: datetime,
    ) -> object:
        """Dispatch one already authenticated command."""


@dataclass(frozen=True, slots=True)
class SpotifyBridgeResponse:
    """Token-free response suitable for Alexa or a local caller."""

    status: str
    profile_id: str | None = None
    target_id: str | None = None

    def as_mapping(self) -> dict[str, object]:
        result: dict[str, object] = {"status": self.status}
        if self.profile_id is not None:
            result["profile_id"] = self.profile_id
        if self.target_id is not None:
            result["target_id"] = self.target_id
        return result


class SpotifyBridge:
    """Validate and dispatch requests without exposing credentials."""

    def __init__(
        self,
        dispatcher: SpotifyCommandDispatcher,
        shared_secret: str,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(shared_secret, str) or len(shared_secret) < 32:
            raise SpotifyBridgeError("Der Bridge-Schlüssel muss mindestens 32 Zeichen haben.")
        self._dispatcher = dispatcher
        self._secret = shared_secret.encode("utf-8")
        self._clock = clock

    def handle(
        self,
        *,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> SpotifyBridgeResponse:
        if method.upper() != "POST" or path != BRIDGE_PATH:
            raise SpotifyBridgeError("Der Bridge-Endpunkt ist nicht verfügbar.")
        if len(body) > MAX_BODY_BYTES:
            raise SpotifyBridgeError("Die Bridge-Anfrage ist zu groß.")
        self._verify_signature(headers, body)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise SpotifyBridgeError("Die Bridge-Anfrage ist kein gültiges JSON.") from None
        if not isinstance(payload, dict) or set(payload) != {
            "intent", "voice_identity", "session"
        }:
            raise SpotifyBridgeError("Die Bridge-Anfrage enthält unerwartete Felder.")
        voice = _voice_identity(payload["voice_identity"])
        session = _session(payload["session"])
        command = parse_spotify_alexa_intent(payload["intent"])
        result = self._dispatcher.dispatch(
            command,
            voice_identity=voice,
            session=session,
            now=datetime.now(UTC),
        )
        return _response(result)

    def _verify_signature(self, headers: Mapping[str, str], body: bytes) -> None:
        signature = headers.get("X-SmartHome-Signature", "")
        timestamp = headers.get("X-SmartHome-Timestamp", "")
        try:
            timestamp_value = int(timestamp)
            supplied = bytes.fromhex(signature)
        except (TypeError, ValueError):
            raise SpotifyBridgeError("Die Bridge-Signatur ist ungültig.") from None
        if abs(self._clock() - timestamp_value) > MAX_CLOCK_SKEW_SECONDS:
            raise SpotifyBridgeError("Die Bridge-Anfrage ist abgelaufen.")
        message = f"{timestamp_value}.".encode("ascii") + body
        expected = hmac.new(self._secret, message, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, supplied):
            raise SpotifyBridgeError("Die Bridge-Signatur ist ungültig.")


def _voice_identity(raw: object) -> AlexaVoiceIdentity:
    if not isinstance(raw, dict) or set(raw) != {"status", "person_id"}:
        raise SpotifyBridgeError("Die Alexa-Identität ist ungültig.")
    try:
        from smarthome.alexa_spotify import AlexaVoiceIdentityStatus

        return AlexaVoiceIdentity(
            status=AlexaVoiceIdentityStatus(raw["status"]),
            person_id=raw["person_id"],
        )
    except (TypeError, ValueError):
        raise SpotifyBridgeError("Die Alexa-Identität ist ungültig.") from None


def _session(raw: object) -> SpotifySkillSession:
    if not isinstance(raw, dict) or set(raw) != {"profile_id"}:
        raise SpotifyBridgeError("Die Spotify-Sitzung ist ungültig.")
    try:
        return SpotifySkillSession(profile_id=raw["profile_id"])
    except (TypeError, ValueError):
        raise SpotifyBridgeError("Die Spotify-Sitzung ist ungültig.") from None


def _response(result: object) -> SpotifyBridgeResponse:
    status = getattr(getattr(result, "status", None), "value", None)
    if not isinstance(status, str):
        raise SpotifyBridgeError("Die lokale Ausführung lieferte kein gültiges Ergebnis.")
    profile_id = getattr(result, "profile_id", None)
    target_id = getattr(result, "target_id", None)
    if profile_id is not None and not isinstance(profile_id, str):
        raise SpotifyBridgeError("Die lokale Ausführung lieferte ein ungültiges Profil.")
    if target_id is not None and not isinstance(target_id, str):
        raise SpotifyBridgeError("Die lokale Ausführung lieferte ein ungültiges Ziel.")
    return SpotifyBridgeResponse(status, profile_id, target_id)


def make_server(
    bridge: SpotifyBridge,
    *,
    host: str = "127.0.0.1",
    port: int = 8766,
) -> ThreadingHTTPServer:
    """Create, but do not start, the local bridge server."""

    # The add-on runs in an isolated Home Assistant container network. It may
    # bind to all interfaces there so Cloudflared can reach it; arbitrary
    # public binds remain rejected.
    if host not in {"127.0.0.1", "0.0.0.0"}:
        raise SpotifyBridgeError("Die Bridge darf nur lokal oder im Add-on-Netz gebunden werden.")
    if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535:
        raise SpotifyBridgeError("Der Bridge-Port ist ungültig.")

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            length = int(self.headers.get("Content-Length", "-1"))
            body = self.rfile.read(max(0, min(length, MAX_BODY_BYTES + 1)))
            try:
                response = bridge.handle(
                    method="POST", path=self.path, headers=self.headers, body=body
                )
                payload = response.as_mapping()
                status = 200
            except SpotifyBridgeError as exc:
                payload = {"error": str(exc)}
                status = 400
            encoded = json.dumps(payload, ensure_ascii=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    return ThreadingHTTPServer((host, port), Handler)
