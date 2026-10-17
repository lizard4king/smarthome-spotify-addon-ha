"""Bounded control broker. Authentication precedes parsing and all side effects."""
import hashlib
import hmac
import json
import math
import os
import re
import threading
import time
from urllib.request import Request, build_opener, HTTPRedirectHandler
from dashboard.music_assistant import MusicAssistant
from smarthome.home_assistant import HomeAssistantConfig, HomeAssistantAdapter

LIMIT = 16 * 1024
PATHS = {"/api/codex/context", "/api/codex/control", "/api/codex/speech"}
SPEECH_ROOMS = {"badezimmer", "buero", "kueche", "schlafzimmer", "wohnzimmer"}
NOTIFY_ENTITY = re.compile(r"notify\.[a-z0-9_]+")
ROOM_NAMES = {
    "badezimmer": "Badezimmer",
    "buero": "Büro",
    "kueche": "Küche",
    "schlafzimmer": "Schlafzimmer",
    "wohnzimmer": "Wohnzimmer",
}
ROOM_PHRASES = {
    "Badezimmer": "im Badezimmer",
    "Büro": "im Büro",
    "Küche": "in der Küche",
    "Schlafzimmer": "im Schlafzimmer",
    "Wohnzimmer": "im Wohnzimmer",
}
def _temperature_text(value):
    if type(value) not in {int, float} or not math.isfinite(value):
        return None
    return f"{value:.1f}".replace(".", ",") + " °C"

def _climate_status_receipt(alias, state):
    normalized = alias.casefold().translate(str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"}))
    normalized = re.sub(r"[^a-z0-9]", "", normalized)
    room_key = re.sub(r"heizung$", "", normalized)
    room = ROOM_NAMES.get(room_key, alias.replace("_", " "))
    room_phrase = ROOM_PHRASES.get(room, f"im {room}")
    attributes = state.get("attributes")
    attributes = attributes if isinstance(attributes, dict) else {}
    measured = _temperature_text(attributes.get("current_temperature"))
    target = _temperature_text(attributes.get("temperature"))
    mode = state.get("state")
    if measured is None:
        receipt = f"Die aktuelle Temperatur {room_phrase} ist nicht verfügbar."
    else:
        receipt = f"Es sind aktuell {measured} {room_phrase}."
    if target is not None:
        receipt += f" Die Solltemperatur liegt bei {target}."
        if measured is not None and isinstance(mode, str) and mode == "off":
            receipt += f" Die Heizung {room_phrase} ist ausgeschaltet."
    elif measured is None and isinstance(mode, str) and mode == "heat":
        receipt += f" Die Heizung {room_phrase} ist im Heizmodus."
    return {"receipt": receipt}

def mac(secret, *parts):
    return hmac.new(secret.encode(), "\n".join(parts).encode(), hashlib.sha256).hexdigest()

def decode(raw):
    def unique(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

class ControlBroker:
    def __init__(self, secret, config_factory=HomeAssistantConfig.from_environment, adapter_factory=HomeAssistantAdapter, clock=time.time, cache_clock=time.monotonic):
        self.secret = secret if isinstance(secret, str) and re.fullmatch(r"[A-Za-z0-9_-]{32,128}", secret) else ""
        self.config_factory, self.adapter_factory, self.clock = config_factory, adapter_factory, clock
        self.cache_clock = cache_clock
        self.lock = threading.Lock()
        self.nonces, self.operations = {}, {}

    def music_targets(self):
        try:
            value = decode(os.getenv("CODEX_MUSIC_TARGETS_JSON", "{}"))
            rooms = {"kueche", "wohnzimmer", "badezimmer", "buero", "schlafzimmer"}
            if (not isinstance(value, dict) or not set(value) <= rooms or
                    any(not isinstance(v, str) or not 1 <= len(v) <= 256 or
                        any(ord(c) < 32 for c in v) for v in value.values())):
                return {}
            return value
        except (ValueError, TypeError, RecursionError):
            return {}

    def answer_targets(self):
        """Return only explicit, unique room-to-notify mappings."""
        try:
            value = decode(os.getenv("CODEX_ANSWER_TARGETS_JSON", "{}"))
            if (not isinstance(value, dict) or not value or not set(value) <= SPEECH_ROOMS
                    or any(not isinstance(entity, str) or not NOTIFY_ENTITY.fullmatch(entity)
                           for entity in value.values())
                    or len(set(value.values())) != len(value)):
                return {}
            return value
        except (ValueError, TypeError, RecursionError):
            return {}

    def speak(self, entity, text):
        """Send one already-validated announcement through the HA adapter."""
        config = self.config_factory()
        if not config.allow_writes:
            raise ValueError("Home Assistant writes are disabled")
        if "notify" in config.blocked_domains:
            raise ValueError("Home Assistant notify writes are blocked")
        adapter = self.adapter_factory(config)
        adapter._request("POST", "api/services/notify/send_message", {
            "entity_id": entity,
            "message": text,
        })
        return {"receipt": "Sprachausgabe wurde angenommen."}

    def context(self):
        devices = {}
        try:
            config = self.config_factory()
            # An absent explicit allowlist grants no model-visible capability.
            for alias in sorted(config.write_allowlist or ())[:64]:
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", alias):
                    continue
                entity = config.entities[alias]
                domain = entity.split(".", 1)[0]
                if domain in {"light", "climate"} and domain not in config.blocked_domains:
                    devices[alias] = {"domain": domain, "writable": config.allow_writes}
        except ValueError:
            pass
        profiles = os.getenv("CODEX_SPOTIFY_PROFILES", "").split(",")
        profiles = [p for p in profiles if re.fullmatch(r"[A-Za-z0-9_-]{1,40}", p)][:16]
        result = {"version": 1, "devices": devices, "spotify": {
            "enabled": bool(profiles) and bool(os.getenv("SPOTIFY_BRIDGE_URL")) and bool(re.fullmatch(r"[A-Za-z0-9_-]{32,128}", os.getenv("SPOTIFY_BRIDGE_SECRET", ""))),
            "profiles": profiles, "targets": ["kueche", "wohnzimmer", "badezimmer", "buero", "schlafzimmer"]},
            "operations": ["none", "status", "set_light", "set_temperature", "spotify_play"]}
        targets = self.music_targets()
        music = MusicAssistant()
        if targets and music.configured and music.valid_config and music.allow_playback:
            result["music"] = {"enabled": True, "targets": sorted(targets)}
            result["operations"] += ["music_play", "music_control"]
        return result

    def validate(self, action, context):
        fields = {"none": {"operation"}, "status": {"operation", "alias"}, "set_light": {"operation", "alias", "is_on"}, "set_temperature": {"operation", "alias", "temperature"}, "spotify_play": {"operation", "profile", "target", "query"}, "music_play": {"operation", "target", "query"}, "music_control": {"operation", "target", "command"}}
        if not isinstance(action, dict) or not isinstance(action.get("operation"), str):
            raise ValueError("invalid action")
        op = action["operation"]
        if op not in fields or set(action) != fields[op]:
            raise ValueError("unsupported action")
        if op in {"status", "set_light", "set_temperature"}:
            alias = action["alias"]
            if not isinstance(alias, str) or alias not in context["devices"]:
                raise ValueError("unknown alias")
            device = context["devices"][alias]
            if op != "status" and (not device["writable"] or device["domain"] != ("light" if op == "set_light" else "climate")):
                raise ValueError("write blocked")
        if op == "set_light" and type(action["is_on"]) is not bool:
            raise ValueError("invalid light")
        if op == "set_temperature":
            value = action["temperature"]
            if type(value) not in {int, float} or not math.isfinite(value) or not 10 <= value <= 30:
                raise ValueError("invalid temperature")
        if op == "spotify_play":
            spotify = context["spotify"]
            if (not spotify["enabled"] or not isinstance(action["profile"], str) or action["profile"] not in spotify["profiles"]
                    or not isinstance(action["target"], str) or action["target"] not in spotify["targets"]
                    or not isinstance(action["query"], str) or not 1 <= len(action["query"].strip()) <= 200
                    or any(ord(c) < 32 or c in ":/\\" for c in action["query"])):
                raise ValueError("invalid Spotify request")
        if op in {"music_play", "music_control"}:
            music = context.get("music", {})
            if not music.get("enabled") or action["target"] not in music.get("targets", []):
                raise ValueError("music target unavailable")
            if op == "music_play":
                query = action["query"]
                if (not isinstance(query, str) or not 1 <= len(query.strip()) <= 200 or
                        any(ord(c) < 32 or c in ":/\\" for c in query)):
                    raise ValueError("invalid music query")
            elif not isinstance(action["command"], str) or action["command"] not in {"pause", "resume", "stop"}:
                raise ValueError("invalid music command")
        return op

    def execute(self, action, op):
        if op == "none":
            return {"receipt": "Keine Aktion angefordert."}
        if op in {"status", "set_light", "set_temperature"}:
            config = self.config_factory()
            adapter = self.adapter_factory(config)
            alias = action["alias"]
            if op == "status":
                # Read only the selected mapped entity; never expose whole inventory.
                state = adapter.get_state(alias)
                if config.entities[alias].split(".", 1)[0] == "climate":
                    return _climate_status_receipt(alias, state)
                value = state.get("state") if isinstance(state, dict) else None
                if not isinstance(value, str) or len(value) > 80 or any(ord(c) < 32 for c in value):
                    raise ValueError("invalid status")
                return {"receipt": f"{alias}: {value}."}
            if op == "set_light":
                adapter.set_light(alias, action["is_on"])
                return {"receipt": f"Lichtbefehl für {alias} wurde von Home Assistant angenommen."}
            adapter.set_temperature(alias, action["temperature"])
            return {"receipt": f"Temperaturbefehl für {alias} wurde von Home Assistant angenommen."}
        if op in {"music_play", "music_control"}:
            player = self.music_targets().get(action["target"])
            if not player:
                raise ValueError("music target unavailable")
            music = MusicAssistant()
            if op == "music_control":
                music.control(player, action["command"])
                return {"receipt": "Musikbefehl wurde von Music Assistant angenommen."}
            tracks = music.tracks(action["query"])["tracks"]
            if len(tracks) != 1:
                return {"receipt": "Bitte nenne den Titel genauer." if tracks else "Ich habe keinen passenden eigenen Titel gefunden."}
            music.play(tracks[0]["uri"], player)
            return {"receipt": "Eigene Musik wurde von Music Assistant angenommen."}
        secret = os.environ["SPOTIFY_BRIDGE_SECRET"]
        body = json.dumps({"intent": {"name": "SpotifyPlayIntent", "slots": {
            "MediaQuery": {"value": action["query"]}, "ProfileAlias": {"value": action["profile"]},
            "TargetAlias": {"value": "echo_show" if action["target"] == "wohnzimmer" else action["target"]}, "RememberForSession": {"value": "nein"}}},
            "voice_identity": {"status": "absent", "person_id": None}, "session": {"profile_id": None}}).encode()
        timestamp = str(int(self.clock()))
        signature = hmac.new(secret.encode(), (timestamp + ".").encode() + body, hashlib.sha256).hexdigest()
        request = Request(os.environ["SPOTIFY_BRIDGE_URL"].rstrip("/") + "/api/spotify/command", body, method="POST", headers={"Content-Type": "application/json", "X-SmartHome-Timestamp": timestamp, "X-SmartHome-Signature": signature})
        with build_opener(NoRedirect()).open(request, timeout=5) as response:
            raw = response.read(LIMIT + 1)
            value = decode(raw) if len(raw) <= LIMIT else None
            if (response.status != 200 or not isinstance(value, dict) or value.get("status") != "routed"
                    or response.geturl() != request.full_url
                    or response.headers.get("Content-Type", "").split(";", 1)[0] != "application/json"
                    or response.headers.get("Content-Encoding")):
                raise ValueError("invalid Spotify response")
        return {"receipt": "Spotify-Bridge hat den Wiedergabeauftrag angenommen."}

    def process(self, path, headers, raw):
        nonce = headers.get("X-Control-Nonce", "")
        status, result = 403, {"error": "Control request rejected."}
        try:
            if (not self.secret or path not in PATHS or len(raw) > LIMIT
                    or headers.get("Content-Type") != "application/json" or headers.get("Content-Encoding")
                    or headers.get("Transfer-Encoding") or headers.get("Origin")):
                raise ValueError("rejected")
            timestamp = headers.get("X-Control-Timestamp", "")
            if not re.fullmatch(r"[0-9]{1,12}", timestamp) or abs(self.clock() - int(timestamp)) > 30 or not re.fullmatch(r"[0-9a-f]{32}", nonce):
                raise ValueError("rejected")
            expected = mac(self.secret, "POST", path, timestamp, nonce, hashlib.sha256(raw).hexdigest())
            if not hmac.compare_digest(expected, headers.get("X-Control-Signature", "")):
                raise ValueError("rejected")
            execute_job = None
            with self.lock:
                now = self.cache_clock()
                self.nonces = {key: expiry for key, expiry in self.nonces.items() if expiry > now}
                if nonce in self.nonces or len(self.nonces) >= 256:
                    raise ValueError("replayed")
                self.nonces[nonce] = now + 61
                payload = decode(raw)
                expected_keys = ({"request_id"} if path.endswith("context") else
                                 {"request_id", "target", "text"} if path.endswith("speech") else
                                 {"request_id", "action"})
                if not isinstance(payload, dict) or set(payload) != expected_keys or not isinstance(payload.get("request_id"), str) or not re.fullmatch(r"[0-9a-f]{32}", payload["request_id"]):
                    raise ValueError("invalid payload")
                if path.endswith("context"):
                    context = self.context()
                    status, result = 200, context
                elif path.endswith("speech"):
                    targets = self.answer_targets()
                    target, text = payload["target"], payload["text"]
                    if (not isinstance(target, str) or target not in targets
                            or not isinstance(text, str) or not 1 <= len(text) <= 2000):
                        raise ValueError("invalid speech request")
                    if not text.strip() or any(ord(char) < 32 for char in text):
                        raise ValueError("invalid speech text")
                    config = self.config_factory()
                    if not config.allow_writes or "notify" in config.blocked_domains:
                        raise ValueError("writes disabled")
                    self.operations = {key: value for key, value in self.operations.items() if value[0] > now}
                    key, digest = payload["request_id"], hashlib.sha256(raw).hexdigest()
                    previous = self.operations.get(key)
                    if previous:
                        if previous[1] != digest:
                            raise ValueError("changed operation")
                        status, result = previous[2], previous[3]
                    else:
                        if len(self.operations) >= 256:
                            raise ValueError("capacity")
                        status, result = 503, {"error": "Ausführung nicht bestätigt; keine automatische Wiederholung."}
                        self.operations[key] = (now + 600, digest, status, result)
                        execute_job = ("speech", key, digest, targets[target], text)
                else:
                    context = self.context()
                    op = self.validate(payload["action"], context)
                    self.operations = {key: value for key, value in self.operations.items() if value[0] > now}
                    key, digest = payload["request_id"], hashlib.sha256(raw).hexdigest()
                    previous = self.operations.get(key)
                    if previous:
                        if previous[1] != digest:
                            raise ValueError("changed operation")
                        status, result = previous[2], previous[3]
                    else:
                        if len(self.operations) >= 256:
                            raise ValueError("capacity")
                        # Reserve and retain ambiguity before the downstream effect.
                        status, result = 503, {"error": "Ausführung nicht bestätigt; keine automatische Wiederholung."}
                        self.operations[key] = (now + 600, digest, status, result)
                        execute_job = (key, digest, payload["action"], op)
            if execute_job is not None:
                if execute_job[0] == "speech":
                    _, key, digest, entity, text = execute_job
                    try:
                        result = self.speak(entity, text)
                        status = 200
                    except Exception:
                        pass
                    with self.lock:
                        self.operations[key] = (self.cache_clock() + 600, digest, status, result)
                    execute_job = None
                else:
                    key, digest, action, op = execute_job
            if execute_job is not None:
                try:
                    result = self.execute(action, op)
                    status = 200
                except Exception:
                    pass
                with self.lock:
                    self.operations[key] = (self.cache_clock() + 600, digest, status, result)
        except (ValueError, TypeError, RecursionError):
            pass
        body = json.dumps(result, separators=(",", ":"), ensure_ascii=True).encode()
        signature = mac(self.secret, "RESPONSE", path, nonce, str(status), hashlib.sha256(body).hexdigest()) if self.secret else ""
        return status, body, signature

BROKER = ControlBroker(os.getenv("CODEX_CONTROL_SECRET", ""))

def handle(handler):
    try:
        handler.connection.settimeout(5)
        if any(len(handler.headers.get_all(key, [])) != 1 for key in ("Content-Length", "Content-Type", "X-Control-Timestamp", "X-Control-Nonce", "X-Control-Signature")):
            raise ValueError("duplicate or missing header")
        length = int(handler.headers.get("Content-Length", "-1"))
        if not 0 <= length <= LIMIT or handler.headers.get("Transfer-Encoding"):
            raise ValueError("invalid length")
        raw = handler.rfile.read(length)
        if len(raw) != length:
            raise ValueError("short body")
        status, body, signature = BROKER.process(handler.path, handler.headers, raw)
    except (ValueError, OSError):
        status, body, signature = 400, b'{"error":"Control request rejected."}', ""
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("X-Control-Signature", signature)
    handler.end_headers()
    handler.wfile.write(body)
