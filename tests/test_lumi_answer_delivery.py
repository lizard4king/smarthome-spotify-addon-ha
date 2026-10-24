"""Offline checks of automatic delivery, cancellation and the signed boundary."""
import asyncio
import hashlib
import hmac
import json
import time

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from music_alexa import answer_delivery as delivery
from music_alexa.lumi_relay import LumiRelay, validate_event
from test_lumi_relay import DEVICE, USER, SKILL, event, settings

SECRET = "offline-answer-secret-0123456789-abcdef"


def config(**changes):
    return settings(lumi_answer_targets_json=json.dumps({DEVICE: "buero"}),
                    lumi_answer_bridge_secret=SECRET, **changes)


def setup_gateway(monkeypatch, configuration=None):
    monkeypatch.setattr(delivery, "POLL_SECONDS", .001)
    gateway = LumiRelay(None, configuration or config())
    gateway._submit_poll_seconds = .001
    completed = asyncio.Event()
    calls, spoken = [], []
    async def bridge(path, value):
        calls.append((path, dict(value)))
        if path == "/v1/submit":
            return {"state": "pending", "job_id": ("a" if len([c for c in calls if c[0] == path]) == 1 else "b") * 32}
        return {"state": "done", "answer": "Im Büro sind es 21 Grad."} if completed.is_set() else {"state": "pending"}
    async def speak(self, request_id, target, text):
        spoken.append((request_id, target, text))
    gateway._bridge_call = bridge
    monkeypatch.setattr(delivery.SpeechClient, "speak", speak)
    return gateway, completed, calls, spoken


async def ask(gateway, payload=None):
    payload = payload or event()
    principal = gateway._principal_inputs(payload)[0]
    return await gateway._dispatch(payload["request"], payload, principal)


@pytest.mark.asyncio
async def test_pending_ends_session_and_delivers_exact_job_once(monkeypatch):
    gateway, completed, calls, spoken = setup_gateway(monkeypatch)
    response = await ask(gateway)
    assert response["response"] == {"outputSpeech": {"type": "PlainText", "text": "Einen Moment."}, "shouldEndSession": True}
    slot = gateway.answer_delivery.slots[DEVICE]
    principal = slot.principal
    completed.set()
    await slot.task
    assert len(spoken) == 1 and spoken[0][1:] == ("buero", "Im Büro sind es 21 Grad.")
    assert len(spoken[0][0]) == 32
    assert all(value == {"principal": principal, "job_id": "a" * 32} for path, value in calls if path == "/v1/result")
    assert len([path for path, _ in calls if path == "/v1/submit"]) == 1
    assert not gateway.answer_delivery.slots and not gateway.pending
    await gateway.close()


@pytest.mark.asyncio
async def test_fast_answer_keeps_session_open_and_has_no_delivery(monkeypatch):
    gateway, completed, _, spoken = setup_gateway(monkeypatch)
    completed.set()
    response = await ask(gateway)
    assert response["response"]["shouldEndSession"] is False
    assert response["response"]["outputSpeech"]["text"] == "Im Büro sind es 21 Grad."
    assert not gateway.answer_delivery.slots and not spoken


@pytest.mark.asyncio
async def test_authenticated_request_replay_does_not_submit_or_speak_twice(monkeypatch):
    gateway, completed, calls, spoken = setup_gateway(monkeypatch)
    payload = event()
    raw = json.dumps(payload).encode()
    principal, device, user = gateway._principal_inputs(payload)
    initial = await gateway._process_verified(payload, raw, principal, device, user)
    task = gateway.answer_delivery.slots[DEVICE].task
    completed.set()
    await task
    replay = await gateway._process_verified(payload, raw, principal, device, user)
    assert replay.body == initial.body
    assert len([path for path, _ in calls if path == "/v1/submit"]) == 1
    assert len(spoken) == 1


@pytest.mark.asyncio
async def test_result_arriving_after_job_ttl_is_not_delivered(monkeypatch):
    from music_alexa.voice_gateway import PendingJob
    gateway, completed, _, spoken = setup_gateway(monkeypatch)
    await ask(gateway)
    slot = gateway.answer_delivery.slots[DEVICE]
    async def expired(*args):
        slot.job = PendingJob(slot.job.job_id, time.monotonic() - 1)
        return "Veraltete Antwort."
    gateway.answer_delivery.poll = expired
    await slot.task
    assert not spoken


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["target", "user", "remove", "secret"])
async def test_mapping_changes_block_late_delivery(monkeypatch, change):
    configuration = config()
    gateway, completed, _, spoken = setup_gateway(monkeypatch, configuration)
    await ask(gateway)
    task = gateway.answer_delivery.slots[DEVICE].task
    if change == "target":
        configuration.lumi_answer_targets_json = json.dumps({DEVICE: "kueche"})
    elif change == "user":
        configuration.lumi_device_mappings = [{"device_id": DEVICE, "user_id": USER + "other"}]
    elif change == "remove":
        configuration.lumi_device_mappings = []
    else:
        configuration.lumi_answer_bridge_secret = "x" * 32
    completed.set()
    await task
    assert not spoken and not gateway.answer_delivery.slots


@pytest.mark.asyncio
async def test_new_question_on_new_session_cancels_old_delivery(monkeypatch):
    gateway, completed, calls, spoken = setup_gateway(monkeypatch)
    await ask(gateway)
    old = gateway.answer_delivery.slots[DEVICE]
    new = event()
    new["session"]["sessionId"] = "new-session"
    new["request"]["requestId"] = "second-question"
    await ask(gateway, new)
    slot = gateway.answer_delivery.slots[DEVICE]
    assert slot.principal != old.principal
    assert old.principal not in gateway.pending
    assert old.task.cancelled()
    completed.set()
    await slot.task
    assert len(spoken) == 1
    assert calls[-1][1]["job_id"] == "b" * 32


@pytest.mark.asyncio
@pytest.mark.parametrize("intent", ["AMAZON.StopIntent", "AMAZON.CancelIntent"])
async def test_stop_cancels_and_close_joins_workers(monkeypatch, intent):
    gateway, completed, _, spoken = setup_gateway(monkeypatch)
    await ask(gateway)
    task = gateway.answer_delivery.slots[DEVICE].task
    payload = event(intent)
    validate_event(payload, SKILL)
    response = await ask(gateway, payload)
    assert response["response"]["shouldEndSession"] is True
    completed.set()
    await asyncio.gather(task, return_exceptions=True)
    await gateway.close()
    assert task.cancelled() and not spoken and not gateway.answer_delivery.slots


@pytest.mark.asyncio
async def test_no_target_has_honest_message_without_fetch_word(monkeypatch):
    gateway, completed, _, spoken = setup_gateway(monkeypatch, settings())
    response = await ask(gateway)
    assert "keine automatische Sprachausgabe" in response["response"]["outputSpeech"]["text"]
    assert response["response"]["shouldEndSession"] is True
    assert not gateway.answer_delivery.slots and not spoken


@pytest.mark.asyncio
async def test_poll_failure_is_spoken_and_uncertain_speech_is_never_retried(monkeypatch):
    gateway, _, _, spoken = setup_gateway(monkeypatch)
    await ask(gateway)
    slot = gateway.answer_delivery.slots[DEVICE]
    async def broken(*args):
        raise TimeoutError()
    gateway.answer_delivery.poll = broken
    async def uncertain(self, request_id, target, text):
        spoken.append((request_id, target, text))
        raise TimeoutError()
    monkeypatch.setattr(delivery.SpeechClient, "speak", uncertain)
    await slot.task
    assert len(spoken) == 1 and spoken[0][2] == delivery.FAILURE
    assert not gateway.answer_delivery.slots


@pytest.mark.asyncio
async def test_transient_poll_error_recovers_without_submitting_again(monkeypatch):
    gateway, _, calls, spoken = setup_gateway(monkeypatch)
    await ask(gateway)
    slot = gateway.answer_delivery.slots[DEVICE]
    attempts = 0
    async def recover(*args):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise TimeoutError()
        return "Im Büro sind es 21 Grad."
    gateway.answer_delivery.poll = recover
    await slot.task
    assert attempts == 3 and len(spoken) == 1
    assert spoken[0][2] == "Im Büro sind es 21 Grad."
    assert len([path for path, _ in calls if path == "/v1/submit"]) == 1
    assert not gateway.pending


@pytest.mark.asyncio
async def test_same_job_remains_deduplicated_after_uncertain_delivery(monkeypatch):
    gateway, completed, _, spoken = setup_gateway(monkeypatch)
    async def bridge(path, value):
        if path == "/v1/submit":
            return {"state": "pending", "job_id": "a" * 32}
        return {"state": "done", "answer": "21 Grad."} if completed.is_set() else {"state": "pending"}
    gateway._bridge_call = bridge
    async def uncertain(self, request_id, target, text):
        spoken.append(request_id)
        raise TimeoutError()
    monkeypatch.setattr(delivery.SpeechClient, "speak", uncertain)
    await ask(gateway)
    first = gateway.answer_delivery.slots[DEVICE]
    completed.set()
    await first.task
    completed.clear()
    await ask(gateway)
    second = gateway.answer_delivery.slots[DEVICE]
    assert second.request_id == first.request_id
    completed.set()
    await second.task
    assert spoken == [first.request_id]


@pytest.mark.asyncio
async def test_slots_bounded_and_shutdown_cancels(monkeypatch):
    gateway, _, _, spoken = setup_gateway(monkeypatch)
    monkeypatch.setattr(delivery, "MAX_DELIVERIES", 1)
    await ask(gateway)
    task = gateway.answer_delivery.slots[DEVICE].task
    assert gateway.answer_delivery.reserve("p", DEVICE + "2", USER) is None
    await gateway.close()
    assert task.cancelled() and not spoken


@pytest.mark.asyncio
async def test_shutdown_joins_tasks_already_removed_by_cancel(monkeypatch):
    gateway, _, _, _ = setup_gateway(monkeypatch)
    await ask(gateway)
    task = gateway.answer_delivery.slots[DEVICE].task
    gateway.answer_delivery.cancel(DEVICE)
    assert not gateway.answer_delivery.slots
    await gateway.close()
    assert task.cancelled() and not gateway.answer_delivery.tasks


def test_delivery_cannot_start_without_a_job(monkeypatch):
    gateway, _, _, _ = setup_gateway(monkeypatch)
    slot = gateway.answer_delivery.reserve("p", DEVICE, USER)
    assert gateway.answer_delivery.start(slot) is False
    assert not gateway.answer_delivery.slots


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_mac", [False, True])
async def test_speech_hmac_contract_and_bounded_body(monkeypatch, bad_mac):
    requests = []
    async def speech(request):
        raw = await request.read()
        nonce, stamp = request.headers["X-Control-Nonce"], request.headers["X-Control-Timestamp"]
        expected = hmac.new(SECRET.encode(), f"POST\n/api/codex/speech\n{stamp}\n{nonce}\n{hashlib.sha256(raw).hexdigest()}".encode(), hashlib.sha256).hexdigest()
        assert request.headers["X-Control-Signature"] == expected
        requests.append(json.loads(raw))
        body = b'{"receipt":"accepted"}'
        signature = hmac.new(SECRET.encode(), f"RESPONSE\n/api/codex/speech\n{nonce}\n200\n{hashlib.sha256(body).hexdigest()}".encode(), hashlib.sha256).hexdigest()
        return web.Response(body=body, content_type="application/json", headers={"X-Control-Signature": "0" * 64 if bad_mac else signature})
    app = web.Application()
    app.router.add_post(delivery.SPEECH_PATH, speech)
    monkeypatch.setattr(delivery, "SPEECH_HOST", "127.0.0.1")
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        client = delivery.SpeechClient(session, SECRET, port=server.port)
        if bad_mac:
            with pytest.raises(ValueError, match="Unauthenticated"):
                await client.speak("a" * 32, "buero", "Satz. " * 1000)
        else:
            await client.speak("a" * 32, "buero", "Satz. " * 1000)
    assert len(requests) == 1
    assert set(requests[0]) == {"request_id", "target", "text"}
    assert len(requests[0]["text"]) <= 2000 and "zu lang" in requests[0]["text"]


@pytest.mark.parametrize("raw,secret", [('[]', SECRET), ('{"amzn1.ask.device.echo":"attic"}', SECRET),
                                        ('{"amzn1.ask.device.echo":"buero","amzn1.ask.device.echo":"kueche"}', SECRET),
                                        ('{}', 'short')])
def test_invalid_delivery_configuration_fails_closed(raw, secret):
    configuration = settings(lumi_answer_targets_json=raw, lumi_answer_bridge_secret=secret)
    with pytest.raises(ValueError):
        LumiRelay(None, configuration)
