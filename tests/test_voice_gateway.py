"""Offline tests for the optional, signed Lumi-to-local-Codex gateway."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from types import SimpleNamespace

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from music_alexa import voice_gateway as module
from music_alexa.security import Rejected
from music_alexa.voice_gateway import VoiceGateway, voice_gateway_enabled


SKILL = "amzn1.ask.skill.12345678-1234-1234-1234-123456789abc"
DEVICE = "amzn1.ask.device.living"
USER = "amzn1.ask.account.owner"
SECRET = "a-test-bridge-secret-at-least-32-bytes-long"
DEVICES = {DEVICE: {"device_id": DEVICE, "user_id": USER, "player_id": "Living"}}


class FakeVerifier:
    def __init__(self, *, reject=False):
        self.reject = reject
        self.calls = 0

    async def verify(self, headers, body, payload):
        self.calls += 1
        if self.reject or payload["context"]["System"]["application"]["applicationId"] != SKILL:
            raise Rejected("Invalid Alexa request")


def event(intent="AskChatGPTIntent", *, question="Was ist Licht?", request_id="req-1",
          device=DEVICE, user=USER, person=None, request_type="IntentRequest"):
    system = {"application": {"applicationId": SKILL}, "device": {"deviceId": device},
              "user": {"userId": user}}
    if person is not None:
        system["person"] = {"personId": person}
    req = {"type": request_type, "requestId": request_id, "timestamp": "not-used-by-fake"}
    if intent:
        req["intent"] = {"name": intent, "slots": {"Question": {"value": question}}}
    return {"context": {"System": system}, "request": req}


def raw_event(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()


def request_sig(secret, path, timestamp, nonce, body):
    payload = (b"POST\n" + path.encode() + b"\n" + timestamp.encode() + b"\n" + nonce.encode()
               + b"\n" + hashlib.sha256(body).hexdigest().encode())
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def response_sig(secret, path, nonce, status, body):
    payload = (b"RESPONSE\n" + path.encode() + b"\n" + nonce.encode() + b"\n" + str(status).encode() + b"\n"
               + hashlib.sha256(body).hexdigest().encode())
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


@pytest.fixture
def settings():
    return SimpleNamespace(voice_skill_id=SKILL, voice_bridge_secret=SECRET)


@pytest_asyncio.fixture
async def gateway_pair(monkeypatch, settings):
    questions = []
    jobs = {}
    requests_seen = []
    ready = {"value": False}
    submit_state = {"value": "pending"}
    missing = {"value": False}
    forged_missing = {"value": False}
    wrong_response_path = {"value": None}
    bridge_nonce = set()

    async def check_signature(request, body):
        nonce = request.headers.get("X-Voice-Nonce", "")
        timestamp = request.headers.get("X-Voice-Timestamp", "")
        supplied = request.headers.get("X-Voice-Signature", "")
        if (not nonce or nonce in bridge_nonce or not timestamp.isdecimal()
                or abs(int(time.time()) - int(timestamp)) > 30
                or not hmac.compare_digest(supplied, request_sig(SECRET, request.path,
                                                                   timestamp, nonce, body))):
            return None
        bridge_nonce.add(nonce)
        return nonce

    async def submit(request):
        body = await request.read()
        nonce = await check_signature(request, body)
        if nonce is None:
            return web.json_response({"error": "bad"}, status=401)
        value = json.loads(body)
        job_id = "a" * 32
        questions.append(value)
        state = submit_state["value"]
        jobs[job_id] = {"question": value["question"], "state": state}
        result_data = {"job_id": job_id, "state": state}
        result = json.dumps(result_data, separators=(",", ":")).encode()
        return web.Response(status=202, body=result, headers={"X-Voice-Response-Signature":
                          response_sig(SECRET, request.path, nonce, 202, result), "Content-Type": "application/json"})

    async def result(request):
        body = await request.read()
        nonce = await check_signature(request, body)
        if nonce is None:
            return web.json_response({"error": "bad"}, status=401)
        value = json.loads(body)
        requests_seen.append(value)
        job_state = jobs[value["job_id"]]["state"]
        if missing["value"]:
            response = {"error": "unavailable"}
            status = 404
        elif job_state == "failed":
            response = {"state": "failed", "error": "unavailable"}
            status = 200
        elif ready["value"] or job_state == "done":
            response = {"state": "done", "answer": jobs[value["job_id"]]["question"]}
            status = 200
        else:
            response = {"state": "pending"}
            status = 200
        raw = json.dumps(response, separators=(",", ":")).encode()
        signed_path = wrong_response_path["value"] or request.path
        signature = response_sig(SECRET, signed_path, nonce, status, raw)
        if missing["value"] and forged_missing["value"]:
            signature = "0" * 64
        output = web.StreamResponse(status=status, headers={"X-Voice-Response-Signature":
                                   signature, "Content-Type": "application/json"})
        await output.prepare(request)
        midpoint = max(1, len(raw) // 2)
        await output.write(raw[:midpoint])
        await output.write(raw[midpoint:])
        await output.write_eof()
        return output

    bridge = web.Application()
    bridge.router.add_post("/v1/submit", submit)
    bridge.router.add_post("/v1/result", result)
    async with TestServer(bridge) as bridge_server:
        monkeypatch.setattr(module, "INTERNAL_ORIGIN", str(bridge_server.make_url("")).rstrip("/"))
        async with aiohttp.ClientSession() as session:
            verifier = FakeVerifier()
            gateway = VoiceGateway(session, settings, lambda: DEVICES, verifier=verifier)
            gateway.test_wrong_response_path = wrong_response_path
            app = web.Application(client_max_size=module.MAX_BODY)
            app.router.add_post("/voice", gateway.handle)
            async with TestClient(TestServer(app)) as client:
                yield client, gateway, verifier, questions, requests_seen, ready, submit_state, missing, forged_missing


def signed_headers(value, *, nonce="alexa-signature-is-faked"):
    # The fake verifier owns Alexa authentication in these offline tests.
    return {"Content-Type": "application/json", "X-Test-Nonce": nonce}


def test_optional_configuration_fails_closed():
    assert not voice_gateway_enabled(SimpleNamespace(voice_skill_id="", voice_bridge_secret=""))
    assert voice_gateway_enabled(SimpleNamespace(voice_skill_id=SKILL, voice_bridge_secret=SECRET))
    for skill, secret in ((SKILL, ""), ("bad", SECRET), (SKILL, "short"), (SKILL, "x" * 32 + "!")):
        with pytest.raises(Rejected):
            voice_gateway_enabled(SimpleNamespace(voice_skill_id=skill, voice_bridge_secret=secret))


@pytest.mark.asyncio
async def test_question_is_signed_submitted_and_returns_answer(gateway_pair):
    client, gateway, verifier, questions, result_calls, ready, _submit_state, _missing, _forged = gateway_pair
    ready["value"] = True
    value = event(question="  Was   ist Licht?  ")
    response = await client.post("/voice", data=raw_event(value), headers=signed_headers(value))
    result = await response.json()
    assert response.status == 200
    assert result["response"]["outputSpeech"] == {"type": "PlainText", "text": "Was ist Licht?"}
    assert questions[0]["question"] == "Was ist Licht?"
    assert len(result_calls) == 1
    assert verifier.calls == 1
    principal = gateway._principal_inputs(value)[0]
    assert questions[0]["principal"] == principal
    assert USER not in principal and DEVICE not in principal


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["done", "failed"])
async def test_submit_terminal_states_match_bridge_contract(gateway_pair, state):
    client, _gateway, _verifier, _questions, result_calls, _ready, submit_state, _missing, _forged = gateway_pair
    submit_state["value"] = state
    value = event(question="Sofort", request_id=f"submit-{state}")
    response = await client.post("/voice", data=raw_event(value), headers=signed_headers(value))
    answer = (await response.json())["response"]["outputSpeech"]["text"]
    if state == "done":
        assert answer == "Sofort"
    else:
        assert answer == "Der Gesprächsdienst ist gerade nicht erreichbar."
    assert len(result_calls) == 1


@pytest.mark.asyncio
async def test_pending_answer_is_kept_for_followup_and_repeat(gateway_pair, monkeypatch):
    client, gateway, _verifier, questions, _calls, ready, _submit_state, _missing, _forged = gateway_pair
    monkeypatch.setattr(module, "SUBMIT_POLL_SECONDS", 0.01)
    first = event(question="Frage", request_id="first")
    first_response = await client.post("/voice", data=raw_event(first), headers=signed_headers(first))
    first_json = await first_response.json()
    assert "Sage: Antwort" in first_json["response"]["outputSpeech"]["text"]
    assert first_json["response"]["shouldEndSession"] is False
    assert len(questions) == 1
    ready["value"] = True
    followup = event(intent="GetCodexAnswerIntent", request_id="second")
    followup_response = await client.post("/voice", data=raw_event(followup), headers=signed_headers(followup))
    followup_json = await followup_response.json()
    assert followup_json["response"]["outputSpeech"]["text"] == "Frage"
    assert len(questions) == 1
    repeat = event(intent="AMAZON.RepeatIntent", request_id="repeat")
    repeat_response = await client.post("/voice", data=raw_event(repeat), headers=signed_headers(repeat))
    assert (await repeat_response.json())["response"]["outputSpeech"]["text"] == "Frage"
    assert len(questions) == 1


@pytest.mark.asyncio
async def test_signed_missing_job_clears_pending_and_last_job(gateway_pair, monkeypatch):
    client, gateway, _verifier, _questions, _calls, _ready, _submit, missing, _forged = gateway_pair
    monkeypatch.setattr(module, "SUBMIT_POLL_SECONDS", 0.01)
    first = event(question="Frage", request_id="missing-start")
    response = await client.post("/voice", data=raw_event(first), headers=signed_headers(first))
    await response.json()
    principal = gateway._principal_inputs(first)[0]
    assert principal in gateway.pending and principal in gateway.last_jobs
    missing["value"] = True
    followup = event(intent="GetCodexAnswerIntent", request_id="missing-check")
    response = await client.post("/voice", data=raw_event(followup), headers=signed_headers(followup))
    assert response.headers["Cache-Control"] == "no-store"
    answer = (await response.json())["response"]["outputSpeech"]["text"]
    assert answer == "Die Anfrage ist nicht mehr verfügbar. Stelle deine Frage bitte erneut."
    assert principal not in gateway.pending and principal not in gateway.last_jobs


@pytest.mark.asyncio
async def test_unsigned_404_does_not_clear_pending_job(gateway_pair, monkeypatch):
    client, gateway, _verifier, _questions, _calls, _ready, _submit, missing, forged = gateway_pair
    monkeypatch.setattr(module, "SUBMIT_POLL_SECONDS", 0.01)
    first = event(question="Frage", request_id="forged-start")
    response = await client.post("/voice", data=raw_event(first), headers=signed_headers(first))
    await response.json()
    principal = gateway._principal_inputs(first)[0]
    missing["value"] = True
    forged["value"] = True
    followup = event(intent="GetCodexAnswerIntent", request_id="forged-check")
    response = await client.post("/voice", data=raw_event(followup), headers=signed_headers(followup))
    answer = (await response.json())["response"]["outputSpeech"]["text"]
    assert answer == "Der Gesprächsdienst ist gerade nicht erreichbar."
    assert principal in gateway.pending and principal in gateway.last_jobs


@pytest.mark.asyncio
async def test_404_mac_signed_for_unknown_path_is_rejected_at_result(gateway_pair, monkeypatch):
    client, gateway, _verifier, _questions, _calls, _ready, _submit, missing, _forged = gateway_pair
    monkeypatch.setattr(module, "SUBMIT_POLL_SECONDS", 0.01)
    first = event(question="Frage", request_id="wrong-path-start")
    response = await client.post("/voice", data=raw_event(first), headers=signed_headers(first))
    await response.json()
    principal = gateway._principal_inputs(first)[0]
    missing["value"] = True
    gateway.test_wrong_response_path["value"] = "/unknown"
    followup = event(intent="GetCodexAnswerIntent", request_id="wrong-path-check")
    response = await client.post("/voice", data=raw_event(followup), headers=signed_headers(followup))
    answer = (await response.json())["response"]["outputSpeech"]["text"]
    assert answer == "Der Gesprächsdienst ist gerade nicht erreichbar."
    assert principal in gateway.pending and principal in gateway.last_jobs


@pytest.mark.asyncio
async def test_session_ended_returns_empty_response_and_no_store(gateway_pair):
    client, _gateway, _verifier, _questions, _calls, _ready, _submit, _missing, _forged = gateway_pair
    value = event(intent=None, request_type="SessionEndedRequest", request_id="session-end")
    response = await client.post("/voice", data=raw_event(value), headers=signed_headers(value))
    assert response.headers["Cache-Control"] == "no-store"
    assert (await response.json()) == {"version": "1.0", "response": {}}


@pytest.mark.asyncio
async def test_rejects_invalid_signature_body_and_unpaired_device(gateway_pair):
    client, _gateway, verifier, _questions, _calls, _ready, _submit_state, _missing, _forged = gateway_pair
    verifier.reject = True
    value = event()
    bad = await client.post("/voice", data=raw_event(value), headers=signed_headers(value))
    assert bad.status == 400
    verifier.reject = False
    unknown = event(device="amzn1.ask.device.unknown", request_id="unknown")
    response = await client.post("/voice", data=raw_event(unknown), headers=signed_headers(unknown))
    assert response.status == 403


@pytest.mark.asyncio
async def test_rechecks_revoked_mapping_before_reply(gateway_pair):
    client, gateway, _verifier, _questions, _calls, ready, _submit_state, _missing, _forged = gateway_pair
    ready["value"] = True
    original = gateway.devices_callable
    calls = [0]

    def changing_devices():
        calls[0] += 1
        return DEVICES if calls[0] <= 1 else {}

    gateway.devices_callable = changing_devices
    value = event()
    response = await client.post("/voice", data=raw_event(value), headers=signed_headers(value))
    assert response.status == 403
    gateway.devices_callable = original


@pytest.mark.asyncio
async def test_duplicate_request_reuses_response_but_changed_body_is_rejected(gateway_pair, monkeypatch):
    client, _gateway, _verifier, questions, _calls, ready, _submit_state, _missing, _forged = gateway_pair
    ready["value"] = True
    one = event(question="Erste Frage", request_id="same")
    response1 = await client.post("/voice", data=raw_event(one), headers=signed_headers(one))
    response2 = await client.post("/voice", data=raw_event(one), headers=signed_headers(one, nonce="other"))
    assert await response1.json() == await response2.json()
    assert len(questions) == 1
    changed = event(question="Andere Frage", request_id="same")
    response3 = await client.post("/voice", data=raw_event(changed), headers=signed_headers(changed))
    assert response3.status == 400


@pytest.mark.asyncio
async def test_concurrent_replay_submits_only_once(gateway_pair, monkeypatch):
    client, gateway, _verifier, questions, _calls, _ready, _submit_state, _missing, _forged = gateway_pair
    monkeypatch.setattr(module, "SUBMIT_POLL_SECONDS", 0.01)
    value = event(request_id="concurrent")
    body = raw_event(value)
    first, second = await asyncio.gather(
        client.post("/voice", data=body, headers=signed_headers(value, nonce="one")),
        client.post("/voice", data=body, headers=signed_headers(value, nonce="two")),
    )
    assert first.status == second.status == 200
    assert await first.json() == await second.json()
    assert len(questions) == 1
    assert not gateway.request_locks


@pytest.mark.asyncio
async def test_request_body_is_read_across_chunks():
    body = b'{"question":"chunked"}'

    class Chunks:
        async def iter_chunked(self, _size):
            yield body[:7]
            yield body[7:]

    request = SimpleNamespace(headers={}, content_length=len(body), content=Chunks())
    assert await VoiceGateway._read_body(request) == body


@pytest.mark.asyncio
async def test_bridge_response_mac_is_required(gateway_pair, monkeypatch):
    client, gateway, _verifier, _questions, _calls, ready, _submit_state, _missing, _forged = gateway_pair
    ready["value"] = True
    original_session = gateway.session

    class TamperedResponse:
        def __init__(self, response):
            self._response = response
            self.status = response.status
            self.content = response.content
            self.headers = dict(response.headers)
            self.headers["X-Voice-Response-Signature"] = "0" * 64

    class TamperedContext:
        def __init__(self, context):
            self.context = context

        async def __aenter__(self):
            return TamperedResponse(await self.context.__aenter__())

        async def __aexit__(self, *args):
            return await self.context.__aexit__(*args)

    class TamperedSession:
        def post(self, *args, **kwargs):
            return TamperedContext(original_session.post(*args, **kwargs))

    gateway.session = TamperedSession()
    value = event()
    response = await client.post("/voice", data=raw_event(value), headers=signed_headers(value))
    assert response.status == 200
    spoken = (await response.json())["response"]["outputSpeech"]["text"]
    assert spoken == "Der Gesprächsdienst ist gerade nicht erreichbar."


@pytest.mark.asyncio
async def test_body_limit_and_safe_non_leaking_errors(gateway_pair):
    client, _gateway, _verifier, questions, _calls, _ready, _submit_state, _missing, _forged = gateway_pair
    large = b"{" + b" " * module.MAX_BODY + b"}"
    response = await client.post("/voice", data=large, headers={"Content-Type": "application/json"})
    assert response.status in {400, 413}
    assert not questions


@pytest.mark.asyncio
async def test_pre_auth_peer_rate_limit(gateway_pair):
    client, _gateway, verifier, _questions, _calls, _ready, _submit_state, _missing, _forged = gateway_pair
    verifier.reject = True
    value = event()
    body = raw_event(value)
    statuses = []
    for index in range(121):
        response = await client.post("/voice", data=body, headers=signed_headers(value, nonce=str(index)))
        statuses.append(response.status)
    assert statuses[:120] == [400] * 120
    assert statuses[120] == 429


@pytest.mark.asyncio
async def test_absolute_deadline_returns_safe_speech(gateway_pair, monkeypatch):
    client, gateway, verifier, questions, _calls, _ready, _submit_state, _missing, _forged = gateway_pair
    monkeypatch.setattr(module, "DEADLINE_SECONDS", 0.01)

    async def slow_read(_request):
        await asyncio.sleep(0.05)
        return raw_event(event())

    gateway._read_body = slow_read
    response = await client.post("/voice", data=b"ignored", headers={"Content-Type": "application/json"})
    answer = (await response.json())["response"]["outputSpeech"]["text"]
    assert answer == "Der Gesprächsdienst ist gerade nicht erreichbar."
    assert verifier.calls == 0
    assert not questions


@pytest.mark.asyncio
async def test_spoken_answer_preserves_complete_answer_beyond_old_limit():
    value = ("Ein vollständiger Satz mit einer wichtigen Erklärung. " * 30).strip()
    spoken = VoiceGateway._spoken(value)
    assert len(value) > 480
    assert spoken == value


@pytest.mark.asyncio
async def test_signed_bridge_answer_beyond_old_limit_reaches_alexa_in_full(gateway_pair):
    client, _gateway, _verifier, _questions, _calls, ready, *_rest = gateway_pair
    ready["value"] = True
    question = ("Ein vollständiger Satz. " * 23).strip()
    assert 480 < len(question) <= module.MAX_QUESTION
    value = event(question=question)
    response = await client.post("/voice", data=raw_event(value), headers=signed_headers(value))
    assert response.status == 200
    assert (await response.json())["response"]["outputSpeech"]["text"] == question


def test_spoken_answer_normalizes_whitespace_and_accepts_bridge_limit():
    assert VoiceGateway._spoken("  Hallo\n\tWelt.  ") == "Hallo Welt."
    value = "x" * module.MAX_SPEECH
    assert VoiceGateway._spoken(value) == value


def test_oversized_answer_ends_at_sentence_and_announces_limit():
    spoken = VoiceGateway._spoken("Ein vollständiger Satz. " + "Wort " * 1400)
    assert spoken == ("Ein vollständiger Satz. Die Antwort ist zu lang zum Vorlesen. "
                      "Bitte stelle eine gezieltere Frage.")
    assert len(spoken) <= module.MAX_SPEECH


def test_oversized_answer_without_sentence_does_not_read_fragment():
    spoken = VoiceGateway._spoken("x" * (module.MAX_SPEECH + 1))
    assert spoken == "Die Antwort ist zu lang zum Vorlesen. Bitte stelle eine gezieltere Frage."


def test_unicode_speech_wire_body_fits_lambda_limit():
    answer = "😀" * module.MAX_SPEECH
    response = VoiceGateway._json(VoiceGateway._speech(VoiceGateway._spoken(answer), end=False))
    assert len(response.body) < 32768
    assert json.loads(response.body)["response"]["outputSpeech"]["text"] == answer


def test_sentence_boundary_checks_original_text_and_german_closing_quote():
    notice = "Die Antwort ist zu lang zum Vorlesen. Bitte stelle eine gezieltere Frage."
    budget = module.MAX_SPEECH - len(notice) - 1
    # The cutoff ends exactly on the decimal dot, which is not a sentence end.
    value = "x" * (budget - 2) + "3.14159" + "x" * 100
    assert VoiceGateway._spoken(value) == notice
    assert VoiceGateway._spoken('„Fertig.“ ' + "x" * 6000) == '„Fertig.“ ' + notice
