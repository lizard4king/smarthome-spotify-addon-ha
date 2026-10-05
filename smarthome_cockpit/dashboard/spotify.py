"""Token-free cockpit boundary for independent Spotify profile requests."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

MAX_JSON = 128 * 1024


class SpotifyCockpitError(RuntimeError):
    def __init__(self, message: str, *, outcome="unknown"):
        super().__init__(message)
        self.outcome = outcome if isinstance(outcome, str) and outcome in {"not_sent", "unknown"} else "unknown"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


class SpotifyCockpit:
    def __init__(self):
        self.url = os.environ.get("SPOTIFY_BRIDGE_URL", "").rstrip("/")
        self.secret = os.environ.get("SPOTIFY_BRIDGE_SECRET", "")
        self.opener = build_opener(NoRedirect())

    def request(self, path: str, value: dict) -> dict:
        parsed = urlsplit(self.url)
        if (not self.url or len(self.secret) < 32 or parsed.scheme not in {"http", "https"}
                or not parsed.hostname or parsed.username or parsed.password or parsed.query
                or parsed.fragment or parsed.path not in {"", "/"}):
            raise SpotifyCockpitError("Spotify-Bridge ist nicht konfiguriert.", outcome="not_sent")
        body = json.dumps(value, ensure_ascii=True).encode("utf-8")
        if len(body) > 16 * 1024:
            raise ValueError("Die Spotify-Anfrage ist zu groß.")
        timestamp = str(int(time.time()))
        message = (timestamp + ".").encode() + body
        headers = {"Content-Type": "application/json", "User-Agent": "SmartHome Cockpit",
                   "X-SmartHome-Timestamp": timestamp}
        if path == "/api/spotify/control":
            nonce = secrets.token_hex(16)
            message = f"v2.POST.{path}.{timestamp}.{nonce}.".encode("ascii") + body
            headers.update({"X-SmartHome-Nonce": nonce, "X-SmartHome-Signature-Version": "2"})
        headers["X-SmartHome-Signature"] = hmac.new(self.secret.encode(), message, hashlib.sha256).hexdigest()
        request = Request(self.url + path, data=body, method="POST", headers=headers)
        try:
            with self.opener.open(request, timeout=75) as response:
                raw = response.read(MAX_JSON + 1)
        except HTTPError as exc:
            if path == "/api/spotify/control" and exc.code == 400:
                try:
                    raw_error = exc.read(MAX_JSON + 1)
                    error = json.loads(raw_error) if len(raw_error) <= MAX_JSON else None
                except (ValueError, UnicodeError, OSError, TimeoutError):
                    error = None
                if isinstance(error, dict) and isinstance(error.get("error"), str) and len(error["error"]) <= 512:
                    raise SpotifyCockpitError(error["error"], outcome=error.get("outcome")) from None
            raise SpotifyCockpitError("Spotify-Bridge ist nicht erreichbar oder hat die Anfrage abgelehnt.") from None
        except (URLError, OSError, TimeoutError):
            raise SpotifyCockpitError("Spotify-Bridge ist nicht erreichbar oder hat die Anfrage abgelehnt.") from None
        if len(raw) > MAX_JSON:
            raise SpotifyCockpitError("Die Spotify-Antwort ist zu groß.")
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError):
            raise SpotifyCockpitError("Die Spotify-Antwort ist ungültig.") from None
        if not isinstance(result, dict):
            raise SpotifyCockpitError("Die Spotify-Antwort ist ungültig.")
        if result.get("status") == "failed" and not isinstance(result.get("assignments"), list):
            # The bridge's controlled errors are safe user-facing messages.
            message = result.get("error")
            raise SpotifyCockpitError(message if isinstance(message, str) and len(message) <= 512
                                      else "Spotify hat die Anfrage abgelehnt.", outcome=result.get("outcome"))
        return result

    def status(self) -> dict:
        result = self.request("/api/spotify/status", {})
        if (not isinstance(result.get("profiles"), list) or not isinstance(result.get("targets"), list)
                or type(result.get("available")) is not bool):
            raise SpotifyCockpitError("Die Spotify-Bridge unterstützt den Wiedergabestatus noch nicht.")
        return result

    def play(self, payload: dict) -> dict:
        if "assignments" in payload:
            if set(payload) != {"assignments"}:
                raise ValueError("Unerwartete Felder im Spotify-Auftrag.")
            requests = payload["assignments"]
        else:
            if set(payload) != {"profile", "target", "track"}:
                raise ValueError("Spotify benötigt Profil, Ziel und Titel.")
            requests = [payload]
        result = self.request("/api/spotify/assignments", {"assignments": requests})
        if (result.get("status") not in {"accepted", "partial", "failed"}
                or not isinstance(result.get("assignments"), list)):
            raise SpotifyCockpitError("Spotify hat keinen gültigen Wiedergabeauftrag bestätigt.")
        return result

    def control(self, payload: dict) -> dict:
        action = payload.get("action")
        if not isinstance(action, str) or action not in {"pause", "resume", "next", "previous", "seek"}:
            raise ValueError("Die Spotify-Steueraktion ist ungültig.")
        fields = {"profile", "target", "action"} | ({"position_ms", "track_uri"} if action == "seek" else set())
        if set(payload) != fields or any(not isinstance(payload.get(k), str) or
                not payload[k].strip() or len(payload[k]) > 512 for k in ("profile", "target")):
            raise ValueError("Die Spotify-Steuerung enthält ungültige Felder.")
        if action == "seek" and (type(payload["position_ms"]) is not int or
                                 not 0 <= payload["position_ms"] <= 2_147_483_647):
            raise ValueError("Die Spotify-Position muss eine nichtnegative ganze Millisekundenzahl sein.")
        if action == "seek" and (not isinstance(payload["track_uri"], str) or
                                 not payload["track_uri"].startswith("spotify:track:") or
                                 len(payload["track_uri"]) > 512):
            raise ValueError("Die beobachtete Spotify-Titel-URI fehlt oder ist ungültig.")
        result = self.request("/api/spotify/control", payload)
        if result.get("status") != "accepted" or result.get("playback_verified") is not False:
            raise SpotifyCockpitError("Der Ausgang der Spotify-Steueraktion ist unbekannt. Prüfe den Wiedergabestatus; wiederhole den Auftrag nicht automatisch.")
        return result
