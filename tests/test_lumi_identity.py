"""Owner-key display window (synthetic IDs only, loopback servers, no devices)."""
from __future__ import annotations

import json
import hashlib
import hmac
import time

import aiohttp
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from music_alexa.companion import Companion, Settings
from music_alexa.lumi_identity import LumiIdentityObserver, MAX_SPEAKERS
from music_alexa.lumi_relay import PATH, mac

SKILL = "amzn1.ask.skill.12345678-1234-1234-1234-123456789abc"
DEVICE = "amzn1.ask.device.lumi-echo"
USER = "amzn1.ask.account.lumi-user"
PERSON = "amzn1.ask.person.lumi-person"
LUMI_SECRET = "offline-lumi-relay-secret-0123456789-abc"
VOICE_SECRET = "offline-voice-bridge-secret-0123456789-abc"
ADMIN_PASSWORD = "offline-admin-password-for-loopback-test"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_window_is_bounded_format_checked_and_erased_on_close():
    clock = Clock()
    observer = LumiIdentityObserver(clock=clock)
    assert observer.observe("a" * 64) is False  # closed window
    for invalid in (0, 901, True, "60"):
        with pytest.raises(ValueError):
            observer.open(invalid)
    observer.open(60)
    assert observer.observe(PERSON) is False  # raw Alexa IDs are never accepted
    assert observer.observe("A" * 64) is False
    assert observer.observe("a" * 64) and observer.observe("a" * 64)
    snapshot = observer.snapshot()
    assert snapshot["speakers"] == [{"person_key": "a" * 64, "requests": 2, "seconds_since_last": 0}]
    for index in range(MAX_SPEAKERS + 3):
        observer.observe(f"{index:064x}")
    assert len(observer.snapshot()["speakers"]) == MAX_SPEAKERS
    clock.now += 61
    assert observer.snapshot() == {"open": False, "remaining_seconds": 0, "speakers": []}
    observer.open(60)
    observer.observe("b" * 64)
    observer.close()
    assert observer.snapshot()["speakers"] == []


def settings():
    return Settings(
        skill_id=SKILL, app_username="local-admin", app_password=ADMIN_PASSWORD,
        ma_api_url="http://music-assistant.local:8095", ma_api_token="offline-test-only",
        ma_stream_origin="http://music-assistant.local:8097",
        public_base_url="https://companion.example.test", device_mappings=[],
        lumi_skill_id=SKILL, lumi_relay_secret=LUMI_SECRET, voice_bridge_secret=VOICE_SECRET,
        lumi_device_mappings=[{"device_id": DEVICE, "user_id": USER}],
    )


def question_event(*, person=True):
    system = {"application": {"applicationId": SKILL}, "device": {"deviceId": DEVICE},
              "user": {"userId": USER}}
    if person:
        system["person"] = {"personId": PERSON}
    return {"version": "1.0", "session": {"application": {"applicationId": SKILL}},
            "context": {"System": system},
            "request": {"type": "IntentRequest", "requestId": "question-1",
                        "intent": {"name": "AskChatGPTIntent",
                                   "slots": {"Question": {"value": "Wie spät ist es?"}}}}}


def signed(value, nonce):
    body = json.dumps(value, separators=(",", ":")).encode("utf-8")
    timestamp = str(int(time.time()))
    return body, {"Content-Type": "application/json", "X-Lumi-Nonce": nonce,
                  "X-Lumi-Timestamp": timestamp,
                  "X-Lumi-Signature": mac(LUMI_SECRET, "POST", nonce, timestamp, body)}


def admin_auth():
    return {"Authorization": aiohttp.encode_basic_auth("local-admin", ADMIN_PASSWORD)}


@pytest_asyncio.fixture
async def clients():
    async with aiohttp.ClientSession() as session:
        instance = Companion(settings(), session)
        private = TestClient(TestServer(instance.private_app()))
        public = TestClient(TestServer(instance.public_app()))
        async with private, public:
            yield instance, private, public


@pytest.mark.asyncio
async def test_recognized_speaker_key_matches_memory_key_and_hides_raw_ids(clients, monkeypatch):
    instance, private, public = clients

    async def answer(req, payload, principal):
        return instance.lumi._speech("Es ist zwölf.", end=False)
    monkeypatch.setattr(instance.lumi, "_dispatch", answer)

    body, headers = signed(question_event(), "e" * 32)
    assert (await public.post(PATH, data=body, headers=headers)).status == 200
    closed_state = await (await private.get("/lumi/identity", headers=admin_auth())).json()
    assert closed_state["speakers"] == []  # nothing is recorded without an open window

    opened = await private.post("/lumi/identity", json={"duration_seconds": 300}, headers=admin_auth())
    assert opened.status == 200 and opened.headers["Cache-Control"] == "no-store"
    body, headers = signed(question_event(), "f" * 32)
    assert (await public.post(PATH, data=body, headers=headers)).status == 200
    body, headers = signed(question_event(person=False), "1" * 32)
    assert (await public.post(PATH, data=body, headers=headers)).status == 200

    response = await private.get("/lumi/identity", headers=admin_auth())
    raw = await response.text()
    document = json.loads(raw)
    expected = instance.lumi._memory_value(question_event())["person_key"]
    independent = hmac.new(VOICE_SECRET.encode(), b"lumi-memory-person-v1\0" + json.dumps(
        [SKILL, USER, PERSON], separators=(",", ":")).encode(), hashlib.sha256).hexdigest()
    assert expected == independent == "83d24c62ccc5d2fcc43aaab409853ba7192f6dd76ad7370a348d2db1968388d3"
    assert [item["person_key"] for item in document["speakers"]] == [expected]
    for identifier in (PERSON, USER, DEVICE, SKILL):
        assert identifier not in raw


@pytest.mark.asyncio
async def test_identity_window_is_private_authenticated_and_csrf_safe(clients):
    _, private, public = clients
    routes = {route.resource.canonical for route in public.server.app.router.routes()}
    assert "/lumi/identity" not in routes
    assert (await private.get("/lumi/identity")).status == 401
    csrf = await private.post("/lumi/identity", json={"duration_seconds": 10},
                              headers={**admin_auth(), "Origin": "https://attacker.invalid"})
    assert csrf.status == 401
    assert (await private.post("/lumi/identity", json={"duration_seconds": 901},
                               headers=admin_auth())).status == 400
    closed = await private.delete("/lumi/identity", headers=admin_auth())
    assert (await closed.json()) == {"status": "closed"}


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["signature", "application", "person", "session"])
async def test_rejected_relay_event_never_adds_an_identity(clients, monkeypatch, fault):
    instance, private, public = clients
    monkeypatch.setattr(instance.lumi, "_dispatch", lambda *a: pytest.fail("Rejected event dispatched"))
    assert (await private.post("/lumi/identity", json={"duration_seconds": 300},
                               headers=admin_auth())).status == 200
    event = question_event()
    if fault == "application":
        event["context"]["System"]["application"]["applicationId"] = (
            "amzn1.ask.skill.00000000-0000-0000-0000-000000000000")
    elif fault == "person":
        event["context"]["System"]["person"]["personId"] = 42
    elif fault == "session":
        event["session"]["sessionId"] = ""
    body, headers = signed(event, "a" * 32)
    if fault == "signature":
        headers["X-Lumi-Signature"] = "0" * 64
    assert (await public.post(PATH, data=body, headers=headers)).status == 400
    state = await (await private.get("/lumi/identity", headers=admin_auth())).json()
    assert state["speakers"] == []


@pytest.mark.asyncio
async def test_window_reopen_delete_and_expiry_erase_observed_results(clients, monkeypatch):
    instance, private, public = clients
    clock = Clock()
    instance.lumi_identity._clock = clock

    async def answer(*args):
        return instance.lumi._speech("Lokale Testantwort.", end=False)
    monkeypatch.setattr(instance.lumi, "_dispatch", answer)
    for nonce in ("b", "c"):
        assert (await private.post("/lumi/identity", json={"duration_seconds": 300},
                                   headers=admin_auth())).status == 200
        state = await (await private.get("/lumi/identity", headers=admin_auth())).json()
        assert state["speakers"] == []
        body, headers = signed(question_event(), nonce * 32)
        assert (await public.post(PATH, data=body, headers=headers)).status == 200
        assert len((await (await private.get("/lumi/identity", headers=admin_auth())).json())["speakers"]) == 1
    clock.now += 301
    state = await (await private.get("/lumi/identity", headers=admin_auth())).json()
    assert state == {"open": False, "remaining_seconds": 0, "speakers": []}
    await private.post("/lumi/identity", json={"duration_seconds": 300}, headers=admin_auth())
    body, headers = signed(question_event(), "d" * 32)
    assert (await public.post(PATH, data=body, headers=headers)).status == 200
    assert (await private.delete("/lumi/identity", headers=admin_auth())).status == 200
    assert (await (await private.get("/lumi/identity", headers=admin_auth())).json()) == {
        "open": False, "remaining_seconds": 0, "speakers": []}
