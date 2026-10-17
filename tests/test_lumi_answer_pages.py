"""Synthetic page, identity, cancellation, replay and safe failure contracts."""
import asyncio
import json
from types import SimpleNamespace
import time
from unittest.mock import AsyncMock

import pytest

from music_alexa.answer_pages import (
    AnswerPages, CONTINUE_HINT, ERROR_MESSAGES, MAX_ANSWERS, MAX_REQUESTS,
    PENDING_TEXT, split_answer,
)
from music_alexa.answer_delivery import bounded_speech
from music_alexa.voice_gateway import PendingJob, BridgeFailure
from music_alexa.lumi_relay import LumiRelay
from codex_voice.voice_bridge import VoiceJobs, _json_bytes
from test_lumi_relay import event, settings, DEVICE
from test_lumi_answer_delivery import setup_gateway, ask


def job(name="a", expires=None):
    return PendingJob(name * 32, time.monotonic() + 600 if expires is None else expires)


@pytest.mark.parametrize("length", [1, 1999, 2000, 2001, 3999, 6000])
def test_unpunctuated_answer_is_preserved_with_bounded_pages(length):
    answer = "x" * length
    pages = split_answer(answer)
    bodies = [page.removesuffix(" " + CONTINUE_HINT) for page in pages]
    assert "".join(bodies) == answer
    assert all(0 < len(page) <= 2000 for page in pages)
    assert all(page.endswith(CONTINUE_HINT) for page in pages[:-1])
    assert not pages[-1].endswith(CONTINUE_HINT)


def test_sentences_words_and_unicode_survive_paging():
    text = "Ein vollständiger Satz. " * 220
    pages = split_answer(text)
    bodies = [page.removesuffix(" " + CONTINUE_HINT) for page in pages]
    assert " ".join(bodies).split() == text.split()
    assert len(pages) > 1


@pytest.mark.parametrize("text", [None, 37, "", " ", "x" * 6001])
def test_invalid_full_answers_are_rejected(text):
    with pytest.raises(ValueError):
        split_answer(text)


def test_final_speech_boundary_never_silently_discards_remainder():
    assert bounded_speech("x" * 2000) == "x" * 2000
    with pytest.raises(ValueError, match="limit"):
        bounded_speech("x" * 2001)


@pytest.mark.parametrize("character", ["ä", "\U0001f600"])
def test_full_unicode_answer_fits_existing_signed_wire_budget(character):
    answer = character * 6000
    raw = _json_bytes({"state": "done", "answer": answer})
    assert len(raw) <= 32768
    assert json.loads(raw)["answer"] == answer
    assert character.encode("utf-8") in raw


def test_next_repeat_replay_and_new_job_are_distinct():
    pages = AnswerPages()
    first = pages.first("person-a", job(), "x" * 6000)
    second = pages.select("person-a", "next-1")
    assert pages.answers["person-a"].index == 1
    assert pages.select("person-a", "repeat-1", repeat=True) == second
    pages.select("person-a", "next-2")
    assert pages.answers["person-a"].index == 2
    assert pages.select("person-a", "next-1") == second
    assert pages.answers["person-a"].index == 2
    with pytest.raises(ValueError, match="mismatch"):
        pages.select("person-a", "next-1", repeat=True)
    pages.first("person-a", job("b"), "new answer")
    assert pages.select("person-a", "next-1", repeat=True) == "new answer"


def test_pages_expire_and_never_cross_principal_boundary():
    now = [10.0]
    pages = AnswerPages(clock=lambda: now[0])
    pages.first("session-a", job(expires=20), "x" * 3000)
    with pytest.raises(ValueError, match="unavailable"):
        pages.select("session-b", "req")
    now[0] = 20
    assert not pages.available("session-a")
    with pytest.raises(ValueError, match="Expired"):
        pages.first("session-a", job(expires=20), "answer")


def test_answer_and_replay_maps_are_bounded():
    pages = AnswerPages()
    for index in range(MAX_ANSWERS):
        pages.first(str(index), job(), "answer")
    with pytest.raises(ValueError, match="capacity"):
        pages.first("overflow", job(), "answer")
    for index in range(MAX_REQUESTS):
        pages.select("0", str(index), repeat=True)
    with pytest.raises(ValueError, match="capacity"):
        pages.select("0", "overflow")
    assert pages.select("0", "0", repeat=True) == "answer"


@pytest.mark.asyncio
async def test_fast_long_answer_next_and_repeat_do_not_resubmit_or_duplicate_cursor(monkeypatch):
    gateway, _, calls, spoken = setup_gateway(monkeypatch)
    count = 0
    async def bridge(path, value):
        nonlocal count
        if path == "/v1/submit":
            count += 1
            return {"job_id": "a" * 32, "state": "done"}
        return {"state": "done", "answer": "x" * 6000}
    gateway._bridge_call = bridge
    first = await ask(gateway)
    principal = gateway._principal_inputs(event())[0]
    assert first["response"]["outputSpeech"]["text"].endswith(CONTINUE_HINT)
    assert first["response"]["shouldEndSession"] is False
    request = event("GetCodexAnswerIntent")
    request["request"]["requestId"] = "next"
    one = await ask(gateway, request)
    assert gateway.pages.answers[principal].index == 1
    assert await ask(gateway, request) == one
    assert gateway.pages.answers[principal].index == 1
    repeated = event("AMAZON.RepeatIntent")
    repeated["request"]["requestId"] = "repeat"
    assert await ask(gateway, repeated) == one
    assert count == 1 and not spoken
    await gateway.close()


@pytest.mark.asyncio
async def test_automatic_delivery_uses_first_page_and_retains_the_rest(monkeypatch):
    gateway, completed, _, spoken = setup_gateway(monkeypatch)
    async def bridge(path, value):
        if path == "/v1/submit":
            return {"state": "pending", "job_id": "a" * 32}
        return {"state": "done", "answer": "x" * 6000} if completed.is_set() else {"state": "pending"}
    gateway._bridge_call = bridge
    await ask(gateway)
    slot = gateway.answer_delivery.slots[DEVICE]
    completed.set()
    await slot.task
    assert len(spoken) == 1
    assert len(spoken[0][2]) <= 2000 and spoken[0][2].endswith(CONTINUE_HINT)
    assert len(gateway.pages.answers[slot.principal].pages) > 1
    assert gateway.pages.answers[slot.principal].presented is True
    await gateway.close()


@pytest.mark.asyncio
async def test_failed_automatic_speech_does_not_skip_first_page_or_retry(monkeypatch):
    from music_alexa.answer_delivery import SpeechClient
    gateway, completed, _, _ = setup_gateway(monkeypatch)
    async def bridge(path, value):
        if path == "/v1/submit":
            return {"state": "pending", "job_id": "a" * 32}
        return {"state": "done", "answer": "x" * 6000} if completed.is_set() else {"state": "pending"}
    gateway._bridge_call = bridge
    speak = AsyncMock(side_effect=TimeoutError("uncertain transport"))
    monkeypatch.setattr(SpeechClient, "speak", speak)
    await ask(gateway)
    slot = gateway.answer_delivery.slots[DEVICE]
    completed.set()
    await slot.task
    answer = gateway.pages.answers[slot.principal]
    assert answer.presented is False
    request = event("GetCodexAnswerIntent")
    request["request"]["requestId"] = "manual-first"
    response = await ask(gateway, request)
    assert response["response"]["outputSpeech"]["text"] == answer.pages[0]
    assert answer.index == 0 and answer.presented is True
    speak.assert_awaited_once()
    await gateway.close()


@pytest.mark.asyncio
async def test_pages_never_cross_recognized_person_boundary(monkeypatch):
    gateway, _, _, _ = setup_gateway(monkeypatch)
    payload = event()
    payload["context"]["System"]["person"] = {"personId": "person-a"}
    principal = gateway._principal_inputs(payload)[0]
    gateway.pages.first(principal, job(), "private old answer " * 160)
    foreign = event("GetCodexAnswerIntent")
    foreign["context"]["System"]["person"] = {"personId": "person-b"}
    foreign["request"]["requestId"] = "foreign-person"
    assert gateway._principal_inputs(foreign)[0] != principal
    response = await ask(gateway, foreign)
    assert "keine fertige Antwort" in response["response"]["outputSpeech"]["text"]
    assert gateway.pages.answers[principal].index == 0
    await gateway.close()


@pytest.mark.asyncio
async def test_result_after_cancellation_cannot_restore_pages(monkeypatch):
    gateway, _, _, _ = setup_gateway(monkeypatch)
    principal = gateway._principal_inputs(event())[0]
    old_job = job()
    gateway.last_jobs[principal] = old_job
    entered, release = asyncio.Event(), asyncio.Event()
    async def delayed(path, value):
        entered.set()
        await release.wait()
        return {"state": "done", "answer": "old private answer"}
    gateway._bridge_call = delayed
    task = asyncio.create_task(gateway._poll_exact(principal, old_job))
    await entered.wait()
    gateway._clear_answer_pages(principal)
    gateway.last_jobs.pop(principal)
    release.set()
    assert await task is None
    assert not gateway.pages.available(principal)
    await gateway.close()


@pytest.mark.asyncio
async def test_pending_does_not_claim_missing_speech_configuration(monkeypatch):
    gateway, _, _, _ = setup_gateway(monkeypatch)
    principal = gateway._principal_inputs(event())[0]
    gateway.pending[principal] = job()
    gateway.last_jobs[principal] = gateway.pending[principal]
    gateway._bridge_call = AsyncMock(return_value={"state": "pending"})
    response = await gateway._get_answer(principal, request_id="poll")
    assert response["response"]["outputSpeech"]["text"] == PENDING_TEXT
    assert response["response"]["shouldEndSession"] is False
    await gateway.close()


@pytest.mark.asyncio
async def test_stop_new_question_and_new_anonymous_session_cannot_reveal_old_pages(monkeypatch):
    gateway, completed, _, _ = setup_gateway(monkeypatch)
    initial = event()
    principal = gateway._principal_inputs(initial)[0]
    gateway.pages.first(principal, job(), "old private answer " * 150)
    foreign = event("GetCodexAnswerIntent")
    foreign["session"]["sessionId"] = "different-anonymous-session"
    foreign["request"]["requestId"] = "foreign"
    assert gateway._principal_inputs(foreign)[0] != principal
    response = await ask(gateway, foreign)
    assert "old private answer" not in response["response"]["outputSpeech"]["text"]
    stop = event("AMAZON.StopIntent")
    stop["request"]["requestId"] = "stop"
    await ask(gateway, stop)
    assert not gateway.pages.available(principal)
    gateway.pages.first(principal, job(), "old private answer " * 150)
    completed.set()
    await ask(gateway)
    assert "old private answer" not in gateway.pages.answers[principal].pages[0]
    await gateway.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("code", sorted(ERROR_MESSAGES))
async def test_job_error_codes_map_only_to_fixed_speech(monkeypatch, code):
    gateway, _, _, _ = setup_gateway(monkeypatch)
    gateway._bridge_call = AsyncMock(return_value={"state": "failed", "error": code})
    text = await gateway._poll_exact("principal", job())
    assert text == ERROR_MESSAGES[code]
    await gateway.close()


@pytest.mark.asyncio
async def test_unknown_error_and_stale_result_cannot_become_spoken_text(monkeypatch):
    gateway, _, _, _ = setup_gateway(monkeypatch)
    gateway._bridge_call = AsyncMock(return_value={"state": "failed", "error": "private-diagnostic"})
    with pytest.raises(BridgeFailure):
        await gateway._poll_exact("principal", job())
    gateway._bridge_call = AsyncMock(return_value={"state": "done", "answer": "stale answer"})
    assert await gateway._poll_exact("principal", job()) is None
    assert not gateway.pages.available("principal")
    await gateway.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["AMAZON.StopIntent", "AskChatGPTIntent"])
@pytest.mark.parametrize("pages_expired", [False, True])
async def test_cached_retrieval_is_retired_after_stop_or_new_question(monkeypatch, action, pages_expired):
    gateway, _, _, _ = setup_gateway(monkeypatch)
    submissions = []
    async def bridge(path, value):
        if path == "/v1/submit":
            submissions.append(value)
            return {"state": "done", "job_id": ("a" if len(submissions) == 1 else "b") * 32}
        return {"state": "done", "answer": ("a" if len(submissions) == 1 else "b") * 6000}
    gateway._bridge_call = bridge
    async def verified(payload):
        principal, device, user = gateway._principal_inputs(payload)
        response = await gateway._process_verified(payload, json.dumps(payload).encode(), principal, device, user)
        return json.loads(response.body)
    await verified(event())
    retrieve = event("GetCodexAnswerIntent")
    retrieve["request"]["requestId"] = "retrieve-page"
    initial = await verified(retrieve)
    principal = gateway._principal_inputs(retrieve)[0]
    assert gateway.pages.answers[principal].index == 1
    assert await verified(retrieve) == initial
    cached = gateway.replies[(principal, "retrieve-page")]
    if pages_expired:
        gateway.pages.answers[principal].expires = time.monotonic() - 1
        gateway.pages.expire()
    stop_or_new = event(action)
    stop_or_new["request"]["requestId"] = "stop-or-new"
    await verified(stop_or_new)
    assert gateway.replies[(principal, "retrieve-page")][:2] == cached[:2]
    replay = await verified(retrieve)
    assert replay["response"]["outputSpeech"]["text"] == "Die Anfrage wurde abgebrochen."
    if action == "AskChatGPTIntent":
        assert gateway.pages.answers[principal].index == 0
        assert len(submissions) == 2
    else:
        assert not gateway.pages.available(principal)
        assert len(submissions) == 1
    await gateway.close()


@pytest.mark.asyncio
async def test_timeout_transport_is_translated_without_private_diagnostics(monkeypatch):
    gateway, _, _, _ = setup_gateway(monkeypatch)
    # Use the actual wrapper instead of the test helper's bridge substitute.
    del gateway._bridge_call
    gateway._bridge_call_inner = AsyncMock(side_effect=TimeoutError("private-url"))
    with pytest.raises(BridgeFailure) as caught:
        await gateway._bridge_call("/v1/result", {})
    assert caught.value.code == "timeout"
    assert "private" not in str(caught.value)
    await gateway.close()


@pytest.mark.asyncio
async def test_failed_new_submission_cannot_resurrect_previous_answer(monkeypatch):
    gateway, completed, _, _ = setup_gateway(monkeypatch)
    completed.set()
    await ask(gateway)
    principal = gateway._principal_inputs(event())[0]
    assert principal in gateway.last_jobs and gateway.pages.available(principal)
    gateway._bridge_call = AsyncMock(side_effect=BridgeFailure("private diagnostic"))
    fresh = event()
    fresh["request"]["requestId"] = "new-question"
    await ask(gateway, fresh)
    assert principal not in gateway.last_jobs and principal not in gateway.pending
    result = await gateway._get_answer(principal, request_id="retrieve")
    assert "keine fertige Antwort" in result["response"]["outputSpeech"]["text"]
    gateway._bridge_call.assert_awaited_once()
    await gateway.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_signature,extra_field", [(False, False), (True, False), (False, True)])
async def test_only_authenticated_exact_busy_response_has_busy_meaning(monkeypatch, bad_signature, extra_field):
    gateway, _, _, _ = setup_gateway(monkeypatch)
    del gateway._bridge_call
    document = {"error": "busy", **({"extra": "private detail"} if extra_field else {})}
    raw = json.dumps(document).encode()
    class Content:
        async def iter_chunked(self, size):
            yield raw
    class Response:
        status = 429
        content = Content()
        def __init__(self, nonce):
            signature = gateway._response_signature("/v1/submit", nonce, 429, raw)
            self.headers = {"X-Voice-Response-Signature": "0" * 64 if bad_signature else signature}
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    class Session:
        def post(self, url, *, headers, **kwargs):
            return Response(headers["X-Voice-Nonce"])
    gateway.session = Session()
    with pytest.raises(BridgeFailure) as caught:
        await gateway._bridge_call("/v1/submit", {})
    assert caught.value.code == ("unavailable" if bad_signature or extra_field else "busy")
    assert "private" not in str(caught.value)
    await gateway.close()


@pytest.mark.parametrize("error,expected", [(TimeoutError("private-network"), "timeout"),
    (RuntimeError("timeout"), "timeout"), (RuntimeError("busy"), "busy"),
    (ValueError("private-diagnostic"), "unavailable"), (None, "unauthenticated")])
def test_voice_jobs_return_safe_error_codes_without_private_diagnostics(error, expected, capsys):
    class Runtime:
        def authenticated(self): return error is not None
        def query(self, question): raise error
        def query_lumi(self, question, *, memory): raise error
    jobs = VoiceJobs(Runtime())
    try:
        status, submitted = jobs.submit("1" * 64, "synthetic", "question",
                                        memory={"person_key": "", "session_key": "2" * 64})
        assert status == 202
        with jobs._condition:
            assert jobs._condition.wait_for(
                lambda: jobs._records[submitted["job_id"]]["state"] == "failed", timeout=2)
        status, result = jobs.result("1" * 64, submitted["job_id"])
        assert status == 200 and result == {"state": "failed", "error": expected}
        assert "private" not in capsys.readouterr().out
    finally:
        jobs.close()
