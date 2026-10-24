"""Offline relay authentication, isolation and real internal bridge tests."""
import asyncio
import hashlib
import hmac
import json
import secrets
import threading
import time
from types import SimpleNamespace

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from music_alexa import lumi_relay as module
from music_alexa import voice_gateway as voice_module
from music_alexa.companion import Companion, Settings
from music_alexa.lumi_relay import LumiRelay, lumi_relay_enabled, mac
from music_alexa.security import Rejected
from music_alexa.voice_gateway import VoiceGateway, voice_gateway_enabled
from codex_voice.voice_bridge import make_server

SKILL = "amzn1.ask.skill.12345678-1234-1234-1234-123456789abc"
DEVICE = "amzn1.ask.device.echo"
USER = "amzn1.ask.account.lumi"
SECRET = "offline-relay-secret-0123456789-abcdef"
INTERNAL = "offline-bridge-secret-0123456789-abcdef"


def settings(**changes):
    values = dict(voice_skill_id="", voice_bridge_secret=INTERNAL,
                  lumi_skill_id=SKILL, lumi_relay_secret=SECRET,
                  lumi_device_mappings=[{"device_id": DEVICE, "user_id": USER}])
    values.update(changes)
    return SimpleNamespace(**values)


def event(intent="AskChatGPTIntent"):
    return {"version": "1.0", "session": {"application": {"applicationId": SKILL}},
            "context": {"System": {"application": {"applicationId": SKILL},
                                   "device": {"deviceId": DEVICE}, "user": {"userId": USER}}},
            "request": {"type": "IntentRequest", "requestId": "relay-1",
                        "intent": {"name": intent, "slots": {"Question": {"value": "Testfrage"}}}}}


def signed(value=None, *, nonce=None, stamp=None):
    body = json.dumps(value or event(), separators=(",", ":")).encode()
    nonce, stamp = nonce or secrets.token_hex(16), stamp or str(int(time.time()))
    headers = {"Content-Type": "application/json", "X-Lumi-Nonce": nonce,
               "X-Lumi-Timestamp": stamp, "X-Lumi-Signature": mac(SECRET, "POST", nonce, stamp, body)}
    return body, headers


async def check_response(response, nonce, status):
    body = await response.read()
    assert response.status == status
    assert response.headers["Cache-Control"] == "no-store"
    # Independently reconstruct the protocol, including actual status/body bytes.
    message = f"RESPONSE\n/lumi/voice\n{nonce}\n{status}\n{hashlib.sha256(body).hexdigest()}".encode()
    expected = hmac.new(SECRET.encode(), message, hashlib.sha256).hexdigest()
    assert response.headers["X-Lumi-Response-Signature"] == expected
    assert SECRET.encode() not in body and INTERNAL.encode() not in body
    return json.loads(body)


@pytest_asyncio.fixture
async def relay():
    async with aiohttp.ClientSession() as session:
        config = settings()
        gateway = LumiRelay(session, config)
        async def dispatch(*args):
            return gateway._speech("Offline-Antwort")
        gateway._dispatch = dispatch
        app = web.Application()
        app.router.add_post(module.PATH, gateway.handle)
        async with TestClient(TestServer(app)) as client:
            yield client, gateway, config


@pytest.mark.asyncio
async def test_authenticated_and_replay(relay):
    client, _, _ = relay
    body, headers = signed()
    response = await client.post(module.PATH, data=body, headers=headers)
    answer = await check_response(response, headers["X-Lumi-Nonce"], 200)
    assert answer["response"]["outputSpeech"]["text"] == "Offline-Antwort"
    repeated = await client.post(module.PATH, data=body, headers=headers)
    assert repeated.status == 400
    assert "X-Lumi-Response-Signature" not in repeated.headers


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["tamper", "bad_mac", "stale", "encoded", "unsigned", "query"])
async def test_invalid_auth_has_no_response_mac(relay, failure):
    client, _, _ = relay
    body, headers = signed(stamp=str(int(time.time()) - 31) if failure == "stale" else None)
    path = module.PATH
    if failure == "tamper":
        body += b" "
    if failure == "bad_mac":
        headers["X-Lumi-Signature"] = "0" * 64
    if failure == "encoded":
        headers["Content-Encoding"] = "gzip"
    if failure == "unsigned":
        headers.pop("X-Lumi-Signature")
    if failure == "query":
        path += "?x=1"
    # identity avoids aiohttp trying to decompress a deliberately invalid gzip.
    if failure == "encoded":
        headers["Content-Encoding"] = "deflate"
    response = await client.post(path, data=body, headers=headers)
    assert response.status == 400
    assert "X-Lumi-Response-Signature" not in response.headers


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,status", [("context_skill", 400), ("session_skill", 400),
                                            ("token", 400), ("music_intent", 400),
                                            ("unpaired", 403), ("no_mapping", 403)])
async def test_authenticated_rejections_are_signed(relay, failure, status):
    client, _, config = relay
    value = event()
    if failure == "context_skill":
        value["context"]["System"]["application"]["applicationId"] += "x"
    if failure == "session_skill":
        value["session"]["application"]["applicationId"] += "x"
    if failure == "token":
        value["context"]["System"]["apiAccessToken"] = "forbidden-secret"
    if failure == "music_intent":
        value["request"]["intent"]["name"] = "PlayAudio"
    if failure == "unpaired":
        value["context"]["System"]["user"]["userId"] += "other"
    if failure == "no_mapping":
        config.lumi_device_mappings = []
    body, headers = signed(value)
    await check_response(await client.post(module.PATH, data=body, headers=headers), headers["X-Lumi-Nonce"], status)


@pytest.mark.asyncio
async def test_nonce_concurrency_and_capacity(relay, monkeypatch):
    client, gateway, _ = relay
    body, headers = signed()
    responses = await asyncio.gather(*(client.post(module.PATH, data=body, headers=headers) for _ in range(2)))
    assert sorted(item.status for item in responses) == [200, 400]
    monkeypatch.setattr(module, "MAX_NONCES", 1)
    body, headers = signed()
    response = await client.post(module.PATH, data=body, headers=headers)
    assert response.status == 400 and len(gateway.nonces) == 1


@pytest.mark.asyncio
async def test_timeout_signed_and_cache_namespace(relay, monkeypatch):
    client, gateway, config = relay
    direct = VoiceGateway(None, settings(voice_skill_id=SKILL), lambda: {})
    assert gateway._principal_inputs(event())[0] != direct._principal_inputs(event())[0]
    assert gateway.pending is not direct.pending and gateway.replies is not direct.replies
    assert not voice_gateway_enabled(config)
    monkeypatch.setattr(module, "DEADLINE_SECONDS", .02)
    async def slow(*args):
        await asyncio.sleep(1)
    gateway._dispatch = slow
    body, headers = signed()
    started = time.monotonic()
    answer = await check_response(await client.post(module.PATH, data=body, headers=headers), headers["X-Lumi-Nonce"], 200)
    assert time.monotonic() - started < .5
    assert answer["response"]["outputSpeech"]["text"] == "Der Gesprächsdienst ist gerade nicht erreichbar."


def test_configuration_defaults_and_partial():
    assert not lumi_relay_enabled(settings(lumi_skill_id="", lumi_relay_secret="", lumi_device_mappings=[]))
    for changes in ({"lumi_skill_id": ""}, {"lumi_relay_secret": ""}, {"voice_bridge_secret": ""},
                    {"lumi_device_mappings": None}, {"lumi_device_mappings": [{"device_id": DEVICE, "user_id": USER, "player_id": "x"}]},
                    {"lumi_device_mappings": [{"device_id": DEVICE, "user_id": USER}] * 2}):
        with pytest.raises(Rejected):
            lumi_relay_enabled(settings(**changes))
    with pytest.raises(Rejected):
        voice_gateway_enabled(settings(lumi_skill_id=""))


@pytest.mark.asyncio
async def test_companion_routes_and_settings(tmp_path):
    base = dict(skill_id=SKILL, app_username="admin", app_password="offline-password-with-32-characters",
                ma_api_url="http://ma.internal:8095", ma_api_token="offline", ma_stream_origin="http://ma.internal:8097",
                public_base_url="https://example.test", data_dir=tmp_path)
    async with aiohttp.ClientSession() as session:
        disabled = Companion(Settings(**base), session)
        assert module.PATH not in {route.resource.canonical for route in disabled.public_app().router.routes()}
        config = Settings(**base, **vars(settings()))
        enabled = Companion(config, session)
        paths = {route.resource.canonical for route in enabled.public_app().router.routes()}
        assert module.PATH in paths and "/voice" not in paths
        assert enabled.devices == {} and not (tmp_path / "devices.json").exists()
        assert enabled.options_digest() == disabled.options_digest()


class OfflineRuntime:
    def authenticated(self):
        return True

    def query(self, question):
        return "Antwort aus interner Test-Runtime."

    def query_lumi(self, question, *, memory):
        assert set(memory) == {"person_key", "session_key"}
        assert all(value == "" or (isinstance(value, str) and len(value) == 64) for value in memory.values())
        return self.query(question)


def test_relay_budget_preserves_direct_voice_defaults():
    relay = LumiRelay(None, settings())
    direct = VoiceGateway(None, settings(voice_skill_id=SKILL), lambda: {})
    assert direct.bridge_timeout_seconds == 1.5
    assert direct.submit_poll_seconds == 2.0
    assert relay.bridge_timeout_seconds == 1.0
    assert relay.submit_poll_seconds == 4.0
    assert module.DEADLINE_SECONDS == 6.3
    assert 2 * relay.bridge_timeout_seconds + relay.submit_poll_seconds < module.DEADLINE_SECONDS


@pytest.mark.asyncio
async def test_delayed_pending_bridge_returns_pending_not_generic_failure(monkeypatch):
    calls = []
    async def delayed_bridge(request):
        body = await request.read()
        calls.append(request.path)
        # Slow individual calls must still return the normal pending response
        # within the expanded outer budget, not a generic deadline failure.
        await asyncio.sleep(.9 if request.path == "/v1/submit" else .95)
        value = ({"job_id": "a" * 32, "state": "pending"}
                 if request.path == "/v1/submit" else {"state": "pending"})
        raw = json.dumps(value, separators=(",", ":")).encode()
        status = 202 if request.path == "/v1/submit" else 200
        message = (f"RESPONSE\n{request.path}\n{request.headers['X-Voice-Nonce']}\n{status}\n"
                   f"{hashlib.sha256(raw).hexdigest()}").encode()
        signature = hmac.new(INTERNAL.encode(), message, hashlib.sha256).hexdigest()
        return web.Response(status=status, body=raw, headers={"Content-Type": "application/json",
                            "X-Voice-Response-Signature": signature})
    bridge = web.Application()
    bridge.router.add_post("/v1/submit", delayed_bridge)
    bridge.router.add_post("/v1/result", delayed_bridge)
    async with TestServer(bridge) as bridge_server, aiohttp.ClientSession() as session:
        monkeypatch.setattr(voice_module, "INTERNAL_ORIGIN", str(bridge_server.make_url("")).rstrip("/"))
        gateway = LumiRelay(session, settings())
        app = web.Application()
        app.router.add_post(module.PATH, gateway.handle)
        async with TestClient(TestServer(app)) as client:
            body, headers = signed()
            started = time.monotonic()
            answer = await check_response(await client.post(module.PATH, data=body, headers=headers),
                                          headers["X-Lumi-Nonce"], 200)
            elapsed = time.monotonic() - started
            assert "keine automatische Sprachausgabe" in answer["response"]["outputSpeech"]["text"]
            assert elapsed < module.DEADLINE_SECONDS
            assert not gateway.pending and calls[0] == "/v1/submit"
            assert len(calls) >= 3 and set(calls[1:]) == {"/v1/result"}


@pytest.mark.asyncio
async def test_answer_ready_after_two_seconds_is_returned_without_fetch_command(relay):
    client, gateway, _config = relay
    # Authentication fixture stubs dispatch; restore the real question flow.
    del gateway._dispatch
    started = time.monotonic()

    async def bridge(path, _value):
        if path == "/v1/submit":
            return {"job_id": "a" * 32, "state": "pending"}
        if time.monotonic() - started < 2.0:
            return {"state": "pending"}
        return {"state": "done", "answer": "Lissabon ist die Hauptstadt von Portugal."}

    gateway._bridge_call = bridge
    body, headers = signed()
    answer = await check_response(await client.post(module.PATH, data=body, headers=headers),
                                  headers["X-Lumi-Nonce"], 200)
    assert answer["response"]["outputSpeech"]["text"] == "Lissabon ist die Hauptstadt von Portugal."
    assert not gateway.pending
    assert 2.0 <= time.monotonic() - started < 3.0


@pytest.mark.asyncio
async def test_real_internal_bridge_roundtrip(monkeypatch):
    bridge = make_server(OfflineRuntime(), INTERNAL, host="127.0.0.1", port=0)
    thread = threading.Thread(target=bridge.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(voice_module, "INTERNAL_ORIGIN", f"http://127.0.0.1:{bridge.server_port}")
    try:
        async with aiohttp.ClientSession() as session:
            gateway = LumiRelay(session, settings())
            app = web.Application()
            app.router.add_post(module.PATH, gateway.handle)
            async with TestClient(TestServer(app)) as client:
                body, headers = signed()
                answer = await check_response(await client.post(module.PATH, data=body, headers=headers), headers["X-Lumi-Nonce"], 200)
                assert answer["response"]["outputSpeech"]["text"] == "Antwort aus interner Test-Runtime."
                body, headers = signed(event("AMAZON.RepeatIntent"))
                # A repeated request ID with different bytes must fail closed.
                await check_response(await client.post(module.PATH, data=body, headers=headers), headers["X-Lumi-Nonce"], 400)
                repeat = event("AMAZON.RepeatIntent")
                repeat["request"]["requestId"] = "relay-repeat"
                body, headers = signed(repeat)
                answer = await check_response(await client.post(module.PATH, data=body, headers=headers), headers["X-Lumi-Nonce"], 200)
                assert answer["response"]["outputSpeech"]["text"] == "Antwort aus interner Test-Runtime."
    finally:
        bridge.shutdown()
        bridge.server_close()
        thread.join(timeout=1)
