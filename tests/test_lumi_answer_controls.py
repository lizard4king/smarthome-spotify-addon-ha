"""Exact spoken answer controls retain authenticated identity and existing jobs."""
import asyncio
import copy
import json
import time
from unittest.mock import AsyncMock

import pytest

from music_alexa.answer_pages import ERROR_MESSAGES, PENDING_TEXT, RETRIEVAL_FAILURE_TEXT
from music_alexa.lumi_relay import LumiRelay, LumiBridgeFailure
from music_alexa.voice_gateway import BridgeJobMissing, PendingJob
from test_lumi_answer_delivery import config
from test_lumi_relay import DEVICE, event


def job(letter="a"):
    return PendingJob(letter * 32, time.monotonic() + 600)


async def verified(gateway, question, request_id="control", *, person=None, intent="AskChatGPTIntent"):
    payload = event(intent)
    payload["request"]["requestId"] = request_id
    payload["request"]["intent"]["slots"]["Question"]["value"] = question
    if person:
        payload["context"]["System"]["person"] = {"personId": person}
    original = copy.deepcopy(payload)
    principal, device, user = gateway._principal_inputs(payload)
    response = await gateway._process_verified(payload, json.dumps(payload).encode(), principal, device, user)
    assert payload == original  # Original request identity/digest must stay intact.
    assert response.status == 200
    return json.loads(response.body)["response"]


def text(response):
    return response["outputSpeech"]["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("question", ["Antwort", " ANTWORT ", "Antwort.", "antwort!", "Antwort?"])
async def test_exact_answer_control_fetches_same_job_without_submission(question):
    gateway = LumiRelay(None, config())
    principal = gateway._principal_inputs(event())[0]
    current = gateway.last_jobs[principal] = job()
    gateway._bridge_call = AsyncMock(return_value={"state": "done", "answer": "Gespeicherte Antwort."})
    try:
        response = await verified(gateway, question)
        assert text(response) == "Gespeicherte Antwort."
        assert gateway.last_jobs[principal] is current
        gateway._bridge_call.assert_awaited_once_with("/v1/result", {"principal": principal, "job_id": current.job_id})
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_answer_next_repeat_and_cached_request_replay():
    gateway = LumiRelay(None, config())
    principal = gateway._principal_inputs(event())[0]
    current = gateway.last_jobs[principal] = job()
    gateway.pages.first(principal, current, "Gespeicherter Satz. " * 250)
    pages = gateway.pages.answers[principal]
    gateway._bridge_call = AsyncMock(side_effect=AssertionError("No bridge call for stored pages"))
    try:
        first = await verified(gateway, "Antwort", "first")
        assert text(first) == pages.pages[0]
        second = await verified(gateway, "Antwort", "next")
        assert text(second) == pages.pages[1]
        assert await verified(gateway, "Antwort", "next") == second
        assert text(await verified(gateway, "Wiederholen!", "repeat")) == pages.pages[1]
        assert text(await verified(gateway, "Antwort", "last")) == pages.pages[2]
        assert await verified(gateway, "Antwort", "first") == first
        assert pages.index == 2
        gateway._bridge_call.assert_not_awaited()
    finally:
        await gateway.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("question", ["Antwort", "Wiederholen"])
async def test_control_without_job_does_not_submit(question):
    gateway = LumiRelay(None, config())
    gateway._bridge_call = AsyncMock(side_effect=AssertionError("No job must mean no submission"))
    try:
        assert "keine fertige Antwort" in text(await verified(gateway, question))
        assert not gateway.last_jobs and not gateway.answer_delivery.slots
        gateway._bridge_call.assert_not_awaited()
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_control_does_not_read_foreign_person_pages():
    gateway = LumiRelay(None, config())
    owner = event()
    owner["context"]["System"]["person"] = {"personId": "person-a"}
    principal = gateway._principal_inputs(owner)[0]
    current = gateway.last_jobs[principal] = job()
    gateway.pages.first(principal, current, "Private Antwort.")
    gateway._bridge_call = AsyncMock(side_effect=AssertionError("Foreign control must not submit"))
    try:
        assert "keine fertige Antwort" in text(await verified(gateway, "Antwort", person="person-b"))
        assert gateway.pages.answers[principal].index == 0
        assert gateway.last_jobs[principal] is current
        gateway._bridge_call.assert_not_awaited()
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_control_preserves_active_background_delivery():
    gateway = LumiRelay(None, config())
    principal, device, user = gateway._principal_inputs(event())
    slot = gateway.answer_delivery.reserve(principal, device, user)
    slot.job = gateway.last_jobs[principal] = gateway.pending[principal] = job()
    gateway._bridge_call = AsyncMock(side_effect=AssertionError("Active delivery must not submit or repoll"))
    try:
        response = await verified(gateway, "Antwort")
        assert text(response) == "Einen Moment."
        assert response["shouldEndSession"] is True
        assert gateway.answer_delivery.slots[DEVICE] is slot
        assert gateway.pending[principal] is slot.job
        gateway._bridge_call.assert_not_awaited()
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_control_recovers_known_job_after_transient_fetch_failure():
    gateway = LumiRelay(None, config())
    principal = gateway._principal_inputs(event())[0]
    current = gateway.last_jobs[principal] = job()
    gateway._bridge_call = AsyncMock(side_effect=[LumiBridgeFailure("timeout"), {"state": "done", "answer": "Erholte Antwort."}])
    try:
        assert text(await verified(gateway, "Antwort", "failure")) == RETRIEVAL_FAILURE_TEXT
        assert gateway.last_jobs[principal] is current
        assert text(await verified(gateway, "Antwort", "recovery")) == "Erholte Antwort."
        assert all(call.args == ("/v1/result", {"principal": principal, "job_id": current.job_id})
                   for call in gateway._bridge_call.await_args_list)
    finally:
        await gateway.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("question", ["Was ist die Antwort?", "Antwort bitte", "Antwort!!", "Antwort ?", "Wiederholen bitte", "Stop", "Cancel"])
async def test_genuine_questions_and_non_exact_controls_keep_submission(question):
    gateway = LumiRelay(None, config())
    gateway._bridge_call = AsyncMock(side_effect=[{"state": "done", "job_id": "b" * 32},
                                                {"state": "done", "answer": "Neue Antwort."}])
    try:
        assert text(await verified(gateway, question)) == "Neue Antwort."
        submission = gateway._bridge_call.await_args_list[0]
        assert submission.args[0] == "/v1/submit"
        assert submission.args[1]["question"] == question
        assert submission.args[1]["request_id"] == "control"
        assert gateway._bridge_call.await_count == 2
    finally:
        await gateway.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["failed", "missing"])
@pytest.mark.parametrize("transition", ["supersede", "stop"])
async def test_stale_failure_after_new_question_or_stop_is_suppressed(terminal, transition):
    gateway = LumiRelay(None, config())
    principal = gateway._principal_inputs(event())[0]
    old = gateway.last_jobs[principal] = job()
    entered, release = asyncio.Event(), asyncio.Event()

    async def bridge(path, value):
        if path == "/v1/submit":
            return {"state": "pending", "job_id": "b" * 32}
        if value["job_id"] != old.job_id:
            return {"state": "pending"}
        entered.set()
        await release.wait()
        if terminal == "missing":
            raise BridgeJobMissing("Synthetic missing job")
        return {"state": "failed", "error": "timeout"}

    gateway._bridge_call = bridge
    gateway._submit_poll_seconds = 0
    retrieval = asyncio.create_task(verified(gateway, "Antwort", "old-retrieve"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if transition == "supersede":
            await verified(gateway, "Eine neue Frage", "new-ask")
            current = gateway.last_jobs[principal]
            assert gateway.pending[principal] is current
        else:
            await verified(gateway, "", "stop", intent="AMAZON.StopIntent")
        release.set()
        assert text(await asyncio.wait_for(retrieval, 2)) == PENDING_TEXT
        if transition == "supersede":
            assert gateway.last_jobs[principal] is current
            assert gateway.pending[principal] is current
        else:
            assert principal not in gateway.last_jobs and principal not in gateway.pending
        assert not gateway.pages.available(principal)
    finally:
        release.set()
        await gateway.close()
