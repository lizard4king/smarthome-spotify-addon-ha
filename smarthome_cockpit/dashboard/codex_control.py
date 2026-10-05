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
from smarthome.home_assistant import HomeAssistantConfig, HomeAssistantAdapter

LIMIT = 16 * 1024
PATHS = {"/api/codex/context", "/api/codex/control"}

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
        return {"version": 1, "devices": devices, "spotify": {
            "enabled": bool(profiles) and bool(os.getenv("SPOTIFY_BRIDGE_URL")) and bool(re.fullmatch(r"[A-Za-z0-9_-]{32,128}", os.getenv("SPOTIFY_BRIDGE_SECRET", ""))),
            "profiles": profiles, "targets": ["kueche", "wohnzimmer", "badezimmer", "buero", "schlafzimmer"]},
            "operations": ["none", "status", "set_light", "set_temperature", "spotify_play"]}

    def validate(self, action, context):
        fields = {"none": {"operation"}, "status": {"operation", "alias"}, "set_light": {"operation", "alias", "is_on"}, "set_temperature": {"operation", "alias", "temperature"}, "spotify_play": {"operation", "profile", "target", "query"}}
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
                state = adapter._request("GET", "api/states/" + config.entities[alias], None)
                value = state.get("state") if isinstance(state, dict) else None
                if not isinstance(value, str) or len(value) > 80 or any(ord(c) < 32 for c in value):
                    raise ValueError("invalid status")
                return {"receipt": f"{alias}: {value}."}
            if op == "set_light":
                adapter.set_light(alias, action["is_on"])
                return {"receipt": f"Lichtbefehl für {alias} wurde von Home Assistant angenommen."}
            adapter.set_temperature(alias, action["temperature"])
            return {"receipt": f"Temperaturbefehl für {alias} wurde von Home Assistant angenommen."}
        secret = os.environ["SPOTIFY_BRIDGE_SECRET"]
        body = json.dumps({"intent": {"name": "SpotifyPlayIntent", "slots": {
            "MediaQuery": {"value": action["query"]}, "ProfileAlias": {"value": action["profile"]},
            "TargetAlias": {"value": action["target"]}, "RememberForSession": {"value": "nein"}}},
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
            with self.lock:
                now = self.cache_clock()
                self.nonces = {key: expiry for key, expiry in self.nonces.items() if expiry > now}
                if nonce in self.nonces or len(self.nonces) >= 256:
                    raise ValueError("replayed")
                self.nonces[nonce] = now + 61
                payload = decode(raw)
                expected_keys = {"request_id"} if path.endswith("context") else {"request_id", "action"}
                if not isinstance(payload, dict) or set(payload) != expected_keys or not isinstance(payload.get("request_id"), str) or not re.fullmatch(r"[0-9a-f]{32}", payload["request_id"]):
                    raise ValueError("invalid payload")
                context = self.context()
                if path.endswith("context"):
                    status, result = 200, context
                else:
                    op = self.validate(payload["action"], context)
                    self.operations = {key: value for key, value in self.operations.items() if value[0] > now}
                    key, digest = payload["request_id"], hashlib.sha256(raw).hexdigest()
                    previous = self.operations.get(key)
                    if previous:
                        if previous[1] != digest:
                            raise ValueError("changed operation")
                        status, result = previous[2], previous[3]
                    else:
                        if len(self.operations) >= 32:
                            raise ValueError("capacity")
                        # Reserve and retain ambiguity before the downstream effect.
                        status, result = 503, {"error": "Ausführung nicht bestätigt; keine automatische Wiederholung."}
                        self.operations[key] = (now + 600, digest, status, result)
                        try:
                            result = self.execute(payload["action"], op)
                            status = 200
                        except Exception:
                            pass
                        self.operations[key] = (now + 600, digest, status, result)
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
