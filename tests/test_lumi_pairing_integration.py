from __future__ import annotations

import json
import time

import aiohttp
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from music_alexa.companion import Companion, Settings
from music_alexa.lumi_relay import PATH, mac


SKILL = "amzn1.ask.skill.12345678-1234-1234-1234-123456789abc"
DEVICE = "amzn1.ask.device.lumi-echo"
USER = "amzn1.ask.account.lumi-user"
MUSIC_DEVICE = "amzn1.ask.device.music-echo"
MUSIC_USER = "amzn1.ask.account.music-user"
LUMI_SECRET = "offline-lumi-relay-secret-0123456789-abc"
VOICE_SECRET = "offline-voice-bridge-secret-0123456789-abc"
ADMIN_PASSWORD = "offline-admin-password-for-loopback-test"


def settings(*, lumi_mappings=None, music_mappings=None):
    return Settings(
        skill_id=SKILL,
        app_username="local-admin",
        app_password=ADMIN_PASSWORD,
        ma_api_url="http://music-assistant.local:8095",
        ma_api_token="offline-test-only",
        ma_stream_origin="http://music-assistant.local:8097",
        public_base_url="https://companion.example.test",
        device_mappings=music_mappings or [],
        lumi_skill_id=SKILL,
        lumi_relay_secret=LUMI_SECRET,
        voice_bridge_secret=VOICE_SECRET,
        lumi_device_mappings=lumi_mappings or [],
    )


def launch_event(*, application_id=SKILL, session_id="lumi-session-1"):
    return {
        "version": "1.0",
        "session": {
            "application": {"applicationId": application_id},
            "sessionId": session_id,
        },
        "context": {
            "System": {
                "application": {"applicationId": application_id},
                "device": {"deviceId": DEVICE},
                "user": {"userId": USER},
            }
        },
        "request": {"type": "LaunchRequest", "requestId": "launch-1"},
    }


def signed_request(value=None, *, bad_signature=False, nonce=None):
    body = json.dumps(value or launch_event(), separators=(",", ":")).encode("utf-8")
    nonce = nonce or ("a" * 32)
    timestamp = str(int(time.time()))
    signature = mac(LUMI_SECRET, "POST", nonce, timestamp, body)
    if bad_signature:
        signature = "0" * 64
    headers = {
        "Content-Type": "application/json",
        "X-Lumi-Nonce": nonce,
        "X-Lumi-Timestamp": timestamp,
        "X-Lumi-Signature": signature,
    }
    return body, headers


@pytest_asyncio.fixture
async def companion_clients(tmp_path):
    async with aiohttp.ClientSession() as session:
        instance = Companion(settings(), session)
        private = TestClient(TestServer(instance.private_app()))
        public = TestClient(TestServer(instance.public_app()))
        async with private, public:
            yield instance, private, public


def admin_auth():
    return {"Authorization": aiohttp.encode_basic_auth("local-admin", ADMIN_PASSWORD)}


@pytest.mark.asyncio
async def test_unknown_launch_observed_privately_but_never_enrolled_or_dispatched(
    companion_clients, monkeypatch
):
    instance, private, public = companion_clients
    await private.post("/lumi/pairing", json={"duration_seconds": 120}, headers=admin_auth())
    dispatches = []

    async def forbidden_dispatch(*args):
        dispatches.append(args)
        raise AssertionError("unpaired launch must not dispatch")

    monkeypatch.setattr(instance.lumi, "_dispatch", forbidden_dispatch)
    body, headers = signed_request()
    response = await public.post(PATH, data=body, headers=headers)
    assert response.status == 403
    assert response.headers["Cache-Control"] == "no-store"
    assert dispatches == []
    assert instance.settings.lumi_device_mappings == []
    assert instance.lumi.pending == {}
    assert instance.lumi.replies == {}

    unauthorized = await private.get("/lumi/pairing")
    assert unauthorized.status == 401
    assert unauthorized.headers["Cache-Control"] == "no-store"
    observed = await private.get("/lumi/pairing", headers=admin_auth())
    assert observed.status == 200
    assert observed.headers["Cache-Control"] == "no-store"
    document = await observed.json()
    assert document["open"] is True
    assert document["pairs"] == [{
        "application_id": SKILL,
        "device_id": DEVICE,
        "user_id": USER,
    }]
    assert set(document) == {"open", "remaining_seconds", "pairs"}
    closed = await private.delete("/lumi/pairing", headers=admin_auth())
    assert closed.status == 200
    state = await private.get("/lumi/pairing", headers=admin_auth())
    assert (await state.json()) == {"open": False, "remaining_seconds": 0, "pairs": []}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["bad_signature", "wrong_skill"])
async def test_bad_signature_or_wrong_skill_never_observed(companion_clients, failure):
    _, private, public = companion_clients
    await private.post("/lumi/pairing", json={"duration_seconds": 120}, headers=admin_auth())
    value = launch_event(application_id="amzn1.ask.skill.aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa") \
        if failure == "wrong_skill" else launch_event()
    body, headers = signed_request(
        value,
        bad_signature=(failure == "bad_signature"),
        nonce="b" * 32 if failure == "bad_signature" else "c" * 32,
    )
    response = await public.post(PATH, data=body, headers=headers)
    assert response.status == 400
    assert response.headers["Cache-Control"] == "no-store"
    observed = await private.get("/lumi/pairing", headers=admin_auth())
    assert (await observed.json())["pairs"] == []


@pytest.mark.asyncio
async def test_paired_launch_keeps_normal_dispatch(companion_clients, monkeypatch):
    instance, _, public = companion_clients
    instance.settings.lumi_device_mappings = [{"device_id": DEVICE, "user_id": USER}]
    instance.lumi_pairing.open(60)
    dispatches = []

    async def normal_dispatch(req, payload, principal):
        dispatches.append((req["type"], principal))
        return instance.lumi._speech("Launch blieb im normalen Pfad.", end=False)

    monkeypatch.setattr(instance.lumi, "_dispatch", normal_dispatch)
    body, headers = signed_request(nonce="d" * 32)
    response = await public.post(PATH, data=body, headers=headers)
    assert response.status == 200
    assert (await response.json())["response"]["outputSpeech"]["text"] == "Launch blieb im normalen Pfad."
    assert len(dispatches) == 1
    assert instance.lumi_pairing.snapshot()["pairs"] == []


@pytest.mark.asyncio
async def test_admin_window_is_basic_auth_csrf_safe_bounded_and_private(companion_clients):
    _, private, public = companion_clients
    routes = {route.resource.canonical for route in public.server.app.router.routes()}
    assert "/lumi/pairing" not in routes

    opened = await private.post("/lumi/pairing", json={"duration_seconds": 901}, headers=admin_auth())
    assert opened.status == 400
    assert opened.headers["Cache-Control"] == "no-store"
    csrf_headers = {**admin_auth(), "Origin": "https://attacker.invalid"}
    csrf = await private.post("/lumi/pairing", json={"duration_seconds": 10},
                              headers=csrf_headers)
    assert csrf.status == 401
    assert csrf.headers["Cache-Control"] == "no-store"

    opened = await private.post("/lumi/pairing", json={"duration_seconds": 60}, headers=admin_auth())
    assert opened.status == 200
    assert opened.headers["Cache-Control"] == "no-store"
    assert (await opened.json()) == {"status": "open", "remaining_seconds": 60}
    closed = await private.delete("/lumi/pairing", headers=admin_auth())
    assert closed.status == 200
    assert closed.headers["Cache-Control"] == "no-store"
    assert (await closed.json()) == {"status": "closed"}
    state = await private.get("/lumi/pairing", headers=admin_auth())
    assert (await state.json()) == {"open": False, "remaining_seconds": 0, "pairs": []}


@pytest.mark.asyncio
async def test_music_device_mapping_and_seen_surface_remain_separate(companion_clients):
    instance, private, public = companion_clients
    music_map = [{"device_id": MUSIC_DEVICE, "user_id": MUSIC_USER, "player_id": "Office"}]
    instance.settings.device_mappings = music_map
    instance.devices = {MUSIC_DEVICE: dict(music_map[0])}
    await private.post("/lumi/pairing", json={"duration_seconds": 30}, headers=admin_auth())
    body, headers = signed_request(nonce="e" * 32)
    response = await public.post(PATH, data=body, headers=headers)
    assert response.status == 403
    devices = await private.get("/devices", headers=admin_auth())
    assert await devices.json() == {"mappings": music_map, "seen": []}
    assert instance.settings.device_mappings == music_map
    assert instance.settings.lumi_device_mappings == []
    pairing = await private.get("/lumi/pairing", headers=admin_auth())
    assert (await pairing.json())["pairs"] == [{
        "application_id": SKILL,
        "device_id": DEVICE,
        "user_id": USER,
    }]
