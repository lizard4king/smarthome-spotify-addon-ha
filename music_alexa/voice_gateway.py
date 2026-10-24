"""Optional, signed Alexa-to-local-Codex voice gateway."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass

import aiohttp
from aiohttp import web

try:
    from .security import AlexaVerifier, Rejected
except ImportError:
    from security import AlexaVerifier, Rejected


LOG = logging.getLogger("music_alexa.voice_gateway")
INTERNAL_ORIGIN = "http://local-codex-voice:8098"
MAX_BODY = 32768
MAX_BRIDGE_BODY = 4096
MAX_BRIDGE_RESPONSE = 32768
MAX_QUESTION = 600
# Match the authenticated bridge answer limit and the Lambda relay validator.
MAX_SPEECH = 6000
DEADLINE_SECONDS = 6.5
SUBMIT_POLL_SECONDS = 2.0
BRIDGE_TIMEOUT_SECONDS = 1.5
PENDING_TTL_SECONDS = 600.0
REQUEST_CACHE_TTL_SECONDS = 150.0
MAX_PENDING = 32
MAX_REQUEST_CACHE = 256
MAX_PRINCIPALS_PER_PEER = 256
SKILL_ID_RE = re.compile(r"amzn1\.ask\.skill\.[0-9a-fA-F-]{36}\Z")
USER_ID_RE = re.compile(r"amzn1\.ask\.account\.[A-Za-z0-9._-]{1,1000}\Z")
DEVICE_ID_RE = re.compile(r"amzn1\.ask\.device\.[A-Za-z0-9._-]{1,1000}\Z")
OPAQUE_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
BRIDGE_SECRET_RE = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")
PERSON_ID_RE = re.compile(r"[^\x00-\x1f\x7f]{1,1024}\Z")


def voice_gateway_enabled(settings) -> bool:
    """Return disabled only when both settings are empty; reject partial config."""
    skill_id = getattr(settings, "voice_skill_id", "")
    bridge_secret = getattr(settings, "voice_bridge_secret", "")
    # A relay-only deployment shares the internal bridge secret, but does not
    # register or enable the Amazon-certificate-authenticated /voice route.
    if skill_id in (None, "") and getattr(settings, "lumi_skill_id", ""):
        if not isinstance(bridge_secret, str) or not BRIDGE_SECRET_RE.fullmatch(bridge_secret):
            raise Rejected("Invalid voice gateway configuration")
        return False
    if skill_id in (None, "") and bridge_secret in (None, ""):
        return False
    if (not isinstance(skill_id, str) or not SKILL_ID_RE.fullmatch(skill_id)
            or not isinstance(bridge_secret, str) or not BRIDGE_SECRET_RE.fullmatch(bridge_secret)):
        raise Rejected("Invalid voice gateway configuration")
    return True


@dataclass(frozen=True)
class PendingJob:
    job_id: str
    expires: float


@dataclass
class RequestLock:
    lock: asyncio.Lock
    references: int = 0


class BridgeFailure(Rejected):
    """Internal bridge failure translated to generic speech, never request detail."""


class BridgeJobMissing(BridgeFailure):
    """A correctly signed result response confirms that the job no longer exists."""


class VoiceGateway:
    """Verify a Lumi Alexa skill request, then call only the fixed local bridge."""

    def __init__(self, session, settings, devices_callable, *, verifier=None,
                 bridge_timeout_seconds=None, submit_poll_seconds=None):
        self.session = session
        self.settings = settings
        self.devices_callable = devices_callable
        self._bridge_timeout_seconds = bridge_timeout_seconds
        self._submit_poll_seconds = submit_poll_seconds
        self.enabled = voice_gateway_enabled(settings)
        self.secret = getattr(settings, "voice_bridge_secret", "") if self.enabled else ""
        skill_id = getattr(settings, "voice_skill_id", "") if self.enabled else ""
        self.verifier = verifier or (AlexaVerifier(session, skill_id) if self.enabled else None)
        self.pending: dict[str, PendingJob] = {}
        self.last_jobs: dict[str, PendingJob] = {}
        self.replies: dict[tuple[str, str], tuple[float, str, dict]] = {}
        self.request_locks: dict[tuple[str, str], RequestLock] = {}
        self.peer_times: dict[str, list[float]] = {}
        self.verified_times: list[float] = []
        self.verify_slots = asyncio.Semaphore(4)
        self.active_verified = 0

    @property
    def bridge_timeout_seconds(self):
        return BRIDGE_TIMEOUT_SECONDS if self._bridge_timeout_seconds is None else self._bridge_timeout_seconds

    @property
    def submit_poll_seconds(self):
        return SUBMIT_POLL_SECONDS if self._submit_poll_seconds is None else self._submit_poll_seconds

    async def handle(self, request) -> web.Response:
        if not self.enabled:
            return self._json({"error": "Not found"}, status=404)
        try:
            async with asyncio.timeout(DEADLINE_SECONDS):
                return await self._handle(request)
        except TimeoutError:
            return self._error("Der Gesprächsdienst ist gerade nicht erreichbar.")
        except asyncio.CancelledError:
            raise
        except Exception:
            # Do not log request data, credentials, bridge URLs, or exception text.
            LOG.warning("Voice request failed")
            return self._error("Der Gesprächsdienst ist gerade nicht erreichbar.")

    async def _handle(self, request) -> web.Response:
        now = time.monotonic()
        peer = request.remote or "unknown"
        if not self._allow_peer(peer, now):
            return self._json({"error": "Request limit"}, status=429)
        try:
            body = await self._read_body(request)
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise Rejected("Invalid request")
            async with self.verify_slots:
                await self.verifier.verify(request.headers, body, payload)
            principal, device_id, user_id = self._principal_inputs(payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            return self._json({"error": "Invalid Alexa request"}, status=400)

        if self.active_verified >= 4:
            return self._json({"error": "Verifier busy"}, status=503)
        self.active_verified += 1
        try:
            return await self._process_verified(payload, body, principal, device_id, user_id)
        finally:
            self.active_verified -= 1

    async def _process_verified(self, payload, body, principal, device_id, user_id):

        now = time.monotonic()
        self.verified_times = [stamp for stamp in self.verified_times if now - stamp < 60]
        if len(self.verified_times) >= 60:
            return self._json({"error": "Request limit"}, status=429)
        self.verified_times.append(now)

        row = self.devices_callable().get(device_id)
        if not row or not hmac.compare_digest(row.get("user_id", ""), user_id):
            return self._json({"error": "Device not paired"}, status=403)

        req = payload["request"]
        request_id = req.get("requestId")
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 1024:
            return self._json({"error": "Invalid Alexa request"}, status=400)
        digest = hashlib.sha256(body).hexdigest()
        cache_key = (principal, request_id)
        self.replies = {item: value for item, value in self.replies.items()
                        if value[0] > time.monotonic()}
        if cache_key not in self.replies and len(self.replies) >= MAX_REQUEST_CACHE:
            return self._json({"error": "Request limit"}, status=429)
        entry = self.request_locks.get(cache_key)
        if entry is None:
            if len(self.request_locks) >= MAX_REQUEST_CACHE:
                return self._json({"error": "Request limit"}, status=429)
            entry = self.request_locks[cache_key] = RequestLock(asyncio.Lock())
        entry.references += 1
        try:
            async with entry.lock:
                try:
                    cached = self._cached_reply(cache_key, digest, time.monotonic())
                except Rejected:
                    return self._json({"error": "Invalid Alexa request"}, status=400)
                if cached is not None:
                    return self._json(cached)
                try:
                    answer = await self._dispatch(req, payload, principal)
                except BridgeJobMissing:
                    raise
                except BridgeFailure:
                    answer = self._speech("Der Gesprächsdienst ist gerade nicht erreichbar.")
                except Rejected:
                    return self._json({"error": "Invalid Alexa request"}, status=400)
                # Never use a result after Companion device/user mapping has changed.
                current = self.devices_callable().get(device_id)
                if (current != row or not current
                        or not hmac.compare_digest(current.get("user_id", ""), user_id)):
                    return self._json({"error": "Device mapping changed"}, status=403)
                self._store_reply(cache_key, digest, answer, time.monotonic())
                return self._json(answer)
        finally:
            entry.references -= 1
            if entry.references == 0 and self.request_locks.get(cache_key) is entry:
                self.request_locks.pop(cache_key, None)

    @staticmethod
    async def _read_body(request) -> bytes:
        if request.headers.get("Transfer-Encoding"):
            raise Rejected("Invalid body")
        length = request.content_length
        if length is None or length <= 0 or length > MAX_BODY:
            raise Rejected("Invalid body")
        chunks = []
        size = 0
        async for chunk in request.content.iter_chunked(8192):
            size += len(chunk)
            if size > MAX_BODY:
                raise Rejected("Invalid body")
            chunks.append(chunk)
        body = b"".join(chunks)
        if len(body) != length or len(body) > MAX_BODY:
            raise Rejected("Invalid body")
        return body

    def _allow_peer(self, peer: str, now: float) -> bool:
        self.peer_times = {key: rows for key, rows in self.peer_times.items()
                           if rows and now - rows[-1] < 60}
        if peer not in self.peer_times and len(self.peer_times) >= MAX_PRINCIPALS_PER_PEER:
            return False
        rows = [stamp for stamp in self.peer_times.get(peer, []) if now - stamp < 60]
        if len(rows) >= 120:
            return False
        rows.append(now)
        self.peer_times[peer] = rows
        return True

    def _principal_inputs(self, payload: dict) -> tuple[str, str, str]:
        context = payload.get("context")
        system = context.get("System") if isinstance(context, dict) else None
        if not isinstance(system, dict):
            raise Rejected("Invalid request")
        device, user = system.get("device"), system.get("user")
        device_id = device.get("deviceId") if isinstance(device, dict) else None
        user_id = user.get("userId") if isinstance(user, dict) else None
        if (not isinstance(device_id, str) or not DEVICE_ID_RE.fullmatch(device_id)
                or not isinstance(user_id, str) or not USER_ID_RE.fullmatch(user_id)):
            raise Rejected("Invalid request")
        person_id = ""
        person = system.get("person")
        if not isinstance(person, dict):
            session = payload.get("session")
            person = session.get("person") if isinstance(session, dict) else None
        if isinstance(person, dict) and "personId" in person:
            person_id = person["personId"]
            if not isinstance(person_id, str) or not PERSON_ID_RE.fullmatch(person_id):
                raise Rejected("Invalid request")
        canonical = json.dumps([user_id, device_id, person_id], ensure_ascii=False,
                               separators=(",", ":")).encode("utf-8")
        principal = hmac.new(self.secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()
        return principal, device_id, user_id

    def _cached_reply(self, key: tuple[str, str], digest: str, now: float):
        self.replies = {item: value for item, value in self.replies.items() if value[0] > now}
        cached = self.replies.get(key)
        if cached is None:
            return None
        if not hmac.compare_digest(cached[1], digest):
            raise Rejected("Duplicate request mismatch")
        return cached[2]

    def _store_reply(self, key, digest, response, now):
        self.replies = {item: value for item, value in self.replies.items() if value[0] > now}
        if len(self.replies) < MAX_REQUEST_CACHE:
            self.replies[key] = (now + REQUEST_CACHE_TTL_SECONDS, digest, response)

    async def _dispatch(self, req: dict, payload: dict, principal: str) -> dict:
        request_type = req.get("type")
        if request_type == "LaunchRequest":
            return self._speech("Hallo, ich bin Lumi. Was möchtest du wissen?", end=False,
                                reprompt="Was möchtest du wissen?")
        if request_type == "SessionEndedRequest":
            return {"version": "1.0", "response": {}}
        if request_type != "IntentRequest":
            raise Rejected("Invalid request")
        intent = req.get("intent")
        intent_name = intent.get("name") if isinstance(intent, dict) else None
        if intent_name in {"AMAZON.StopIntent", "AMAZON.CancelIntent"}:
            return self._speech("Bis bald.", end=True)
        if intent_name == "AMAZON.HelpIntent":
            return self._speech("Stelle Lumi eine kurze Frage oder sage Stopp.", end=False,
                                reprompt="Was möchtest du wissen?")
        if intent_name in {"AMAZON.RepeatIntent", "GetCodexAnswerIntent"}:
            return await self._get_answer(principal)
        if intent_name != "AskChatGPTIntent":
            return self._speech("Das habe ich nicht verstanden. Was möchtest du wissen?", end=False,
                                reprompt="Was möchtest du wissen?")
        slots = intent.get("slots")
        question_slot = slots.get("Question") if isinstance(slots, dict) else None
        question = question_slot.get("value") if isinstance(question_slot, dict) else None
        if not isinstance(question, str) or not question.strip() or len(question) > MAX_QUESTION:
            return self._speech("Welche kurze Frage soll ich beantworten?", end=False,
                                reprompt="Was möchtest du wissen?")
        question = " ".join(question.split())
        now = time.monotonic()
        self._expire_pending(now)
        if principal in self.pending:
            return await self._get_answer(principal)
        if len(self.pending) >= MAX_PENDING:
            return self._speech("Der Gesprächsdienst ist gerade ausgelastet.")
        result = await self._bridge_call(
            "/v1/submit",
            self._submission_value(req, payload, principal, question),
        )
        state = result.get("state")
        if set(result) != {"job_id", "state"} or state not in {"pending", "done", "failed"}:
            raise BridgeFailure("Invalid bridge response")
        job_id = result.get("job_id")
        if not isinstance(job_id, str) or not OPAQUE_ID_RE.fullmatch(job_id):
            raise BridgeFailure("Invalid bridge response")
        pending = PendingJob(job_id, time.monotonic() + PENDING_TTL_SECONDS)
        self.pending[principal] = pending
        self._store_last_job(principal, pending)
        if state in {"done", "failed"}:
            return await self._get_answer(principal)
        until = time.monotonic() + self.submit_poll_seconds
        while time.monotonic() < until:
            answer = await self._poll(principal)
            if answer is not None:
                return self._speech(answer, end=False, reprompt="Möchtest du noch etwas wissen?")
            await asyncio.sleep(min(0.12, max(0, until - time.monotonic())))
        return self._speech("Die Antwort wird noch erstellt. Sage: Antwort.", end=False,
                            reprompt="Sage Antwort, um die fertige Antwort abzurufen.")

    async def _get_answer(self, principal: str) -> dict:
        self._expire_pending(time.monotonic())
        if principal not in self.pending and principal not in self.last_jobs:
            return self._speech("Es gibt keine fertige Antwort. Stelle deine Frage bitte noch einmal.",
                                end=False, reprompt="Was möchtest du wissen?")
        answer = await self._poll(principal)
        if answer is None:
            return self._speech("Die Antwort wird noch erstellt. Sage: Antwort.", end=False,
                                reprompt="Sage Antwort, um die fertige Antwort abzurufen.")
        return self._speech(answer, end=False, reprompt="Möchtest du noch etwas wissen?")

    def _submission_value(
        self, req: dict, payload: dict, principal: str, question: str
    ) -> dict:
        """Build the legacy bridge request; specialized relays may add metadata."""
        return {
            "principal": principal,
            "request_id": req["requestId"],
            "question": question,
        }

    async def _poll(self, principal: str) -> str | None:
        pending = self.pending.get(principal) or self.last_jobs.get(principal)
        if not pending:
            return None
        try:
            result = await self._bridge_call("/v1/result", {
                "principal": principal,
                "job_id": pending.job_id,
            })
        except BridgeJobMissing:
            self.pending.pop(principal, None)
            self.last_jobs.pop(principal, None)
            return "Die Anfrage ist nicht mehr verfügbar. Stelle deine Frage bitte erneut."
        state = result.get("state")
        if state == "pending":
            if set(result) != {"state"}:
                raise BridgeFailure("Invalid bridge response")
            return None
        if state == "failed":
            if set(result) != {"state", "error"} or result.get("error") != "unavailable":
                raise BridgeFailure("Invalid bridge response")
            self.pending.pop(principal, None)
            self.last_jobs.pop(principal, None)
            return "Der Gesprächsdienst ist gerade nicht erreichbar."
        if state != "done" or set(result) != {"state", "answer"}:
            raise BridgeFailure("Invalid bridge response")
        self.pending.pop(principal, None)
        self._store_last_job(principal, pending)
        try:
            return self._spoken(result["answer"])
        except Rejected as exc:
            raise BridgeFailure("Invalid bridge response") from exc

    async def _bridge_call(self, path: str, value: dict) -> dict:
        try:
            return await self._bridge_call_inner(path, value)
        except BridgeJobMissing:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise BridgeFailure("Bridge unavailable") from exc

    async def _bridge_call_inner(self, path: str, value: dict) -> dict:
        if path not in {"/v1/submit", "/v1/result"}:
            raise Rejected("Invalid bridge path")
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(body) > MAX_BRIDGE_BODY:
            raise Rejected("Bridge request too large")
        timestamp = str(int(time.time()))
        nonce = secrets.token_hex(16)
        signature = self._request_signature(path, timestamp, nonce, body)
        headers = {
            "Content-Type": "application/json",
            "Accept-Encoding": "identity",
            "X-Voice-Timestamp": timestamp,
            "X-Voice-Nonce": nonce,
            "X-Voice-Signature": signature,
        }
        timeout = aiohttp.ClientTimeout(total=self.bridge_timeout_seconds)
        async with self.session.post(INTERNAL_ORIGIN + path, data=body, headers=headers,
                                     allow_redirects=False, timeout=timeout) as response:
            chunks = []
            size = 0
            async for chunk in response.content.iter_chunked(8192):
                size += len(chunk)
                if size > MAX_BRIDGE_RESPONSE:
                    raise Rejected("Bridge response too large")
                chunks.append(chunk)
            raw = b"".join(chunks)
            if (len(raw) > MAX_BRIDGE_RESPONSE
                    or response.headers.get("Content-Encoding", "identity").lower() != "identity"):
                raise Rejected("Bridge unavailable")
            expected = self._response_signature(path, nonce, response.status, raw)
            supplied = response.headers.get("X-Voice-Response-Signature", "")
            if not re.fullmatch(r"[0-9a-f]{64}", supplied) or not hmac.compare_digest(supplied, expected):
                raise Rejected("Invalid bridge response")
            if path == "/v1/result" and response.status == 404:
                try:
                    missing = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    raise Rejected("Invalid bridge response") from None
                if not isinstance(missing, dict) or set(missing) != {"error"} or missing.get("error") != "unavailable":
                    raise Rejected("Invalid bridge response")
                raise BridgeJobMissing("Result job no longer exists")
            expected_status = 202 if path == "/v1/submit" else 200
            if response.status != expected_status:
                raise Rejected("Bridge unavailable")
        try:
            result = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise Rejected("Invalid bridge response") from None
        if not isinstance(result, dict):
            raise Rejected("Invalid bridge response")
        return result

    def _request_signature(self, path, timestamp, nonce, body):
        message = (b"POST\n" + path.encode() + b"\n" + timestamp.encode() + b"\n"
                   + nonce.encode() + b"\n" + hashlib.sha256(body).hexdigest().encode())
        return hmac.new(self.secret.encode(), message, hashlib.sha256).hexdigest()

    def _response_signature(self, path, nonce, status, body):
        message = (b"RESPONSE\n" + path.encode() + b"\n" + nonce.encode() + b"\n" + str(status).encode()
                   + b"\n" + hashlib.sha256(body).hexdigest().encode())
        return hmac.new(self.secret.encode(), message, hashlib.sha256).hexdigest()

    def _expire_pending(self, now):
        self.pending = {key: value for key, value in self.pending.items() if value.expires > now}
        self.last_jobs = {key: value for key, value in self.last_jobs.items() if value.expires > now}

    def _store_last_job(self, principal, job):
        self.last_jobs.pop(principal, None)
        if len(self.last_jobs) >= MAX_PENDING:
            oldest = next(iter(self.last_jobs))
            self.last_jobs.pop(oldest, None)
        self.last_jobs[principal] = job

    @staticmethod
    def _spoken(value):
        if not isinstance(value, str):
            raise Rejected("Invalid answer")
        text = " ".join(value.split())
        if not text:
            raise Rejected("Invalid answer")
        if len(text) > MAX_SPEECH:
            # Never turn an oversized answer into an apparently complete,
            # mid-sentence fragment. Normal bridge replies fit in full.
            notice = "Die Antwort ist zu lang zum Vorlesen. Bitte stelle eine gezieltere Frage."
            budget = MAX_SPEECH - len(notice) - 1
            ends = [match.end() for match in re.finditer(
                r'[.!?](?:[\"»”’“\')\]]*)(?=\s|$)', text
            ) if match.end() <= budget]
            text = (text[:ends[-1]] + " " + notice) if ends else notice
        return text

    @staticmethod
    def _speech(text, *, end=True, reprompt=None):
        output = {"type": "PlainText", "text": text}
        response = {"outputSpeech": output, "shouldEndSession": end}
        if reprompt:
            response["reprompt"] = {"outputSpeech": {"type": "PlainText", "text": reprompt}}
        return {"version": "1.0", "response": response}

    @classmethod
    def _error(cls, text):
        return cls._json(cls._speech(text), status=200)

    @staticmethod
    def _json(data, *, status=200):
        return web.json_response(data, status=status, headers={"Cache-Control": "no-store"},
                                 dumps=lambda value: json.dumps(value, ensure_ascii=False))
