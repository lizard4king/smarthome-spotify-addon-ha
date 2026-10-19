"""Optional Lambda HMAC relay, isolated from music and direct Alexa state."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import time
from types import SimpleNamespace

from aiohttp import web

try:
    from .answer_pages import (AnswerPages, ERROR_MESSAGES, PENDING_TEXT,
                               RETRIEVAL_FAILURE_TEXT, SUBMISSION_TIMEOUT_TEXT)
    from .lumi_greeting import LocalGreetings
    from .answer_delivery import AnswerDelivery, SPEECH_PORT
    from .security import Rejected
    from .voice_gateway import (OPAQUE_ID_RE, BridgeFailure, BridgeBusy, PendingJob, PENDING_TTL_SECONDS, MAX_QUESTION, MAX_REQUEST_CACHE, REQUEST_CACHE_TTL_SECONDS, BridgeJobMissing, VoiceGateway, BRIDGE_SECRET_RE, DEVICE_ID_RE,
                                USER_ID_RE, SKILL_ID_RE, PERSON_ID_RE)
except ImportError:
    from answer_pages import (AnswerPages, ERROR_MESSAGES, PENDING_TEXT,
                              RETRIEVAL_FAILURE_TEXT, SUBMISSION_TIMEOUT_TEXT)
    from lumi_greeting import LocalGreetings
    from answer_delivery import AnswerDelivery, SPEECH_PORT
    from security import Rejected
    from voice_gateway import (OPAQUE_ID_RE, BridgeFailure, BridgeBusy, PendingJob, PENDING_TTL_SECONDS, MAX_QUESTION, MAX_REQUEST_CACHE, REQUEST_CACHE_TTL_SECONDS, BridgeJobMissing, VoiceGateway, BRIDGE_SECRET_RE, DEVICE_ID_RE,
                               USER_ID_RE, SKILL_ID_RE, PERSON_ID_RE)

PATH = "/lumi/voice"
DEADLINE_SECONDS = 6.3
MAX_NONCES = 256
NONCE_TTL = 61
AUTHENTICATED_NONCE = web.RequestKey("lumi_authenticated_nonce", str)
GREETING_FALLBACK = "Da bist du."


class LumiBridgeFailure(BridgeFailure):
    def __init__(self, code):
        super().__init__("Bridge unavailable")
        self.code = code if code in ERROR_MESSAGES else "unavailable"


def lumi_mappings(value):
    if not isinstance(value, list) or len(value) > 256:
        raise Rejected("Invalid Lumi mappings")
    result = {}
    for row in value:
        if (not isinstance(row, dict) or set(row) != {"device_id", "user_id"}
                or not isinstance(row["device_id"], str)
                or not DEVICE_ID_RE.fullmatch(row["device_id"])
                or not isinstance(row["user_id"], str)
                or not USER_ID_RE.fullmatch(row["user_id"])
                or row["device_id"] in result):
            raise Rejected("Invalid Lumi mappings")
        result[row["device_id"]] = dict(row)
    return result


def lumi_relay_enabled(settings):
    skill = getattr(settings, "lumi_skill_id", "")
    secret = getattr(settings, "lumi_relay_secret", "")
    mappings = getattr(settings, "lumi_device_mappings", [])
    lumi_mappings(mappings)
    if skill == "" and secret == "" and not mappings:
        return False
    internal = getattr(settings, "voice_bridge_secret", "")
    if (not isinstance(skill, str) or not SKILL_ID_RE.fullmatch(skill)
            or not isinstance(secret, str) or not BRIDGE_SECRET_RE.fullmatch(secret)
            or not isinstance(internal, str) or not BRIDGE_SECRET_RE.fullmatch(internal)):
        raise Rejected("Invalid Lumi relay configuration")
    return True


def mac(secret, prefix, nonce, status_or_timestamp, body):
    if prefix == "POST":
        parts = [prefix, PATH, status_or_timestamp, nonce]
    else:
        parts = [prefix, PATH, nonce, str(status_or_timestamp)]
    parts.append(hashlib.sha256(body).hexdigest())
    return hmac.new(secret.encode(), "\n".join(parts).encode(), hashlib.sha256).hexdigest()


def _keys(value, allowed, required=()):
    if not isinstance(value, dict) or not set(value) <= set(allowed) or not set(required) <= set(value):
        raise Rejected("Invalid relay event")


def validate_event(payload, skill):
    # Exact allowlists ensure no Amazon API/user access token reaches the bridge.
    _keys(payload, {"version", "context", "session", "request"}, {"context", "session", "request"})
    _keys(payload["context"], {"System"}, {"System"})
    system = payload["context"]["System"]
    _keys(system, {"application", "user", "device", "person"}, {"application", "user", "device"})
    session = payload["session"]
    _keys(session, {"application", "user", "person", "sessionId", "new"}, {"application"})
    if "sessionId" in session and (
        not isinstance(session["sessionId"], str)
        or not 1 <= len(session["sessionId"]) <= 1024
    ):
        raise Rejected("Invalid relay session")
    for container in (system, session):
        _keys(container["application"], {"applicationId"}, {"applicationId"})
        if container["application"]["applicationId"] != skill:
            raise Rejected("Invalid relay skill")
        if "user" in container:
            _keys(container["user"], {"userId"}, {"userId"})
        if "person" in container:
            _keys(container["person"], {"personId"}, {"personId"})
    if "user" in session and session["user"] != system["user"]:
        raise Rejected("Invalid relay user")
    if "person" in session and "person" in system and session["person"] != system["person"]:
        raise Rejected("Invalid relay person")
    _keys(system["device"], {"deviceId"}, {"deviceId"})
    req = payload["request"]
    _keys(req, {"type", "requestId", "timestamp", "locale", "intent"}, {"type", "requestId"})
    if (req["type"] not in {"IntentRequest", "LaunchRequest"} or not isinstance(req["requestId"], str)
            or not 1 <= len(req["requestId"]) <= 1024):
        raise Rejected("Invalid relay request")
    if req["type"] == "LaunchRequest":
        if "intent" in req:
            raise Rejected("Invalid relay launch")
        return
    if "intent" not in req:
        raise Rejected("Invalid relay intent")
    intent = req["intent"]
    _keys(intent, {"name", "slots"}, {"name"})
    if intent["name"] not in {"AskChatGPTIntent", "GetCodexAnswerIntent", "AMAZON.RepeatIntent",
                              "AMAZON.StopIntent", "AMAZON.CancelIntent"}:
        raise Rejected("Invalid relay intent")
    if "slots" in intent:
        _keys(intent["slots"], {"Question"})
        if "Question" in intent["slots"]:
            _keys(intent["slots"]["Question"], {"name", "value"})


class LumiRelay(VoiceGateway):
    def __init__(self, session, settings, *, pairing_observer=None, answer_port=SPEECH_PORT):
        enabled = lumi_relay_enabled(settings)
        proxy = SimpleNamespace(voice_skill_id=settings.lumi_skill_id if enabled else "",
                                voice_bridge_secret=settings.voice_bridge_secret if enabled else "")
        # Submit + polling window + a last in-flight poll stay below 6.3s:
        # 1.0 + 4.0 + 1.0 leaves 0.3s for request/response processing.
        # Lambda must allow 7.0s for this relay (Alexa's overall limit is 8s).
        super().__init__(session, proxy, lambda: lumi_mappings(settings.lumi_device_mappings),
                         bridge_timeout_seconds=1.0, submit_poll_seconds=4.0)
        self.lumi_skill_id = settings.lumi_skill_id if enabled else ""
        self.relay_secret = settings.lumi_relay_secret if enabled else ""
        self.nonces = {}
        self.pairing_observer = pairing_observer
        self.pages = AnswerPages()
        self._retrieval_ids = {}
        self.greetings = LocalGreetings()
        self.answer_delivery = AnswerDelivery(session, settings, self.devices_callable,
                                              self._poll_exact, port=answer_port,
                                              finish=self._delivery_finished)

    def _authenticate(self, request, body):
        if (request.method != "POST" or request.raw_path != PATH
                or request.content_type != "application/json"
                or request.headers.get("Content-Encoding", "identity") != "identity"):
            raise Rejected("Invalid relay transport")
        headers = request.headers
        names = ("X-Lumi-Timestamp", "X-Lumi-Nonce", "X-Lumi-Signature")
        if any(len(headers.getall(name, [])) != 1 for name in names):
            raise Rejected("Invalid relay headers")
        stamp, nonce, signature = (headers[name] for name in names)
        if (not re.fullmatch(r"[0-9]{1,12}", stamp)
                or abs(time.time() - int(stamp)) > 30
                or not re.fullmatch(r"[0-9a-f]{32}", nonce)
                or not re.fullmatch(r"[0-9a-f]{64}", signature)
                or not hmac.compare_digest(signature, mac(self.relay_secret, "POST", nonce, stamp, body))):
            raise Rejected("Invalid relay authentication")
        now = time.monotonic()
        self.nonces = {key: expires for key, expires in self.nonces.items() if expires > now}
        # No await between replay/capacity check and admission.
        if nonce in self.nonces or len(self.nonces) >= MAX_NONCES:
            raise Rejected("Invalid relay nonce")
        self.nonces[nonce] = now + NONCE_TTL
        request[AUTHENTICATED_NONCE] = nonce

    def _principal_inputs(self, payload):
        principal, device, user = super()._principal_inputs(payload)
        namespace = json.dumps([PATH, self.lumi_skill_id, principal], separators=(",", ":")).encode()
        principal = hmac.new(self.secret.encode(), namespace, hashlib.sha256).hexdigest()
        context = payload.get("context")
        system = context.get("System") if isinstance(context, dict) else None
        session = payload.get("session")
        if not isinstance(system, dict) or not isinstance(session, dict):
            raise Rejected("Invalid relay identity")
        person = system.get("person")
        person_id = person.get("personId") if isinstance(person, dict) else None
        session_id = session.get("sessionId")
        if session_id is not None and (
            not isinstance(session_id, str) or not 1 <= len(session_id) <= 1024
        ):
            raise Rejected("Invalid relay session")
        if person_id is None:
            namespace = json.dumps(
                ["lumi-session-principal-v1", PATH, self.lumi_skill_id, principal, session_id if session_id is not None else ["stateless", payload["request"]["requestId"]]],
                separators=(",", ":"),
            ).encode()
            principal = hmac.new(self.secret.encode(), namespace, hashlib.sha256).hexdigest()
        return principal, device, user


    async def _handle(self, request):
        if not self._allow_peer(request.remote or "unknown", time.monotonic()):
            return self._json({"error": "Request limit"}, status=429)
        try:
            body = await self._read_body(request)
            self._authenticate(request, body)
            payload = json.loads(body)
            validate_event(payload, self.settings.voice_skill_id)
            principal, device, user = self._principal_inputs(payload)
        except Exception:
            return self._json({"error": "Invalid relay request"}, status=400)
        if self.active_verified >= 4:
            return self._json({"error": "Relay busy"}, status=503)
        request_data = payload.get("request")
        if (self.pairing_observer is not None
                and isinstance(request_data, dict)
                and request_data.get("type") == "LaunchRequest"):
            system = payload["context"]["System"]
            application = system["application"]["applicationId"]
            mapping = self.devices_callable().get(device)
            person_key = self._memory_value(payload)["person_key"]
            if (not mapping
                    or not hmac.compare_digest(mapping.get("user_id", ""), user)
                    or person_key):
                self.pairing_observer.observe_verified_request(
                    application_id=application,
                    expected_skill_id=self.lumi_skill_id,
                    device_id=device,
                    user_id=user,
                    person_key=person_key,
                )
        self.active_verified += 1
        try:
            return await self._process_verified(payload, body, principal, device, user)
        finally:
            self.active_verified -= 1


    async def handle(self, request):
        try:
            async with asyncio.timeout(DEADLINE_SECONDS):
                response = await self._handle(request) if self.enabled else self._json({"error": "Not found"}, status=404)
        except asyncio.CancelledError:
            raise
        except Exception:
            response = self._error("Der Gesprächsdienst ist gerade nicht erreichbar.")
        nonce = request.get(AUTHENTICATED_NONCE)
        if nonce:
            response.headers["X-Lumi-Response-Signature"] = mac(
                self.relay_secret, "RESPONSE", nonce, response.status, response.body)
        response.headers["Cache-Control"] = "no-store"
        return response

    async def _dispatch(self, req, payload, principal):
        if req.get("type") != "LaunchRequest":
            name = req.get("intent", {}).get("name")
            if name in {"AMAZON.StopIntent", "AMAZON.CancelIntent"}:
                self._clear_answer_pages(principal)
                self.pending.pop(principal, None)
                self.last_jobs.pop(principal, None)
                _, device, _ = self._principal_inputs(payload)
                previous = self.answer_delivery.cancel(device)
                if previous:
                    self._clear_answer_pages(previous.principal)
                    self.pending.pop(previous.principal, None)
                    self.last_jobs.pop(previous.principal, None)
            if name == "AskChatGPTIntent":
                return await self._ask(req, payload, principal)
            if name in {"AMAZON.RepeatIntent", "GetCodexAnswerIntent"}:
                now = time.monotonic()
                self._retrieval_ids = {key: expiry for key, expiry in self._retrieval_ids.items() if expiry > now}
                retrieval_key = (principal, req["requestId"])
                if retrieval_key not in self._retrieval_ids and len(self._retrieval_ids) >= MAX_REQUEST_CACHE:
                    return self._speech(ERROR_MESSAGES["busy"], end=False)
                self._retrieval_ids[retrieval_key] = now + REQUEST_CACHE_TTL_SECONDS + DEADLINE_SECONDS
                _, device, _ = self._principal_inputs(payload)
                slot = self.answer_delivery.slots.get(device)
                if slot and slot.principal == principal:
                    return self._speech("Einen Moment.", end=True)
                return await self._get_answer(principal, request_id=req["requestId"],
                                              repeat=name == "AMAZON.RepeatIntent")
            return await super()._dispatch(req, payload, principal)
        return self._speech(self.greetings.select(principal, req["requestId"]), end=False)


    def _clear_answer_pages(self, principal):
        for key in tuple(self._retrieval_ids):
            if key[0] != principal:
                continue
            cached = self.replies.get(key)
            if cached is not None:
                # Preserve request identity, expiry and original Ask cache.
                self.replies[key] = (cached[0], cached[1], self._speech(
                    "Die Anfrage wurde abgebrochen.", end=False))
        self.pages.clear(principal)


    async def _get_answer(self, principal, *, request_id=None, repeat=False):
        self._expire_pending(time.monotonic())
        if self.pages.available(principal):
            return self._speech(self.pages.select(principal, request_id, repeat=repeat,
                                first=not self.pages.answers[principal].presented), end=False)
        if principal not in self.pending and principal not in self.last_jobs:
            return self._speech("Es gibt keine fertige Antwort. Stelle deine Frage bitte noch einmal.", end=False)
        job = self.pending.get(principal) or self.last_jobs[principal]
        try:
            answer = await self._poll_exact(principal, job)
        except BridgeFailure as exc:
            return self._speech(self._transport_failure_text(exc, known_job=True), end=False)
        if answer is None:
            return self._speech(PENDING_TEXT, end=False)
        if self.pages.available(principal) and request_id is not None:
            answer = self.pages.select(principal, request_id, repeat=repeat, first=True)
        return self._speech(answer, end=False)

    @staticmethod
    def _transport_failure_text(exc, *, known_job):
        code = getattr(exc, "code", "unavailable")
        if known_job and code in {"timeout", "unavailable"}:
            return RETRIEVAL_FAILURE_TEXT
        if not known_job and code == "timeout":
            return SUBMISSION_TIMEOUT_TEXT
        return ERROR_MESSAGES.get(code, ERROR_MESSAGES["unavailable"])

    async def _ask(self, req, payload, principal):
        slots = req.get("intent", {}).get("slots", {})
        question = slots.get("Question", {}).get("value")
        if not isinstance(question, str) or not question.strip() or len(question) > MAX_QUESTION:
            return self._speech("Welche kurze Frage soll ich beantworten?", end=False)
        _, device, user = self._principal_inputs(payload)
        self._clear_answer_pages(principal)
        self.pending.pop(principal, None)
        self.last_jobs.pop(principal, None)
        previous = self.answer_delivery.cancel(device)
        if previous:
            self._clear_answer_pages(previous.principal)
            self.pending.pop(previous.principal, None)
            self.last_jobs.pop(previous.principal, None)
        slot = self.answer_delivery.reserve(principal, device, user)
        if slot is None:
            return self._speech(ERROR_MESSAGES["busy"], end=False)
        handed_off = False
        try:
            started = time.monotonic()
            result = await self._bridge_call("/v1/submit", self._submission_value(
                req, payload, principal, " ".join(question.split())))
            if (set(result) != {"job_id", "state"} or result.get("state") not in {"pending", "done", "failed"}
                    or not isinstance(result.get("job_id"), str) or not OPAQUE_ID_RE.fullmatch(result["job_id"])):
                raise BridgeFailure("Invalid bridge response")
            if not self.answer_delivery.current(slot):
                return self._speech("Die Anfrage wurde abgebrochen.")
            job = slot.job = PendingJob(result["job_id"], started + PENDING_TTL_SECONDS)
            slot.request_id = hmac.new(self.secret.encode(),
                ("lumi-answer-delivery-v1\n" + principal + "\n" + job.job_id).encode(), hashlib.sha256).hexdigest()[:32]
            self._expire_pending(time.monotonic())
            active_jobs = {(item.principal, item.job) for item in self.answer_delivery.slots.values() if item.job}
            self.pending = {key: value for key, value in self.pending.items() if (key, value) in active_jobs}
            self.pending[principal] = job
            self._store_last_job(principal, job)
            until = time.monotonic() + self.submit_poll_seconds
            while True:
                answer = await self._poll_exact(principal, job)
                if not self.answer_delivery.current(slot):
                    return self._speech("Die Anfrage wurde abgebrochen.")
                if answer is not None:
                    self.pages.mark_presented(principal, job)
                    return self._speech(answer, end=False)
                if time.monotonic() >= until:
                    break
                await asyncio.sleep(min(0.12, max(0, until - time.monotonic())))
            handed_off = self.answer_delivery.start(slot)
            if handed_off:
                return self._speech("Einen Moment.", end=True)
            self.pending.pop(principal, None)
            return self._speech(PENDING_TEXT, end=False)
        except BridgeFailure as exc:
            return self._speech(self._transport_failure_text(exc, known_job=slot.job is not None), end=False)
        finally:
            if not handed_off:
                self.answer_delivery.release(slot)

    async def _poll_exact(self, principal, job):
        try:
            result = await self._bridge_call("/v1/result", {"principal": principal, "job_id": job.job_id})
        except BridgeJobMissing:
            result = {"state": "failed", "error": "unavailable"}
        if result == {"state": "pending"}:
            return None
        if (set(result) == {"state", "error"} and result["state"] == "failed"
                and isinstance(result["error"], str) and result["error"] in ERROR_MESSAGES):
            text = ERROR_MESSAGES[result["error"]]
        elif set(result) == {"state", "answer"} and result["state"] == "done":
            if self.last_jobs.get(principal) != job:
                return None
            try:
                text = self.pages.first(principal, job, result["answer"])
            except ValueError as exc:
                raise BridgeFailure("Invalid bridge answer") from exc
        else:
            raise BridgeFailure("Invalid bridge response")
        if self.pending.get(principal) == job:
            self.pending.pop(principal, None)
        return text

    async def _bridge_call(self, path, value):
        try:
            return await super()._bridge_call(path, value)
        except BridgeJobMissing:
            raise
        except BridgeFailure as exc:
            code = ("busy" if isinstance(exc, BridgeBusy) else "timeout"
                    if isinstance(exc.__cause__, TimeoutError) else getattr(exc, "code", "unavailable"))
            raise LumiBridgeFailure(code) from None

    def _delivery_finished(self, slot):
        if slot.job is not None and slot.accepted:
            self.pages.mark_presented(slot.principal, slot.job)
        if slot.job is not None and self.pending.get(slot.principal) == slot.job:
            self.pending.pop(slot.principal, None)

    async def close(self):
        await self.answer_delivery.close()

    def _submission_value(self, req, payload, principal, question):
        value = super()._submission_value(req, payload, principal, question)
        value["memory"] = self._memory_value(payload)
        return value


    @staticmethod
    def _speech(text, *, end=True, reprompt=None):
        # Lumi does not repeat routine questions when the user remains silent.
        # Keep clarification in the actual spoken text when it is necessary.
        return VoiceGateway._speech(text, end=end)


    def _memory_value(self, payload):
        context = payload.get("context")
        system = context.get("System") if isinstance(context, dict) else None
        session = payload.get("session")
        if not isinstance(system, dict) or not isinstance(session, dict):
            raise Rejected("Invalid relay identity")
        user = system.get("user")
        device = system.get("device")
        user_id = user.get("userId") if isinstance(user, dict) else None
        device_id = device.get("deviceId") if isinstance(device, dict) else None
        if not isinstance(user_id, str) or not isinstance(device_id, str):
            raise Rejected("Invalid relay identity")

        person = system.get("person")
        person_id = person.get("personId") if isinstance(person, dict) else None
        if person_id is not None and (
            not isinstance(person_id, str) or not PERSON_ID_RE.fullmatch(person_id)
        ):
            raise Rejected("Invalid relay person")

        session_id = session.get("sessionId")
        if session_id is not None and (
            not isinstance(session_id, str) or not 1 <= len(session_id) <= 1024
        ):
            raise Rejected("Invalid relay session")

        skill_id = self.lumi_skill_id
        person_key = self._memory_key(
            "lumi-memory-person-v1", skill_id, user_id, person_id
        ) if person_id is not None else ""
        session_key = self._memory_key(
            "lumi-memory-session-v1", skill_id, user_id, device_id, session_id
        ) if session_id is not None else ""
        if person_id is not None and session_id is not None:
            # Unapproved voice profiles must not share an anonymous session's context.
            session_key = self._memory_key(
                "lumi-memory-person-session-v1", skill_id, user_id, device_id, session_id, person_id
            )
        return {"person_key": person_key, "session_key": session_key}


    async def _greeting(self, req, payload, principal):
        # Greeting jobs never replace an outstanding question or its answer.
        greeting_principal = hmac.new(self.secret.encode(),
            b"lumi-greeting-job-v1\0" + principal.encode(), hashlib.sha256).hexdigest()
        result = await self._bridge_call("/v1/submit", {
            "principal": greeting_principal, "request_id": req["requestId"],
            "kind": "greeting", "memory": self._memory_value(payload),
        })
        if (set(result) != {"job_id", "state"}
                or result.get("state") not in {"pending", "done", "failed"}
                or not isinstance(result.get("job_id"), str)
                or not OPAQUE_ID_RE.fullmatch(result["job_id"])):
            raise BridgeFailure("Invalid greeting job")
        until = time.monotonic() + self.submit_poll_seconds
        while True:
            answer = await self._bridge_call("/v1/result", {
                "principal": greeting_principal, "job_id": result["job_id"],
            })
            if answer == {"state": "pending"}:
                if time.monotonic() >= until:
                    return GREETING_FALLBACK
                await asyncio.sleep(min(0.12, max(0, until - time.monotonic())))
                continue
            if set(answer) != {"state", "answer"} or answer["state"] != "done":
                return GREETING_FALLBACK
            text = answer["answer"]
            if (not isinstance(text, str) or not text.strip() or len(text) > 180
                    or any(char in text for char in "?<>")
                    or "was möchtest" in text.casefold()):
                return GREETING_FALLBACK
            return " ".join(text.split())


    def _memory_key(self, domain, *parts):
        payload = domain.encode("ascii") + b"\0" + json.dumps(
            parts, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        return hmac.new(self.secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
