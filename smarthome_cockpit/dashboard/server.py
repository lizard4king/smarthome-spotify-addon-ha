"""Local cockpit server with an explicit, environment-gated live mode.

The default is read-only/simulation. Home-Assistant writes and Spotify bridge
calls are possible only when the existing environment configuration is present
and ``HOME_ASSISTANT_ALLOW_WRITES=true`` is set for device writes.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen
from urllib.parse import parse_qs, urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from smarthome.home_assistant import HomeAssistantAdapter, HomeAssistantConfig, HomeAssistantError
from dashboard.music_assistant import MusicAssistant, MusicAssistantError
from dashboard.codex_control import handle as handle_codex_control
from dashboard.spotify import SpotifyCockpit, SpotifyCockpitError


ROOT = Path(__file__).resolve().parent
MAX_BODY = 16 * 1024


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True).encode("utf-8")


class CockpitHandler(SimpleHTTPRequestHandler):
    """Serve the cockpit and a deliberately small API surface."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def _send_json(self, status: int, payload: object) -> None:
        encoded = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        if not urlsplit(self.path).path.startswith(("/api/music/", "/api/codex/", "/api/spotify")):
            self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_OPTIONS(self) -> None:  # noqa: N802 - stdlib API
        if urlsplit(self.path).path.startswith(("/api/music/", "/api/codex/", "/api/spotify")):
            self._send_json(405, {"error": "Cross-Origin-Musikzugriff ist nicht freigegeben."})
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802 - stdlib API
        if urlsplit(self.path).path == "/api/spotify/status":
            origin = self.headers.get("Origin")
            if (self.headers.get("Sec-Fetch-Site") == "cross-site" or (origin and (
                    urlsplit(origin).scheme not in {"http", "https"} or
                    urlsplit(origin).netloc != self.headers.get("Host")))):
                self._send_json(403, {"available": False, "error": "Cross-Origin-Spotifyzugriff ist nicht freigegeben."})
                return
            try:
                self._send_json(200, SpotifyCockpit().status())
            except SpotifyCockpitError as exc:
                self._send_json(503, {"available": False, "error": str(exc), "profiles": [], "targets": [], "sessions": []})
            return
        if urlsplit(self.path).path.startswith("/api/codex/"):
            self._send_json(404, {"error": "POST required."})
            return
        if urlsplit(self.path).path.startswith("/api/music/"):
            self._handle_music_get()
            return
        if self.path == "/api/capabilities":
            ha_ready = bool(os.getenv("HOME_ASSISTANT_TOKEN"))
            spotify_ready = bool(os.getenv("SPOTIFY_BRIDGE_SECRET"))
            self._send_json(200, {
                "mode": "live" if ha_ready or spotify_ready else "simulation",
                "home_assistant": ha_ready,
                "home_assistant_writes": ha_ready and os.getenv("HOME_ASSISTANT_ALLOW_WRITES", "").casefold() == "true",
                "spotify_bridge": spotify_ready,
            })
            return
        if self.path == "/api/ha/entities":
            try:
                adapter = HomeAssistantAdapter(HomeAssistantConfig.from_environment())
                entities = adapter.discover_entities()
            except HomeAssistantError as exc:
                self._send_json(503, {"error": str(exc)})
                return
            selected = [
                {"entity_id": item.get("entity_id"), "state": item.get("state"), "name": item.get("attributes", {}).get("friendly_name")}
                for item in entities
                if isinstance(item.get("entity_id"), str) and item["entity_id"].split(".", 1)[0] in {"light", "climate", "sensor", "media_player", "camera"}
            ][:512]
            self._send_json(200, {"entities": selected})
            return
        if self.path.startswith("/api/ha/camera_image"):
            query = parse_qs(urlsplit(self.path).query)
            entity_id = query.get("entity_id", [""])[0]
            if not isinstance(entity_id, str) or not entity_id.startswith("camera."):
                self._send_json(400, {"error": "Ungültiges Kamera-Ziel."})
                return
            try:
                config = HomeAssistantConfig.from_environment()
                adapter = HomeAssistantAdapter(config)
                image = adapter.camera_image(entity_id)
            except HomeAssistantError as exc:
                self._send_json(503, {"error": str(exc)})
                return
            self.send_response(200)
            self.send_header("Content-Type", image["content_type"])
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Content-Length", str(len(image["body"])))
            self.end_headers()
            self.wfile.write(image["body"])
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802 - stdlib API
        if urlsplit(self.path).path.startswith("/api/codex/"):
            handle_codex_control(self)
            return
        if self.path not in {"/api/action", "/api/alexa/speak", "/api/spotify", "/api/spotify/search", "/api/spotify/control", "/api/music/play", "/api/music/control"}:
            self._send_json(404, {"error": "Unbekannter Cockpit-Endpunkt."})
            return
        try:
            if self.path.startswith("/api/spotify"):
                if self.headers.get("Sec-Fetch-Site") == "cross-site":
                    self._send_json(403, {"error": "Cross-Origin-Spotifyzugriff ist nicht freigegeben.",
                                         **({"outcome": "not_sent"} if self.path == "/api/spotify/control" else {})})
                    return
                origin = self.headers.get("Origin")
                if origin:
                    parsed = urlsplit(origin)
                    if parsed.scheme not in {"http", "https"} or parsed.netloc != self.headers.get("Host"):
                        self._send_json(403, {"error": "Cross-Origin-Spotifyzugriff ist nicht freigegeben.",
                                             **({"outcome": "not_sent"} if self.path == "/api/spotify/control" else {})})
                        return
                if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
                    raise ValueError("Spotify-Befehle benötigen application/json.")
            length = int(self.headers.get("Content-Length", "-1"))
            if length < 0 or length > MAX_BODY:
                raise ValueError("Die Anfrage ist zu groß oder ungültig.")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("Die Anfrage muss ein JSON-Objekt sein.")
            if self.path.startswith("/api/music/"):
                self._handle_music_post(payload)
            elif self.path == "/api/action":
                self._handle_action(payload)
            elif self.path == "/api/alexa/speak":
                self._handle_alexa_speak(payload)
            elif self.path == "/api/spotify":
                self._handle_spotify(payload)
            elif self.path == "/api/spotify/control":
                self._handle_spotify_control(payload)
            else:
                self._handle_spotify_search(payload)
        except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
            error = {"error": str(exc)}
            if self.path == "/api/spotify/control":
                error["outcome"] = "not_sent"
            self._send_json(400, error)

    def _handle_music_get(self) -> None:
        client = MusicAssistant()
        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query)
        try:
            if parsed.path == "/api/music/status":
                result = client.status()
            elif parsed.path == "/api/music/players":
                result = {"players": client.players()}
            elif parsed.path == "/api/music/tracks":
                result = client.tracks(query.get("q", [""])[0], int(query.get("offset", ["0"])[0]))
            elif parsed.path == "/api/music/artwork":
                body, content_type = client.artwork(query.get("uri", [""])[0])
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Cache-Control", "private, max-age=300")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            else:
                self._send_json(404, {"error": "Unbekannter Musik-Endpunkt."})
                return
            self._send_json(200, result)
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
        except MusicAssistantError as exc:
            self._send_json(503, {"available": False, "error": str(exc)})

    def _handle_music_post(self, payload: dict[str, object]) -> None:
        # Reject browser cross-origin writes, including simple form requests.
        origin = self.headers.get("Origin")
        if origin:
            parsed = urlsplit(origin)
            if parsed.scheme not in {"http", "https"} or parsed.netloc != self.headers.get("Host"):
                self._send_json(403, {"error": "Cross-Origin-Musikzugriff ist nicht freigegeben."})
                return
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
            raise ValueError("Musikbefehle benötigen application/json.")
        client = MusicAssistant()
        try:
            if self.path == "/api/music/play":
                result = client.play(payload.get("uri"), payload.get("player_id"))
            else:
                result = client.control(payload.get("player_id"), payload.get("command"))
            self._send_json(200, result)
        except MusicAssistantError as exc:
            self._send_json(503, {"available": False, "error": str(exc)})

    def _handle_action(self, payload: dict[str, object]) -> None:
        if os.getenv("HOME_ASSISTANT_ALLOW_WRITES", "").casefold() != "true":
            self._send_json(409, {"error": "Home-Assistant-Schreibzugriffe sind nicht freigegeben.", "mode": "simulation"})
            return
        try:
            config = HomeAssistantConfig.from_environment()
            adapter = HomeAssistantAdapter(config)
            action = payload.get("action")
            device_id = payload.get("device_id")
            if action == "set_light" and isinstance(device_id, str) and isinstance(payload.get("is_on"), bool):
                adapter.set_light(device_id, payload["is_on"])
            elif action == "set_temperature" and isinstance(device_id, str) and isinstance(payload.get("temperature"), (int, float)):
                adapter.set_temperature(device_id, float(payload["temperature"]))
            else:
                raise ValueError("Unbekannte oder unvollständige Geräteaktion.")
        except (HomeAssistantError, ValueError) as exc:
            self._send_json(409, {"error": str(exc)})
            return
        self._send_json(200, {"status": "ok", "mode": "live"})

    def _handle_alexa_speak(self, payload: dict[str, object]) -> None:
        if os.getenv("HOME_ASSISTANT_ALLOW_WRITES", "").casefold() != "true":
            self._send_json(409, {"error": "Home-Assistant-Schreibzugriffe sind nicht freigegeben.", "mode": "simulation"})
            return
        target = payload.get("target")
        message = payload.get("message")
        if not isinstance(target, str) or not isinstance(message, str):
            raise ValueError("Alexa-Ziel und Ansagetext sind Pflichtfelder.")
        try:
            config = HomeAssistantConfig.from_environment()
            try:
                device_map = json.loads(os.getenv("HOME_ASSISTANT_ALEXA_DEVICES_JSON", "{}"))
            except json.JSONDecodeError as exc:
                raise HomeAssistantError("Die Alexa-Gerätezuordnung ist ungültig.") from exc
            device_id = device_map.get(target)
            if not isinstance(device_id, str):
                raise HomeAssistantError("Für dieses Echo ist keine Alexa-Geräte-ID hinterlegt.")
            HomeAssistantAdapter(config).send_alexa_text_command(device_id, message)
        except (HomeAssistantError, ValueError) as exc:
            self._send_json(409, {"error": str(exc)})
            return
        self._send_json(200, {"status": "ok", "mode": "live", "target": target})

    def _handle_spotify(self, payload: dict[str, object]) -> None:
        try:
            result = SpotifyCockpit().play(payload)
            self._send_json(200 if result["status"] != "failed" else 409, {**result, "mode": "live"})
        except SpotifyCockpitError as exc:
            self._send_json(409, {"error": str(exc), "mode": "live"})

    def _handle_spotify_control(self, payload: dict[str, object]) -> None:
        try:
            result = SpotifyCockpit().control(payload)
            self._send_json(200, {**result, "mode": "live"})
        except SpotifyCockpitError as exc:
            self._send_json(409, {"error": str(exc), "mode": "live", "outcome": exc.outcome})

    def _handle_spotify_search(self, payload: dict[str, object]) -> None:
        bridge_url = os.getenv("SPOTIFY_BRIDGE_URL", "").rstrip("/")
        secret = os.getenv("SPOTIFY_BRIDGE_SECRET", "")
        if not bridge_url or len(secret) < 32:
            self._send_json(409, {"error": "Spotify-Bridge ist nicht konfiguriert.", "mode": "simulation"})
            return
        profile = payload.get("profile")
        query = payload.get("query")
        if not all(isinstance(item, str) and item.strip() for item in (profile, query)):
            raise ValueError("Spotify-Profil und Suchtext sind Pflichtfelder.")
        body = _json_bytes({"profile_alias": profile, "query": query})
        timestamp = str(int(time.time()))
        signature = hmac.new(secret.encode("utf-8"), (timestamp + ".").encode() + body, hashlib.sha256).hexdigest()
        request = Request(bridge_url + "/api/spotify/search", data=body, method="POST", headers={
            "Content-Type": "application/json", "User-Agent": "Mozilla/5.0 (SmartHome Cockpit)",
            "X-SmartHome-Timestamp": timestamp, "X-SmartHome-Signature": signature,
        })
        try:
            with urlopen(request, timeout=15) as response:
                result = json.loads(response.read(64 * 1024).decode("utf-8"))
        except (OSError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            self._send_json(503, {"error": "Spotify-Suche ist nicht erreichbar."})
            return
        self._send_json(200, result)


def main() -> None:
    # These are local, non-secret defaults. Tokens remain process-only inputs.
    os.environ.setdefault("HOME_ASSISTANT_URL", "http://homeassistant.local:8123")
    os.environ.setdefault("SPOTIFY_BRIDGE_URL", "http://homeassistant.local:8766")
    host = os.getenv("COCKPIT_HOST", "0.0.0.0")
    port = int(os.getenv("COCKPIT_PORT", "8767"))
    with ThreadingHTTPServer((host, port), CockpitHandler) as server:
        print(f"Smart Home Cockpit: http://{host}:{port}/")
        server.serve_forever()


if __name__ == "__main__":
    main()
