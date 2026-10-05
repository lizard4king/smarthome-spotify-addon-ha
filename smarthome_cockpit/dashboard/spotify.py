"""Token-free cockpit boundary for independent Spotify profile requests."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

MAX_JSON = 128 * 1024


class SpotifyCockpitError(RuntimeError):
    pass


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
            raise SpotifyCockpitError("Spotify-Bridge ist nicht konfiguriert.")
        body = json.dumps(value, ensure_ascii=True).encode("utf-8")
        if len(body) > 16 * 1024:
            raise ValueError("Die Spotify-Anfrage ist zu groß.")
        timestamp = str(int(time.time()))
        signature = hmac.new(self.secret.encode(), (timestamp + ".").encode() + body, hashlib.sha256).hexdigest()
        request = Request(self.url + path, data=body, method="POST", headers={
            "Content-Type": "application/json", "User-Agent": "SmartHome Cockpit",
            "X-SmartHome-Timestamp": timestamp, "X-SmartHome-Signature": signature})
        try:
            with self.opener.open(request, timeout=75) as response:
                raw = response.read(MAX_JSON + 1)
        except (HTTPError, URLError, OSError, TimeoutError):
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
                                      else "Spotify hat die Anfrage abgelehnt.")
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
