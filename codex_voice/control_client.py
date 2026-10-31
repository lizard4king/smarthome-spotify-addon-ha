"""Fixed-origin, authenticated control boundary; never passes credentials to Codex."""
import hashlib
import hmac
import json
import math
import re
import secrets
import time
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError

ORIGIN = "http://7b071411-smarthome-cockpit:8767"
LIMIT = 16 * 1024
TIMEOUT_SECONDS = 12

def decode(raw):
    def unique(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))

def mac(secret, *parts):
    return hmac.new(secret.encode(), "\n".join(parts).encode(), hashlib.sha256).hexdigest()

def validate_action(action, context):
    if not isinstance(action, dict):
        raise ValueError("invalid action")
    op = action.get("operation")
    fields = {"none": {"operation"}, "status": {"operation", "alias"},
              "set_light": {"operation", "alias", "is_on"},
              "set_temperature": {"operation", "alias", "temperature"},
              "spotify_play": {"operation", "profile", "target", "query"}, "music_play": {"operation", "target", "query"}, "music_control": {"operation", "target", "command"}}
    if not isinstance(op, str) or op not in fields or set(action) != fields[op]:
        raise ValueError("unsupported action")
    if op in {"status", "set_light", "set_temperature"}:
        alias = action["alias"]
        devices = context.get("devices", {})
        if not isinstance(alias, str) or alias not in devices:
            raise ValueError("unknown alias")
        if op != "status" and (not devices[alias].get("writable") or devices[alias].get("domain") != ("light" if op == "set_light" else "climate")):
            raise ValueError("write blocked")
    if op == "set_light" and type(action["is_on"]) is not bool:
        raise ValueError("invalid light value")
    if op == "set_temperature":
        value = action["temperature"]
        if type(value) not in {int, float} or not math.isfinite(value) or not 10 <= value <= 30:
            raise ValueError("invalid temperature")
    if op == "spotify_play":
        spotify = context.get("spotify", {})
        if (not spotify.get("enabled") or not isinstance(action["profile"], str)
                or action["profile"] not in spotify.get("profiles", [])
                or not isinstance(action["target"], str) or action["target"] not in spotify.get("targets", [])
                or not isinstance(action["query"], str) or not 1 <= len(action["query"].strip()) <= 200
                or any(ord(c) < 32 for c in action["query"])
                or ":" in action["query"] or "/" in action["query"] or "\\" in action["query"]):
            raise ValueError("invalid spotify request")
    if op in {"music_play", "music_control"}:
        music = context.get("music", {})
        if not music.get("enabled") or action["target"] not in music.get("targets", []):
            raise ValueError("music unavailable")
        if op == "music_play":
            q = action["query"]
            if not isinstance(q, str) or not 1 <= len(q.strip()) <= 200 or any(ord(c) < 32 or c in ":/\\" for c in q):
                raise ValueError("invalid music query")
        elif not isinstance(action["command"], str) or action["command"] not in {"pause", "resume", "stop"}:
            raise ValueError("invalid music command")
    return action

def validate_plan(raw, context):
    value = decode(raw)
    if not isinstance(value, dict) or set(value) != {"answer", "action"} or not isinstance(value["answer"], str) or len(value["answer"]) > 2000:
        raise ValueError("invalid plan")
    # Clarifications stay inside the runtime; they are never a broker operation.
    if value["action"] == {"operation": "clarify"}:
        if not value["answer"].strip():
            raise ValueError("empty answer")
        return value
    validate_action(value["action"], context)
    if value["action"]["operation"] == "none" and not value["answer"].strip():
        raise ValueError("empty answer")
    return value

def validate_context(value):
    if (not isinstance(value, dict) or set(value) not in ({"version", "devices", "spotify", "operations"}, {"version", "devices", "spotify", "operations", "music"})
            or type(value["version"]) is not int or value["version"] != 1):
        raise ValueError("invalid capabilities")
    devices = value["devices"]
    if not isinstance(devices, dict) or len(devices) > 64:
        raise ValueError("invalid capabilities")
    for alias, device in devices.items():
        if (not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", alias)
                or not isinstance(device, dict) or set(device) != {"domain", "writable"}
                or not isinstance(device["domain"], str) or device["domain"] not in {"light", "climate"} or type(device["writable"]) is not bool):
            raise ValueError("invalid capability")
    spotify = value["spotify"]
    if (not isinstance(spotify, dict) or set(spotify) != {"enabled", "profiles", "targets"}
            or type(spotify["enabled"]) is not bool):
        raise ValueError("invalid spotify capability")
    for key, bound in (("profiles", 16), ("targets", 5)):
        aliases = spotify[key]
        if not isinstance(aliases, list) or len(aliases) > bound or any(not isinstance(alias, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", alias) for alias in aliases):
            raise ValueError("invalid spotify aliases")
    if spotify["targets"] != ["kueche", "wohnzimmer", "badezimmer", "buero", "schlafzimmer"]:
        raise ValueError("invalid targets")
    expected = ["none", "status", "set_light", "set_temperature", "spotify_play"]
    if "music" in value:
        music = value["music"]
        if (not isinstance(music, dict) or set(music) != {"enabled", "targets"} or
                type(music["enabled"]) is not bool or not isinstance(music["targets"], list) or
                not music["targets"] or any(not isinstance(t, str) for t in music["targets"]) or
                len(set(music["targets"])) != len(music["targets"]) or
                any(t not in spotify["targets"] for t in music["targets"])):
            raise ValueError("invalid music capabilities")
        expected += ["music_play", "music_control"]
    if value["operations"] != expected:
        raise ValueError("invalid operations")
    return value

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

class ControlClient:
    def __init__(self, secret, opener=None):
        if not isinstance(secret, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", secret):
            raise ValueError("invalid control secret")
        self._secret = secret
        self._opener = opener or build_opener(NoRedirect())

    def _post(self, path, payload, *, timeout_seconds=TIMEOUT_SECONDS):
        if path not in {"/api/codex/context", "/api/codex/control"}:
            raise ValueError("invalid control path")
        body = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode()
        if len(body) > LIMIT:
            raise ValueError("request too large")
        nonce, timestamp = secrets.token_hex(16), str(int(time.time()))
        request = Request(ORIGIN + path, body, method="POST", headers={
            "Content-Type": "application/json", "X-Control-Timestamp": timestamp,
            "X-Control-Nonce": nonce, "X-Control-Signature": mac(self._secret, "POST", path, timestamp, nonce, hashlib.sha256(body).hexdigest())})
        try:
            response = self._opener.open(request, timeout=timeout_seconds)
        except HTTPError as exc:
            response = exc
        with response:
            raw = response.read(LIMIT + 1)
            headers = response.headers
            if (len(raw) > LIMIT or response.geturl() != ORIGIN + path
                    or headers.get("Content-Type", "").split(";", 1)[0] != "application/json"
                    or headers.get("Content-Encoding") or headers.get("Transfer-Encoding")
                    or (hasattr(headers, "get_all") and any(len(headers.get_all(key, [])) != 1 for key in ("Content-Type", "X-Control-Signature")))):
                raise ValueError("invalid response")
            expected = mac(self._secret, "RESPONSE", path, nonce, str(response.status), hashlib.sha256(raw).hexdigest())
            if not hmac.compare_digest(expected, response.headers.get("X-Control-Signature", "")):
                raise ValueError("unauthenticated response")
            value = decode(raw)
            if response.status != 200 or not isinstance(value, dict):
                raise ValueError("control failed")
            return value

    def context(self):
        return validate_context(self._post("/api/codex/context", {"request_id": secrets.token_hex(16)}))

    def execute(self, action):
        if action.get("operation") == "clarify":
            raise ValueError("clarification is local only")
        result = self._post("/api/codex/control", {"request_id": secrets.token_hex(16), "action": action},
                            timeout_seconds=60 if action.get("operation") in {"music_play", "music_control"} else TIMEOUT_SECONDS)
        if set(result) != {"receipt"}:
            raise ValueError("invalid receipt")
        receipt = result.get("receipt")
        if not isinstance(receipt, str) or not 1 <= len(receipt) <= 2000:
            raise ValueError("invalid receipt")
        return receipt
