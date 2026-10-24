"""Offline wire tests across the real Alexa gateway and Codex voice bridge."""

from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from codex_voice.voice_bridge import make_server
from music_alexa import voice_gateway as gateway_module
from music_alexa.security import Rejected
from music_alexa.voice_gateway import VoiceGateway


SECRET = "wire-test-secret-0123456789-abcdefgh"
SKILL = "amzn1.ask.skill.12345678-1234-1234-1234-123456789abc"
DEVICE = "amzn1.ask.device.living"
OTHER_DEVICE = "amzn1.ask.device.bedroom"
USER = "amzn1.ask.account.owner"


class SyntheticVerifier:
    async def verify(self, headers, body, payload):
        if payload.get("context", {}).get("System", {}).get("application", {}).get("applicationId") != SKILL:
            raise Rejected("synthetic verification rejected")


class FakeRuntime:
    def __init__(self, *, answer="Antwort aus der Test-Runtime.", failure=None, blocked=False):
        self.answer = answer
        self.failure = failure
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        if not blocked:
            self.release.set()
        self.calls = []

    def authenticated(self):
        return True

    def query(self, question):
        self.calls.append(question)
        self.started.set()
        try:
            self.release.wait(5)
            if self.failure is not None:
                raise RuntimeError(self.failure)
            return self.answer
        finally:
            self.finished.set()


def event(*, intent="AskChatGPTIntent", question="Was ist Licht?", request_id="wire-1",
          device=DEVICE, person=None):
    system = {
        "application": {"applicationId": SKILL},
        "device": {"deviceId": device},
        "user": {"userId": USER},
    }
    if person is not None:
        system["person"] = {"personId": person}
    request = {"type": "IntentRequest", "requestId": request_id, "timestamp": "synthetic"}
    if intent is not None:
        request["intent"] = {"name": intent, "slots": {"Question": {"value": question}}}
    return {"version": "1.0", "context": {"System": system}, "request": request}


@asynccontextmanager
async def running_wire(runtime, monkeypatch, devices=None):
    bridge = make_server(runtime, SECRET, host="127.0.0.1", port=0)
    bridge_thread = threading.Thread(target=bridge.serve_forever, daemon=True)
    bridge_thread.start()
    monkeypatch.setattr(gateway_module, "INTERNAL_ORIGIN", f"http://127.0.0.1:{bridge.server_port}")
    device_rows = devices or {DEVICE: {"user_id": USER}}
    settings = type("Settings", (), {"voice_skill_id": SKILL, "voice_bridge_secret": SECRET})()
    try:
        async with aiohttp.ClientSession() as session:
            gateway = VoiceGateway(session, settings, lambda: device_rows, verifier=SyntheticVerifier())
            gateway._test_bridge = bridge
            app = web.Application(client_max_size=gateway_module.MAX_BODY)
            app.router.add_post("/voice", gateway.handle)
            async with TestClient(TestServer(app, host="127.0.0.1")) as client:
                yield client, gateway, device_rows
    finally:
        bridge.shutdown()
        bridge.server_close()
        bridge_thread.join(timeout=1)


async def ask(client, value):
    return await client.post("/voice", json=value, headers={"Content-Type": "application/json"})


def spoken(response_json):
    return response_json["response"]["outputSpeech"]["text"]


@pytest.mark.asyncio
async def test_real_gateway_bridge_immediate_answer_uses_submit_202_and_result_200(monkeypatch):
    runtime = FakeRuntime()
    async with running_wire(runtime, monkeypatch) as (client, _gateway, _devices):
        response = await ask(client, event())
        body = await response.json()
        assert response.status == 200
        assert spoken(body) == runtime.answer
        assert runtime.calls == ["Was ist Licht?"]


@pytest.mark.asyncio
async def test_lost_gateway_state_reuses_completed_bridge_job_without_second_query(monkeypatch):
    runtime = FakeRuntime(answer="Deduplizierte Antwort.")
    async with running_wire(runtime, monkeypatch) as (client, gateway, _devices):
        value = event(request_id="response-lost", question="Dieselbe Frage")
        first = await ask(client, value)
        assert spoken(await first.json()) == "Deduplizierte Antwort."
        assert runtime.calls == ["Dieselbe Frage"]

        # Simulate process-local gateway state loss after the remote job completed.
        gateway.replies.clear()
        gateway.last_jobs.clear()
        gateway.pending.clear()

        repeated = await ask(client, value)
        assert repeated.status == 200
        assert spoken(await repeated.json()) == "Deduplizierte Antwort."
        assert runtime.calls == ["Dieselbe Frage"]


@pytest.mark.asyncio
async def test_real_gateway_bridge_returns_pending_then_followup_answer(monkeypatch):
    runtime = FakeRuntime(answer="Späte Antwort.", blocked=True)
    monkeypatch.setattr(gateway_module, "SUBMIT_POLL_SECONDS", 0.02)
    async with running_wire(runtime, monkeypatch) as (client, _gateway, _devices):
        first = await ask(client, event(request_id="slow-ask"))
        first_body = await first.json()
        assert first.status == 200
        assert "Sage: Antwort" in spoken(first_body)
        assert first_body["response"]["shouldEndSession"] is False
        assert await asyncio.to_thread(runtime.started.wait, 1)

        runtime.release.set()
        assert await asyncio.to_thread(runtime.finished.wait, 1)
        followup = await ask(client, event(intent="GetCodexAnswerIntent", question="",
                                           request_id="slow-result"))
        assert spoken(await followup.json()) == "Späte Antwort."
        assert runtime.calls == ["Was ist Licht?"]


@pytest.mark.asyncio
async def test_expired_bridge_job_clears_gateway_state_and_allows_new_question(monkeypatch):
    runtime = FakeRuntime(answer="Frische Antwort.", blocked=True)
    monkeypatch.setattr(gateway_module, "SUBMIT_POLL_SECONDS", 0.02)
    async with running_wire(runtime, monkeypatch) as (client, gateway, _devices):
        first_question = event(request_id="expires-first", question="Erste Frage")
        first = await ask(client, first_question)
        assert "Sage: Antwort" in spoken(await first.json())
        assert await asyncio.to_thread(runtime.started.wait, 1)
        runtime.release.set()
        assert await asyncio.to_thread(runtime.finished.wait, 1)

        bridge = gateway._test_bridge
        principal = gateway._principal_inputs(first_question)[0]
        assert gateway.pending[principal]
        with bridge.jobs._condition:
            bridge.jobs._records.clear()
            bridge.jobs._dedupe.clear()
            bridge.jobs._queue.clear()

        followup = await ask(client, event(intent="GetCodexAnswerIntent", question="",
                                           request_id="expires-retrieve"))
        assert spoken(await followup.json()) == "Die Anfrage ist nicht mehr verfügbar. Stelle deine Frage bitte erneut."
        assert principal not in gateway.pending
        assert principal not in gateway.last_jobs

        next_question = await ask(client, event(request_id="expires-next", question="Neue Frage"))
        assert spoken(await next_question.json()) == "Frische Antwort."
        assert runtime.calls == ["Erste Frage", "Neue Frage"]


@pytest.mark.asyncio
async def test_real_gateway_bridge_hides_runtime_failure(monkeypatch):
    runtime = FakeRuntime(failure="synthetic private traceback and question text")
    async with running_wire(runtime, monkeypatch) as (client, _gateway, _devices):
        response = await ask(client, event(request_id="failure"))
        result = await response.json()
        assert response.status == 200
        assert spoken(result) == "Der Gesprächsdienst ist gerade nicht erreichbar."
        assert "synthetic private" not in await response.text()


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ["foreign-device", "foreign-person"])
async def test_other_device_or_person_cannot_retrieve_completed_answer(monkeypatch, identity):
    runtime = FakeRuntime(answer="Privates Ergebnis.", blocked=True)
    monkeypatch.setattr(gateway_module, "SUBMIT_POLL_SECONDS", 0.02)
    devices = {
        DEVICE: {"user_id": USER},
        OTHER_DEVICE: {"user_id": USER},
    }
    async with running_wire(runtime, monkeypatch, devices) as (client, _gateway, _devices):
        first = await ask(client, event(request_id=f"owner-{identity}", person="speaker-one"))
        assert "Sage: Antwort" in spoken(await first.json())
        assert await asyncio.to_thread(runtime.started.wait, 1)
        runtime.release.set()
        assert await asyncio.to_thread(runtime.finished.wait, 1)

        if identity == "foreign-device":
            other = event(intent="GetCodexAnswerIntent", question="", request_id="other-device",
                          device=OTHER_DEVICE, person="speaker-one")
        else:
            other = event(intent="GetCodexAnswerIntent", question="", request_id="other-person",
                          device=DEVICE, person="speaker-two")
        response = await ask(client, other)
        assert response.status == 200
        text = spoken(await response.json())
        assert text == "Es gibt keine fertige Antwort. Stelle deine Frage bitte noch einmal."
        assert "Privates Ergebnis" not in text
        assert runtime.calls == ["Was ist Licht?"]

