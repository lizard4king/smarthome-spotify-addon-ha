"""Authenticated local HTTP boundary for structured Spotify commands."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Protocol

from smarthome.alexa_spotify import AlexaVoiceIdentity, SpotifySkillSession
from smarthome.spotify_alexa_commands import parse_spotify_alexa_intent
from smarthome.spotify_cockpit import SpotifyCockpitError, validate_control


MAX_BODY_BYTES = 16 * 1024
MAX_CLOCK_SKEW_SECONDS = 300
MAX_CONTROL_NONCES = 4096
BRIDGE_PATH = "/api/spotify/command"
SEARCH_PATH = "/api/spotify/search"
STATUS_PATH = "/api/spotify/status"
ASSIGNMENTS_PATH = "/api/spotify/assignments"
CONTROL_PATH = "/api/spotify/control"
HEALTH_PATH = "/health"


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

    def search(self, profile_alias: str, query: str, *, now: datetime) -> object:
        """Search one explicitly selected profile without playback."""

    def status(self, *, now: datetime) -> dict[str, object]:
        """Return observed, token-free profile/player metadata."""

    def play_assignments(self, assignments: object, *, now: datetime) -> dict[str, object]:
        """Start distinct-account, distinct-target assignments after preflight."""

    def control(self, payload: dict, *, now: datetime) -> dict[str, object]:
        """Control one verified profile/device after authenticated preflight."""


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
        self._control_nonces: dict[str, int] = {}
        self._control_nonce_lock = threading.Lock()

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

    def handle_search(
        self,
        *,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> dict[str, object]:
        if method.upper() != "POST" or path != SEARCH_PATH:
            raise SpotifyBridgeError("Der Spotify-Suchendpunkt ist nicht verfügbar.")
        if len(body) > MAX_BODY_BYTES:
            raise SpotifyBridgeError("Die Spotify-Suchanfrage ist zu groß.")
        self._verify_signature(headers, body)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise SpotifyBridgeError("Die Spotify-Suchanfrage ist kein gültiges JSON.") from None
        if not isinstance(payload, dict) or set(payload) != {"profile_alias", "query"}:
            raise SpotifyBridgeError("Die Spotify-Suchanfrage enthält unerwartete Felder.")
        profile_alias = payload.get("profile_alias")
        query = payload.get("query")
        if not isinstance(profile_alias, str) or not profile_alias.strip() or not isinstance(query, str) or not query.strip():
            raise SpotifyBridgeError("Spotify-Profil und Suchtext sind erforderlich.")
        result = self._dispatcher.search(profile_alias.strip(), query.strip(), now=datetime.now(UTC))
        results = []
        for item in result:
            results.append({
                "uri": getattr(item, "uri", None),
                "title": getattr(item, "title", None),
                "subtitle": getattr(item, "subtitle", None),
                "image_url": getattr(item, "image_url", None),
            })
        return {"status": "ok", "results": results}

    def _verify_signature(
        self, headers: Mapping[str, str], body: bytes, *, method: str = "POST", path: str = BRIDGE_PATH
    ) -> None:
        signature = headers.get("X-SmartHome-Signature", "")
        timestamp = headers.get("X-SmartHome-Timestamp", "")
        nonce = headers.get("X-SmartHome-Nonce", "")
        control = path == CONTROL_PATH
        if control and (
            headers.get("X-SmartHome-Signature-Version") != "2"
            or not isinstance(timestamp, str)
            or re.fullmatch(r"[0-9]{1,12}", timestamp) is None
            or not isinstance(nonce, str)
            or re.fullmatch(r"[0-9a-f]{32}", nonce) is None
        ):
            raise SpotifyBridgeError("Die Bridge-Signatur ist ungültig.")
        try:
            timestamp_value = int(timestamp)
            supplied = bytes.fromhex(signature)
        except (TypeError, ValueError):
            raise SpotifyBridgeError("Die Bridge-Signatur ist ungültig.") from None
        now = self._clock()
        if abs(now - timestamp_value) > MAX_CLOCK_SKEW_SECONDS:
            raise SpotifyBridgeError("Die Bridge-Anfrage ist abgelaufen.")
        message = f"{timestamp_value}.".encode("ascii") + body
        if control:
            if method != "POST" or timestamp != str(timestamp_value):
                raise SpotifyBridgeError("Die Bridge-Signatur ist ungültig.")
            message = f"v2.{method}.{path}.{timestamp}.{nonce}.".encode("ascii") + body
        expected = hmac.new(self._secret, message, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, supplied):
            raise SpotifyBridgeError("Die Bridge-Signatur ist ungültig.")
        if control:
            # Claim only authenticated requests. Keep future-dated nonces until
            # their complete acceptance window closes; never evict live entries.
            with self._control_nonce_lock:
                now = self._clock()
                if abs(now - timestamp_value) > MAX_CLOCK_SKEW_SECONDS:
                    raise SpotifyBridgeError("Die Bridge-Anfrage ist abgelaufen.")
                expired = [key for key, expires in self._control_nonces.items() if expires <= now]
                for key in expired:
                    del self._control_nonces[key]
                if nonce in self._control_nonces:
                    raise SpotifyBridgeError("Die Spotify-Steueranfrage wurde bereits verwendet.")
                if len(self._control_nonces) >= MAX_CONTROL_NONCES:
                    raise SpotifyBridgeError("Die Spotify-Steuerung ist vorübergehend ausgelastet.")
                self._control_nonces[nonce] = timestamp_value + MAX_CLOCK_SKEW_SECONDS + 1

    def handle_cockpit(self, *, method: str, path: str, headers: Mapping[str, str], body: bytes) -> dict:
        """Authenticated status or independent assignments; no token fields."""
        if method.upper() != "POST" or path not in {STATUS_PATH, ASSIGNMENTS_PATH, CONTROL_PATH}:
            raise SpotifyBridgeError("Der Cockpit-Endpunkt ist nicht verfügbar.")
        if len(body) > MAX_BODY_BYTES:
            raise SpotifyBridgeError("Die Cockpit-Anfrage ist zu groß.")
        self._verify_signature(headers, body, method=method, path=path)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeError, ValueError):
            raise SpotifyBridgeError("Die Cockpit-Anfrage ist ungültig.") from None
        if path == STATUS_PATH:
            if payload != {}:
                raise SpotifyBridgeError("Die Statusanfrage enthält unerwartete Felder.")
            return self._dispatcher.status(now=datetime.now(UTC))
        if path == CONTROL_PATH:
            return self._dispatcher.control(validate_control(payload), now=datetime.now(UTC))
        if not isinstance(payload, dict) or set(payload) != {"assignments"}:
            raise SpotifyBridgeError("Die Cockpit-Anfrage enthält unerwartete Felder.")
        return self._dispatcher.play_assignments(payload["assignments"], now=datetime.now(UTC))


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
    # bind to all interfaces there so the caller can reach the published port;
    # arbitrary public binds remain rejected.
    if host not in {"127.0.0.1", "0.0.0.0"}:
        raise SpotifyBridgeError("Die Bridge darf nur lokal oder im Add-on-Netz gebunden werden.")
    if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535:
        raise SpotifyBridgeError("Der Bridge-Port ist ungültig.")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            if self.path != HEALTH_PATH:
                self.send_error(404)
                return
            encoded = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            length = int(self.headers.get("Content-Length", "-1"))
            body = self.rfile.read(max(0, min(length, MAX_BODY_BYTES + 1)))
            try:
                if self.path in {STATUS_PATH, ASSIGNMENTS_PATH, CONTROL_PATH}:
                    payload = bridge.handle_cockpit(method="POST", path=self.path, headers=self.headers, body=body)
                elif self.path == SEARCH_PATH:
                    payload = bridge.handle_search(method="POST", path=self.path, headers=self.headers, body=body)
                else:
                    response = bridge.handle(
                        method="POST", path=self.path, headers=self.headers, body=body
                    )
                    payload = response.as_mapping()
                status = 200
            except SpotifyBridgeError as exc:
                payload = {"error": str(exc)}
                if self.path == CONTROL_PATH:
                    payload["outcome"] = "not_sent"
                status = 400
            except RuntimeError as exc:
                # Playback/provider failures must not tear down the HTTP
                # connection.  Return a stable application-level result so
                # callers can report the failure instead of seeing a proxy
                # timeout (for example Cloudflare 524).
                payload = {"status": "failed", "error": str(exc)}
                if self.path == CONTROL_PATH:
                    if isinstance(exc, SpotifyCockpitError):
                        payload["outcome"] = exc.outcome
                    else:
                        payload = {"status": "failed", "outcome": "unknown",
                                   "error": "Der Ausgang der Spotify-Steueraktion ist unbekannt. Prüfe den Wiedergabestatus."}
                status = 200
            encoded = json.dumps(payload, ensure_ascii=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    return ThreadingHTTPServer((host, port), Handler)
