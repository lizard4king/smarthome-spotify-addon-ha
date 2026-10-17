"""Offline tests for pseudonymous Lumi conversation-memory wire metadata."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import http.client
import json
import secrets
import threading
import time
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from codex_voice.voice_bridge import make_server
from music_alexa import voice_gateway as gateway_module
from music_alexa.lumi_relay import LumiRelay, mac, validate_event
from music_alexa.security import Rejected
from music_alexa.voice_gateway import PendingJob, VoiceGateway


SKILL = "amzn1.ask.skill.12345678-1234-1234-1234-123456789abc"
DEVICE = "amzn1.ask.device.echo"
USER = "amzn1.ask.account.owner"
INTERNAL_SECRET = "offline-voice-bridge-secret-0123456789-abcdef"
RELAY_SECRET = "offline-lumi-relay-secret-0123456789-abcdef"
PRINCIPAL = "a" * 64


def settings(**changes):
    values = {
        "voice_skill_id": "",
        "voice_bridge_secret": INTERNAL_SECRET,
        "lumi_skill_id": SKILL,
        "lumi_relay_secret": RELAY_SECRET,
        "lumi_device_mappings": [{"device_id": DEVICE, "user_id": USER}],
    }
    values.update(changes)
    return SimpleNamespace(**values)


def event(*, person_id=None, session_id="session-a", device_id=DEVICE, user_id=USER):
    system = {
        "application": {"applicationId": SKILL},
        "device": {"deviceId": device_id},
        "user": {"userId": user_id},
    }
    if person_id is not None:
        system["person"] = {"personId": person_id}
    session = {"application": {"applicationId": SKILL}}
    if session_id is not None:
        session["sessionId"] = session_id
    return {
        "version": "1.0",
        "session": session,
        "context": {"System": system},
        "request": {
            "type": "IntentRequest",
            "requestId": "request-1",
            "intent": {"name": "AskChatGPTIntent", "slots": {
                "Question": {"name": "Question", "value": "Erkläre das kurz."},
            }},
        },
    }


def expected_memory_key(secret, domain, *parts):
    message = domain.encode("ascii") + b"\0" + json.dumps(
        parts, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def relay_signature(payload, nonce="1" * 32):
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    stamp = str(int(time.time()))
    headers = {
        "Content-Type": "application/json",
        "X-Lumi-Timestamp": stamp,
        "X-Lumi-Nonce": nonce,
        "X-Lumi-Signature": mac(RELAY_SECRET, "POST", nonce, stamp, body),
    }
    return body, headers


class LumiRuntime:
    def __init__(self):
        self.calls = []

    def authenticated(self):
        return True

    def query_lumi(self, question, *, memory):
        self.calls.append((question, memory))
        return "Antwort mit freigegebenem Kontext."

    def query_voice(self, _question):
        pytest.fail("Lumi memory must not fall back to the legacy query path")


@pytest.mark.asyncio
async def test_signed_relay_and_bridge_wire_reach_query_lumi(monkeypatch):
    runtime = LumiRuntime()
    bridge = make_server(runtime, INTERNAL_SECRET, host="127.0.0.1", port=0)
    thread = threading.Thread(target=bridge.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(
        gateway_module,
        "INTERNAL_ORIGIN",
        f"http://127.0.0.1:{bridge.server_port}",
    )
    try:
        async with aiohttp.ClientSession() as session:
            relay = LumiRelay(session, settings())
            app = web.Application()
            app.router.add_post("/lumi/voice", relay.handle)
            async with TestClient(TestServer(app)) as client:
                body, headers = relay_signature(event(person_id="verified-person-a"))
                response = await client.post("/lumi/voice", data=body, headers=headers)
                document = await response.json()
                assert response.status == 200
                assert document["response"]["outputSpeech"]["text"] == "Antwort mit freigegebenem Kontext."

        assert len(runtime.calls) == 1
        question, memory = runtime.calls[0]
        assert question == "Erkläre das kurz."
        assert memory == {
            "person_key": expected_memory_key(
                INTERNAL_SECRET, "lumi-memory-person-v1", SKILL, USER, "verified-person-a"
            ),
            "session_key": expected_memory_key(
                INTERNAL_SECRET, "lumi-memory-person-session-v1", SKILL, USER, DEVICE, "session-a", "verified-person-a"
            ),
        }
    finally:
        bridge.shutdown()
        bridge.server_close()
        thread.join(timeout=1)


def test_person_key_is_device_independent_session_key_is_not_and_missing_person_is_empty():
    relay = LumiRelay(None, settings())
    base = event(person_id="verified-person-a", session_id="session-a")
    req = base["request"]
    first = relay._submission_value(req, base, PRINCIPAL, "question")["memory"]

    other_device = event(
        person_id="verified-person-a",
        session_id="session-a",
        device_id="amzn1.ask.device.bedroom",
    )
    changed_device = relay._submission_value(
        other_device["request"], other_device, PRINCIPAL, "question"
    )["memory"]
    assert changed_device["person_key"] == first["person_key"]
    assert changed_device["session_key"] != first["session_key"]

    other_person = event(person_id="verified-person-b", session_id="session-a")
    changed_person = relay._submission_value(
        other_person["request"], other_person, PRINCIPAL, "question"
    )["memory"]
    assert changed_person["person_key"] != first["person_key"]
    assert changed_person["session_key"] != first["session_key"]

    unknown = event(session_id="session-a")
    unknown_memory = relay._submission_value(
        unknown["request"], unknown, PRINCIPAL, "question"
    )["memory"]
    assert unknown_memory["person_key"] == ""
    assert unknown_memory["session_key"] != first["session_key"]


@pytest.mark.asyncio
async def test_unknown_person_job_principal_is_session_scoped_and_person_principal_is_stable():
    relay = LumiRelay(None, settings())
    session_a = event(session_id="session-a")
    session_a_retry = event(session_id="session-a")
    session_b = event(session_id="session-b")

    principal_a, _, _ = relay._principal_inputs(session_a)
    retry_principal, _, _ = relay._principal_inputs(session_a_retry)
    principal_b, _, _ = relay._principal_inputs(session_b)
    assert retry_principal == principal_a
    assert principal_b != principal_a

    memory_a = relay._submission_value(
        session_a["request"], session_a, principal_a, "question"
    )["memory"]
    memory_retry = relay._submission_value(
        session_a_retry["request"], session_a_retry, retry_principal, "question"
    )["memory"]
    memory_b = relay._submission_value(
        session_b["request"], session_b, principal_b, "question"
    )["memory"]
    assert memory_retry == memory_a
    assert memory_b["person_key"] == ""
    assert memory_b["session_key"] != memory_a["session_key"]

    no_session_a = event(session_id=None)
    no_session_b = event(session_id=None)
    no_session_principal_a, _, _ = relay._principal_inputs(no_session_a)
    no_session_principal_b, _, _ = relay._principal_inputs(no_session_b)
    no_session_memory = relay._submission_value(
        no_session_a["request"], no_session_a, no_session_principal_a, "question"
    )["memory"]
    assert no_session_principal_a == no_session_principal_b
    assert no_session_memory == {"person_key": "", "session_key": ""}

    relay.pending[principal_a] = PendingJob("1" * 32, time.monotonic() + 60)
    relay.last_jobs[principal_a] = relay.pending[principal_a]
    response = await relay._dispatch(
        {"type": "IntentRequest", "requestId": "other-session-retrieval", "intent": {"name": "GetCodexAnswerIntent"}},
        session_b,
        principal_b,
    )
    assert response["response"]["outputSpeech"]["text"] == (
        "Es gibt keine fertige Antwort. Stelle deine Frage bitte noch einmal."
    )

    verified_a = event(person_id="verified-person-a", session_id="session-a")
    verified_b = event(person_id="verified-person-a", session_id="session-b")
    verified_principal_a, _, _ = relay._principal_inputs(verified_a)
    verified_principal_b, _, _ = relay._principal_inputs(verified_b)
    assert verified_principal_a == verified_principal_b


def test_session_person_is_accepted_but_never_used_as_speaker_identity():
    relay = LumiRelay(None, settings())
    first = event(session_id="session-a")
    first["session"]["person"] = {"personId": "session-only-person"}
    second = event(session_id="session-b")
    second["session"]["person"] = {"personId": "session-only-person"}

    validate_event(first, SKILL)
    principal_a, _, _ = relay._principal_inputs(first)
    principal_b, _, _ = relay._principal_inputs(second)
    memory_a = relay._submission_value(
        first["request"], first, principal_a, "question"
    )["memory"]
    memory_b = relay._submission_value(
        second["request"], second, principal_b, "question"
    )["memory"]

    assert principal_a != principal_b
    assert memory_a["person_key"] == memory_b["person_key"] == ""
    assert memory_a["session_key"] != memory_b["session_key"]


def test_malformed_optional_identity_is_rejected():
    value = event(session_id=[])
    with pytest.raises(Rejected):
        validate_event(value, SKILL)

    relay = LumiRelay(None, settings())
    with pytest.raises(Rejected):
        relay._principal_inputs(value)
    value = event(person_id="bad\nperson")
    with pytest.raises(Rejected):
        relay._submission_value(value["request"], value, PRINCIPAL, "question")


def test_base_voice_gateway_keeps_legacy_three_field_request():
    gateway = VoiceGateway(None, settings(voice_skill_id=SKILL), lambda: {})
    request = {"requestId": "request-1"}
    assert gateway._submission_value(request, {}, PRINCIPAL, "question") == {
        "principal": PRINCIPAL,
        "request_id": "request-1",
        "question": "question",
    }


def bridge_submit(port, document, nonce):
    body = json.dumps(document, separators=(",", ":")).encode("utf-8")
    timestamp = str(int(time.time()))
    canonical = (
        f"POST\n/v1/submit\n{timestamp}\n{nonce}\n{hashlib.sha256(body).hexdigest()}"
    ).encode("ascii")
    signature = hmac.new(INTERNAL_SECRET.encode(), canonical, hashlib.sha256).hexdigest()
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    connection.request("POST", "/v1/submit", body, headers={
        "Content-Type": "application/json",
        "X-Voice-Timestamp": timestamp,
        "X-Voice-Nonce": nonce,
        "X-Voice-Signature": signature,
    })
    response = connection.getresponse()
    response.read()
    status = response.status
    connection.close()
    return status


def test_bridge_rejects_invalid_memory_and_dedupes_memory_changes_as_conflicts():
    runtime = LumiRuntime()
    bridge = make_server(runtime, INTERNAL_SECRET, host="127.0.0.1", port=0)
    thread = threading.Thread(target=bridge.serve_forever, daemon=True)
    thread.start()
    try:
        invalid = {
            "principal": PRINCIPAL,
            "request_id": "invalid-memory",
            "question": "question",
            "memory": {"person_key": "wrong", "session_key": ""},
        }
        assert bridge_submit(bridge.server_port, invalid, "2" * 32) == 400

        first = {"person_key": "b" * 64, "session_key": ""}
        changed = {"person_key": "c" * 64, "session_key": ""}
        status, _ = bridge.jobs.submit(PRINCIPAL, "same-request", "question", memory=first)
        assert status == 202
        status, _ = bridge.jobs.submit(PRINCIPAL, "same-request", "question", memory=changed)
        assert status == 409
    finally:
        bridge.shutdown()
        bridge.server_close()
        thread.join(timeout=1)


def test_legacy_jobs_use_query_voice_and_memory_jobs_fail_closed_without_query_lumi():
    class LegacyRuntime:
        def authenticated(self):
            return True

        def query_voice(self, _question):
            return "legacy answer"

        def query(self, _question):
            return "legacy answer"

    from codex_voice.voice_bridge import VoiceJobs

    jobs = VoiceJobs(LegacyRuntime())
    try:
        status, result = jobs.submit(PRINCIPAL, "legacy", "question")
        assert status == 202
        job_id = result["job_id"]
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            result_status, result = jobs.result(PRINCIPAL, job_id)
            if result.get("state") != "pending":
                break
            time.sleep(0.01)
        assert result_status == 200
        assert result == {"state": "done", "answer": "legacy answer"}

        status, memory_job = jobs.submit(
            PRINCIPAL,
            "memory",
            "question",
            memory={"person_key": "d" * 64, "session_key": ""},
        )
        assert status == 202
        job_id = memory_job["job_id"]
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            result_status, result = jobs.result(PRINCIPAL, job_id)
            if result.get("state") != "pending":
                break
            time.sleep(0.01)
        assert result_status == 200
        assert result == {"state": "failed", "error": "unavailable"}
    finally:
        jobs.close()
