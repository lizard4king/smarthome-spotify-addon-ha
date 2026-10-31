"""Optional same-origin browser sessions for the private Lumi job protocol.

The existing authenticated Cloudflared entry remains the login boundary. This
module never authenticates a browser as an Alexa person or exports bridge keys.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
import hashlib
import hmac
from http.cookies import SimpleCookie, CookieError
import json
import os
import re
import secrets
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

BRIDGE = "http://local-codex-voice:8098"
COOKIE = "lumi_session"
HEX32 = re.compile(r"[0-9a-f]{32}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
KEY = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")
MAX_BODY = 4096
MAX_REPLY = 32 * 1024
MAX_SESSIONS = 32
MAX_TURNS = 64
IDLE_SECONDS = 30 * 60
MAX_AGE_SECONDS = 8 * 60 * 60
PATHS = {"/api/lumi/status", "/api/lumi/session", "/api/lumi/turn",
         "/api/lumi/result", "/api/lumi/end"}


def decode(raw):
    def unique(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("duplicate field")
            value[key] = item
        return value
    return json.loads(raw, object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))


class ChatError(Exception):
    def __init__(self, code, status=503, outcome="not_sent"):
        self.code, self.status, self.outcome = code, status, outcome


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class BridgeClient:
    """Fixed private destination; every response is independently authenticated."""
    def __init__(self, key, opener=None, clock=time.time):
        if not isinstance(key, str) or not KEY.fullmatch(key):
            raise ValueError("invalid bridge configuration")
        self.key = key
        self.opener = opener or build_opener(ProxyHandler({}), NoRedirect())
        self.clock = clock

    def _post(self, path, payload, expected_status):
        if path not in {"/v1/submit", "/v1/result"}:
            raise ValueError("invalid path")
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(body) > MAX_BODY:
            raise ValueError("request too large")
        nonce, stamp = secrets.token_hex(16), str(int(self.clock()))
        canonical = "\n".join(("POST", path, stamp, nonce, hashlib.sha256(body).hexdigest()))
        signature = hmac.new(self.key.encode(), canonical.encode(), hashlib.sha256).hexdigest()
        request = Request(BRIDGE + path, body, headers={
            "Content-Type": "application/json", "X-Voice-Timestamp": stamp,
            "X-Voice-Nonce": nonce, "X-Voice-Signature": signature,
        })
        try:
            try:
                response = self.opener.open(request, timeout=5)
            except HTTPError as error:
                response = error
            with response:
                raw = response.read(MAX_REPLY + 1)
                headers = response.headers
                if (len(raw) > MAX_REPLY or response.geturl() != request.full_url
                        or headers.get("Content-Type", "").split(";", 1)[0] != "application/json"
                        or headers.get("Content-Encoding") or headers.get("Transfer-Encoding")
                        or any(len(headers.get_all(name, [])) != 1 for name in
                               ("Content-Type", "X-Voice-Response-Signature"))):
                    raise ValueError("invalid response")
                canonical = "\n".join(("RESPONSE", path, nonce, str(response.status),
                                        hashlib.sha256(raw).hexdigest()))
                expected = hmac.new(self.key.encode(), canonical.encode(), hashlib.sha256).hexdigest()
                if not hmac.compare_digest(expected, headers.get("X-Voice-Response-Signature", "")):
                    raise ValueError("invalid signature")
                if response.status == 429:
                    raise ChatError("busy", 429)
                if response.status != expected_status:
                    raise ValueError("bridge rejected")
                value = decode(raw)
                if not isinstance(value, dict):
                    raise ValueError("invalid object")
                return value
        except ChatError:
            raise
        except (OSError, ValueError, TypeError, RecursionError):
            raise ChatError("unconfirmed", outcome="unknown") from None

    def submit(self, principal, session_key, request_id, text):
        value = self._post("/v1/submit", {
            "principal": principal, "request_id": request_id, "kind": "browser",
            "question": text, "memory": {"person_key": "", "session_key": session_key},
        }, 202)
        if (set(value) != {"job_id", "state"} or not isinstance(value["job_id"], str)
                or not HEX32.fullmatch(value["job_id"]) or not isinstance(value["state"], str)
                or value["state"] not in {"pending", "done", "failed"}):
            raise ChatError("unconfirmed", outcome="unknown")
        return value["job_id"]

    def result(self, principal, job_id):
        value = self._post("/v1/result", {"principal": principal, "job_id": job_id}, 200)
        if value == {"state": "pending"}:
            return value
        if (set(value) == {"state", "answer"} and value["state"] == "done"
                and isinstance(value["answer"], str) and 0 < len(value["answer"]) <= 6000):
            return value
        if (set(value) == {"state", "error"} and value["state"] == "failed"
                and isinstance(value["error"], str)
                and value["error"] in {"timeout", "unauthenticated", "busy", "unavailable"}):
            return value
        raise ChatError("unavailable")


@dataclass
class Turn:
    token: str
    digest: str
    job_id: str = ""
    state: str = "reserved"
    result: dict | None = None


@dataclass
class Session:
    sid: str
    csrf: str
    principal: str
    memory_key: str
    created: float
    touched: float
    turns: OrderedDict = field(default_factory=OrderedDict)
    active: str = ""
    closed: bool = False
    lock: object = field(default_factory=threading.RLock)


class ChatService:
    def __init__(self, *, enabled=False, origin="", key="", client=None,
                 clock=time.monotonic, allow_http_loopback=False):
        self.clock, self.lock, self.sessions = clock, threading.RLock(), {}
        self.enabled, self.origin, self.secure, self.client = False, "", True, None
        if enabled is not True:
            return
        try:
            parsed = urlsplit(origin)
            local = (allow_http_loopback and parsed.scheme == "http"
                     and parsed.hostname in {"127.0.0.1", "localhost", "::1"})
            if (not isinstance(key, str) or not KEY.fullmatch(key) or not parsed.netloc
                    or (parsed.scheme != "https" and not local) or parsed.username or parsed.password
                    or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                    or any(ord(c) < 32 for c in origin)):
                return
            self.origin = parsed.scheme + "://" + parsed.netloc
            self.secure = parsed.scheme == "https"
            self.key = key
            self.client = client or BridgeClient(key)
            self.enabled = True
        except (ValueError, TypeError):
            return

    @classmethod
    def from_environment(cls):
        return cls(enabled=os.getenv("LUMI_CHAT_ENABLED", "").lower() == "true",
                   origin=os.getenv("LUMI_CHAT_ORIGIN", ""), key=os.getenv("LUMI_VOICE_BRIDGE_SECRET", ""))

    def _expire(self):
        now = self.clock()
        self.sessions = {key: value for key, value in self.sessions.items()
                         if not value.closed and now - value.touched < IDLE_SECONDS
                         and now - value.created < MAX_AGE_SECONDS}

    def _sid(self, headers):
        raw = headers.get("Cookie", "")
        if len(raw) > 8192 or sum(item.strip().startswith(COOKIE + "=") for item in raw.split(";")) > 1:
            return ""
        try:
            cookie = SimpleCookie()
            cookie.load(raw)
            sid = cookie[COOKIE].value if COOKIE in cookie else ""
        except CookieError:
            return ""
        return sid if HEX64.fullmatch(sid) else ""

    def session(self, headers, *, create=False):
        with self.lock:
            self._expire()
            current = self.sessions.get(self._sid(headers))
            if current and not create:
                current.touched = self.clock()
                return current
            if current:
                # A deliberate new start must not inherit an old conversation
                # when the previous end response was lost.
                self.end(current)
            if not create:
                raise ChatError("session_expired", 401)
            if len(self.sessions) >= MAX_SESSIONS:
                raise ChatError("capacity", 429)
            sid, now = secrets.token_hex(32), self.clock()
            derive = lambda tag: hmac.new(self.key.encode(), (tag + "\n" + sid).encode(), hashlib.sha256).hexdigest()
            current = Session(sid, secrets.token_hex(32), derive("browser-principal"),
                              derive("browser-session"), now, now)
            self.sessions[sid] = current
            return current

    def authorize(self, headers, session):
        csrf = headers.get("X-Lumi-CSRF", "")
        if not isinstance(csrf, str) or not HEX64.fullmatch(csrf) or not hmac.compare_digest(csrf, session.csrf):
            raise ChatError("forbidden", 403)

    def turn(self, session, payload):
        if (set(payload) != {"message_id", "text"} or not isinstance(payload["message_id"], str)
                or not HEX32.fullmatch(payload["message_id"]) or not isinstance(payload["text"], str)
                or not 1 <= len(payload["text"].strip()) <= 600
                or any(ord(c) < 32 and c not in "\n\t" for c in payload["text"])):
            raise ChatError("invalid_request", 400)
        message, text = payload["message_id"], payload["text"].strip()
        digest = hashlib.sha256(text.encode()).hexdigest()
        with session.lock:
            if session.closed:
                raise ChatError("session_expired", 401)
            previous = session.turns.get(message)
            if previous:
                if previous.digest != digest:
                    raise ChatError("conflict", 409)
                if previous.state == "unconfirmed":
                    raise ChatError("unconfirmed", outcome="unknown")
                if previous.state == "rejected":
                    raise ChatError("busy", 429)
                return {"turn_id": previous.token, "state": "pending"}
            if session.active:
                raise ChatError("busy", 409)
            if len(session.turns) >= MAX_TURNS:
                raise ChatError("capacity", 429)
            turn = Turn(secrets.token_hex(16), digest)
            session.turns[message], session.active = turn, turn.token
        try:
            job = self.client.submit(session.principal, session.memory_key, message, text)
        except ChatError as exc:
            with session.lock:
                turn.state = "unconfirmed" if exc.outcome == "unknown" else "rejected"
                if exc.outcome != "unknown":
                    session.active = ""
            raise
        with session.lock:
            turn.job_id, turn.state = job, "pending"
            if session.closed:
                raise ChatError("session_expired", 401, "unknown")
        return {"turn_id": turn.token, "state": "pending"}

    def result(self, session, payload):
        if set(payload) != {"turn_id"} or not isinstance(payload["turn_id"], str) or not HEX32.fullmatch(payload["turn_id"]):
            raise ChatError("invalid_request", 400)
        with session.lock:
            turn = next((t for t in session.turns.values() if t.token == payload["turn_id"]), None)
            if not turn or session.closed:
                raise ChatError("not_found", 404)
            if turn.result is not None:
                return turn.result
            if turn.state == "unconfirmed":
                raise ChatError("unconfirmed", outcome="unknown")
            if not turn.job_id:
                return {"state": "pending"}
        result = self.client.result(session.principal, turn.job_id)
        with session.lock:
            if session.closed:
                raise ChatError("session_expired", 401)
            if result["state"] != "pending":
                turn.result, turn.state = result, result["state"]
                if session.active == turn.token:
                    session.active = ""
        return result

    def end(self, session):
        with session.lock:
            pending = bool(session.active)
            session.closed = True
            session.turns.clear()
        with self.lock:
            self.sessions.pop(session.sid, None)
        return {"ended": True, "pending": pending}


SERVICE = ChatService.from_environment()


def _single(headers, name):
    values = headers.get_all(name, [])
    if len(values) != 1:
        raise ChatError("invalid_request", 400)
    return values[0]


def handle(handler, service=None):
    service = service or SERVICE
    try:
        if getattr(handler, "connection", None) is not None:
            handler.connection.settimeout(5)
        path = handler.path
        if path not in PATHS:
            raise ChatError("not_found", 404)
        if handler.command == "GET" and path == "/api/lumi/status":
            if handler.headers.get("Sec-Fetch-Site") == "cross-site":
                raise ChatError("forbidden", 403)
            handler._send_json(200, {"enabled": service.enabled})
            return
        if handler.command != "POST" or path == "/api/lumi/status":
            raise ChatError("method_not_allowed", 405)
        if not service.enabled:
            raise ChatError("not_configured")
        if (_single(handler.headers, "Origin") != service.origin
                or _single(handler.headers, "Host") != urlsplit(service.origin).netloc
                or handler.headers.get("Sec-Fetch-Site") not in {None, "same-origin", "none"}
                or _single(handler.headers, "X-Lumi-Client") != "1"):
            raise ChatError("forbidden", 403)
        if (handler.headers.get("Transfer-Encoding") or handler.headers.get("Content-Encoding")
                or _single(handler.headers, "Content-Type") != "application/json"):
            raise ChatError("invalid_request", 400)
        length = _single(handler.headers, "Content-Length")
        if not length.isascii() or not length.isdecimal() or not 1 <= int(length) <= MAX_BODY:
            raise ChatError("invalid_request", 400)
        raw = handler.rfile.read(int(length))
        if len(raw) != int(length):
            raise ChatError("invalid_request", 400)
        payload = decode(raw)
        if not isinstance(payload, dict):
            raise ChatError("invalid_request", 400)
        if path == "/api/lumi/session":
            if payload:
                raise ChatError("invalid_request", 400)
            session = service.session(handler.headers, create=True)
            cookie = f"{COOKIE}={session.sid}; Path=/api/lumi; HttpOnly; SameSite=Strict"
            if service.secure:
                cookie += "; Secure"
            handler._lumi_cookie = cookie
            handler._send_json(200, {"state": "ready", "csrf": session.csrf,
                                     "controls": False, "memory": "session"})
            return
        session = service.session(handler.headers)
        _single(handler.headers, "X-Lumi-CSRF")
        service.authorize(handler.headers, session)
        if path == "/api/lumi/turn":
            result, status = service.turn(session, payload), 202
        elif path == "/api/lumi/result":
            result, status = service.result(session, payload), 200
        else:
            if payload:
                raise ChatError("invalid_request", 400)
            result, status = service.end(session), 200
            # The server revokes the old session. Do not send a late cookie deletion
            # that could erase a newer session created in another browser tab.
        handler._send_json(status, result)
    except ChatError as exc:
        handler._send_json(exc.status, {"error": exc.code, "outcome": exc.outcome})
    except (OSError, ValueError, TypeError, RecursionError):
        handler._send_json(400, {"error": "invalid_request", "outcome": "not_sent"})
