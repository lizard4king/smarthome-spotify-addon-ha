"""Offline distinction between bridge transport failures and terminal job errors."""
import time
from unittest.mock import AsyncMock

import pytest

from music_alexa.answer_pages import (
    ERROR_MESSAGES, RETRIEVAL_FAILURE_TEXT, SUBMISSION_TIMEOUT_TEXT,
)
from music_alexa.lumi_relay import LumiBridgeFailure, LumiRelay
from music_alexa.voice_gateway import PendingJob
from test_lumi_relay import DEVICE, event, settings


def speech(response):
    assert response["response"]["shouldEndSession"] is False
    text = response["response"]["outputSpeech"]["text"]
    assert "private-diagnostic" not in text
    return text


async def dispatch(gateway, intent, request_id):
    payload = event(intent)
    payload["request"]["requestId"] = request_id
    principal = gateway._principal_inputs(payload)[0]
    return await gateway._dispatch(payload["request"], payload, principal)


@pytest.mark.asyncio
@pytest.mark.parametrize("pending", [False, True])
@pytest.mark.parametrize("failure", ["timeout", "unavailable"])
async def test_known_job_fetch_can_recover_after_transport_failure(pending, failure):
    gateway = LumiRelay(None, settings())
    principal = gateway._principal_inputs(event())[0]
    job = PendingJob("a" * 32, time.monotonic() + 60)
    gateway.last_jobs[principal] = job
    if pending:
        gateway.pending[principal] = job
    # Exercise the actual timeout conversion instead of replacing _bridge_call.
    exception = TimeoutError("private-diagnostic") if failure == "timeout" else RuntimeError("private-diagnostic")
    gateway._bridge_call_inner = AsyncMock(side_effect=[
        exception, {"state": "done", "answer": "Die fertige Antwort."},
    ])
    try:
        response = await dispatch(gateway, "GetCodexAnswerIntent", "failed-fetch")
        assert speech(response) == RETRIEVAL_FAILURE_TEXT
        assert speech(response).startswith("Die Antwort konnte")
        assert gateway.last_jobs[principal] is job
        assert (principal in gateway.pending) is pending
        assert not gateway.pages.available(principal)

        response = await dispatch(gateway, "GetCodexAnswerIntent", "later-fetch")
        assert speech(response) == "Die fertige Antwort."
        assert principal not in gateway.pending
        assert gateway.last_jobs[principal] is job
        assert gateway.pages.answers[principal].job_id == job.job_id
        assert [call.args for call in gateway._bridge_call_inner.await_args_list] == [
            ("/v1/result", {"principal": principal, "job_id": job.job_id}),
            ("/v1/result", {"principal": principal, "job_id": job.job_id}),
        ]
    finally:
        await gateway.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "unavailable"])
async def test_initial_poll_failure_keeps_submitted_job_for_later_answer(failure):
    gateway = LumiRelay(None, settings())
    principal = gateway._principal_inputs(event())[0]
    exception = TimeoutError("private-diagnostic") if failure == "timeout" else RuntimeError("private-diagnostic")
    gateway._bridge_call_inner = AsyncMock(side_effect=[
        {"state": "pending", "job_id": "b" * 32}, exception,
        {"state": "done", "answer": "Antwort desselben Auftrags."},
    ])
    try:
        response = await dispatch(gateway, "AskChatGPTIntent", "ask")
        assert speech(response) == RETRIEVAL_FAILURE_TEXT
        job = gateway.last_jobs[principal]
        assert job.job_id == "b" * 32
        # Releasing the automatic delivery slot clears pending, not last_jobs.
        assert principal not in gateway.pending
        assert DEVICE not in gateway.answer_delivery.slots
        assert not gateway.pages.available(principal)

        response = await dispatch(gateway, "GetCodexAnswerIntent", "retrieve")
        assert speech(response) == "Antwort desselben Auftrags."
        calls = gateway._bridge_call_inner.await_args_list
        assert [call.args[0] for call in calls] == ["/v1/submit", "/v1/result", "/v1/result"]
        assert all(call.args[1] == {"principal": principal, "job_id": job.job_id} for call in calls[1:])
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_submission_timeout_reports_uncertain_acceptance_without_repeat_advice():
    gateway = LumiRelay(None, settings())
    principal = gateway._principal_inputs(event())[0]
    old_job = PendingJob("c" * 32, time.monotonic() + 60)
    gateway.last_jobs[principal] = old_job
    gateway.pages.first(principal, old_job, "Alte private Antwort.")
    gateway._bridge_call_inner = AsyncMock(side_effect=TimeoutError("private-diagnostic"))
    try:
        response = await dispatch(gateway, "AskChatGPTIntent", "ask")
        assert speech(response) == SUBMISSION_TIMEOUT_TEXT
        assert speech(response).startswith("Der Gesprächsdienst")
        assert "vorerst nicht" in speech(response)
        assert "erneut" not in speech(response)
        assert principal not in gateway.last_jobs and principal not in gateway.pending
        assert not gateway.pages.available(principal)
        assert DEVICE not in gateway.answer_delivery.slots
        gateway._bridge_call_inner.assert_awaited_once()
        assert gateway._bridge_call_inner.await_args.args[0] == "/v1/submit"
    finally:
        await gateway.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["busy", "unauthenticated", "unavailable"])
async def test_non_timeout_submission_failures_keep_existing_messages(code):
    gateway = LumiRelay(None, settings())
    gateway._bridge_call = AsyncMock(side_effect=LumiBridgeFailure(code))
    try:
        assert speech(await dispatch(gateway, "AskChatGPTIntent", "ask")) == ERROR_MESSAGES[code]
        gateway._bridge_call.assert_awaited_once()
    finally:
        await gateway.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["busy", "unauthenticated"])
async def test_known_job_specific_transport_failures_keep_existing_messages(code):
    gateway = LumiRelay(None, settings())
    principal = gateway._principal_inputs(event())[0]
    job = PendingJob("d" * 32, time.monotonic() + 60)
    gateway.last_jobs[principal] = job
    gateway._bridge_call = AsyncMock(side_effect=LumiBridgeFailure(code))
    try:
        assert speech(await dispatch(gateway, "GetCodexAnswerIntent", "retrieve")) == ERROR_MESSAGES[code]
        assert gateway.last_jobs[principal] is job
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_terminal_backend_timeout_keeps_terminal_message():
    gateway = LumiRelay(None, settings())
    principal = gateway._principal_inputs(event())[0]
    gateway._bridge_call_inner = AsyncMock(side_effect=[
        {"state": "failed", "job_id": "e" * 32},
        {"state": "failed", "error": "timeout"},
        {"state": "failed", "error": "timeout"},
    ])
    try:
        assert speech(await dispatch(gateway, "AskChatGPTIntent", "ask")) == ERROR_MESSAGES["timeout"]
        assert principal not in gateway.pending
        assert speech(await dispatch(gateway, "GetCodexAnswerIntent", "retrieve")) == ERROR_MESSAGES["timeout"]
        assert [call.args[0] for call in gateway._bridge_call_inner.await_args_list].count("/v1/submit") == 1
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_recovery_preserves_page_session_and_stop_isolation():
    gateway = LumiRelay(None, settings())
    principal = gateway._principal_inputs(event())[0]
    gateway._bridge_call_inner = AsyncMock(side_effect=[
        {"state": "pending", "job_id": "f" * 32}, TimeoutError("private-diagnostic"),
        {"state": "done", "answer": "Erholte Antwort. " * 200},
    ])
    try:
        assert speech(await dispatch(gateway, "AskChatGPTIntent", "ask")) == RETRIEVAL_FAILURE_TEXT
        recovered = speech(await dispatch(gateway, "GetCodexAnswerIntent", "retrieve"))
        assert recovered.startswith("Erholte Antwort.")
        assert gateway.pages.answers[principal].index == 0
        assert speech(await dispatch(gateway, "AMAZON.RepeatIntent", "repeat")) == recovered
        foreign = event("GetCodexAnswerIntent")
        foreign["session"]["sessionId"] = "different-session"
        foreign["request"]["requestId"] = "foreign"
        foreign_principal = gateway._principal_inputs(foreign)[0]
        response = await gateway._dispatch(foreign["request"], foreign, foreign_principal)
        assert "Erholte Antwort" not in speech(response)
        await dispatch(gateway, "AMAZON.StopIntent", "stop")
        assert not gateway.pages.available(principal)
        assert principal not in gateway.last_jobs and principal not in gateway.pending
        assert "keine fertige Antwort" in speech(await dispatch(gateway, "GetCodexAnswerIntent", "after-stop"))
        assert gateway._bridge_call_inner.await_count == 3
    finally:
        await gateway.close()
