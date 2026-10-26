"""Offline acceptance boundaries for the dedicated Lumi greeting operation."""
from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import threading

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from codex_voice.app import CodexRuntime
from codex_voice import app as runtime_module
from codex_voice.lumi_assistant import GREETING_FALLBACK, LumiSettings, spoken_greeting
from codex_voice.lumi_context import ContextStore
from codex_voice.voice_bridge import VoiceJobs, make_server
from music_alexa import voice_gateway as gateway_module
from music_alexa.lumi_relay import LumiRelay, mac, validate_event
from music_alexa.security import Rejected
from test_lumi_memory_wire import (settings, event, relay_signature, expected_memory_key, bridge_submit,
    SKILL, USER, DEVICE, INTERNAL_SECRET, RELAY_SECRET, PRINCIPAL)

OWNER = 'a' * 64
SESSION = 'c' * 64
MEMORY = {'person_key': OWNER, 'session_key': SESSION}


def launch(**kwargs):
    value = event(**kwargs)
    value['request'] = {'type': 'LaunchRequest', 'requestId': 'launch-1'}
    return value


def runtime(tmp_path):
    result = CodexRuntime()
    result._lumi_settings = LumiSettings(True, frozenset({OWNER}))
    result._lumi_context = ContextStore(tmp_path / 'history.sqlite3')
    result._lumi_context.remember('person', OWNER, 'Ein früheres Gespräch.', 'Ein bekannter Stand.')
    return result


def test_greeting_uses_only_approved_history_without_question_storage_or_notion(tmp_path):
    instance = runtime(tmp_path)
    instance._lumi_context.remember('person', OWNER, 'Der Filter ist nun blau.', 'Das halte ich fest.')
    before = instance._lumi_context.recent('person', OWNER)
    seen = []
    instance.query = lambda question, **kwargs: seen.append((question, kwargs)) or 'Schön, dich zu hören. Das Blau steht dem Tag.'
    class NoKnowledge:
        def search(self, *_args, **_kwargs):
            pytest.fail('Greeting must not search Notion')
    instance._lumi_knowledge = NoKnowledge()
    answer = instance.greet_lumi(memory=MEMORY)
    assert 'Blau' in answer
    assert seen[0][0] == ''
    assert 'Der Filter ist nun blau.' in seen[0][1]['_conversation_prompt']
    assert '"kind": "greeting"' in seen[0][1]['_conversation_prompt']
    assert '_control_prompt' not in seen[0][1]
    assert instance._lumi_context.recent('person', OWNER) == before


def test_foreign_person_gets_only_same_session_and_control_never_called(tmp_path):
    instance = runtime(tmp_path)
    instance._lumi_context.remember('person', OWNER, 'Private Owner details', 'Private Owner answer')
    instance._lumi_context.remember('session', SESSION, 'Current-session detail', 'Current-session answer')
    prompts = []
    instance.query = lambda _question, **kwargs: prompts.append(kwargs['_conversation_prompt']) or 'Gut, dich zu hören.'
    instance.greet_lumi(memory={'person_key': 'b' * 64, 'session_key': SESSION})
    assert 'Private Owner' not in prompts[0]
    assert 'Current-session detail' in prompts[0]
    prompts.clear()
    assert instance.greet_lumi(memory={'person_key': 'b' * 64, 'session_key': 'd' * 64}) == GREETING_FALLBACK
    assert prompts == []
    instance._control_client = object()
    instance.query = lambda _question, **kwargs: prompts.append(kwargs) or 'Gut, dich zu hören.'
    assert instance.greet_lumi(memory=MEMORY) == 'Gut, dich zu hören.'
    assert '_control_prompt' not in prompts[-1]


@pytest.mark.parametrize('invalid', [None, '', 'Was möchtest du wissen?', 'Sage Antwort.', 'Ein Satz. Zwei Sätze. Noch einer.', 'x' * 181])
def test_generated_greeting_rejects_questions_commands_and_overlong_output(invalid):
    assert spoken_greeting(invalid) == GREETING_FALLBACK


def test_greeting_runtime_failure_uses_neutral_fallback(tmp_path):
    instance = runtime(tmp_path)
    def fail(*_args, **_kwargs):
        raise RuntimeError('synthetic private failure detail')
    instance.query = fail
    assert instance.greet_lumi(memory=MEMORY) == GREETING_FALLBACK


def test_greeting_does_not_wait_behind_an_active_question(tmp_path):
    instance = runtime(tmp_path)
    instance._query_slot.acquire()
    instance._spawn = lambda *_args, **_kwargs: pytest.fail('Busy greeting must not spawn a model')
    try:
        assert instance.greet_lumi(memory=MEMORY) == GREETING_FALLBACK
        assert not instance._query_slot.acquire(blocking=False)
    finally:
        instance._query_slot.release()


def test_short_query_deadline_reaches_process_wait_and_terminates_without_model(monkeypatch):
    instance = CodexRuntime()
    instance._new_log_dir = lambda: None
    calls = []
    class HungProcess:
        stdin = io.BytesIO()
        stdout = io.BytesIO()
        stderr = io.BytesIO()
        def wait(self, timeout=None):
            calls.append(timeout)
            raise runtime_module.subprocess.TimeoutExpired('offline-placeholder', timeout)
    process = HungProcess()
    instance._spawn = lambda *_args, **_kwargs: process
    terminated = []
    monkeypatch.setattr(runtime_module, '_terminate_process', lambda value: terminated.append(value))
    with pytest.raises(runtime_module.RuntimeErrorSafe, match='timeout'):
        instance.query('', _conversation_prompt='Synthetic greeting context', _timeout_seconds=0.03)
    assert 0 <= calls[0] <= 0.03 + 1e-9
    assert process in terminated
    assert instance._query_slot.acquire(blocking=False)
    instance._query_slot.release()


def test_normal_query_retains_execution_budget_after_another_query_finishes(monkeypatch):
    instance = CodexRuntime()
    instance._new_log_dir = lambda: None
    clock = {'now': 1000.0}
    monkeypatch.setattr(runtime_module.time, 'monotonic', lambda: clock['now'])

    class OccupiedSlot:
        waiting = True
        acquired = False
        released = False
        def acquire(self, blocking=False):
            if self.waiting or self.acquired:
                return False
            self.acquired = True
            return True
        def release(self):
            self.acquired = False
            self.released = True

    slot = OccupiedSlot()
    instance._query_slot = slot
    def preceding_query_finishes(_wait):
        clock['now'] += 60.0
        slot.waiting = False
    monkeypatch.setattr(runtime_module.time, 'sleep', preceding_query_finishes)
    budgets = []
    class HungProcess:
        stdin = io.BytesIO()
        stdout = io.BytesIO()
        stderr = io.BytesIO()
        def wait(self, timeout=None):
            budgets.append(timeout)
            raise runtime_module.subprocess.TimeoutExpired('offline-placeholder', timeout)
    instance._spawn = lambda *_args, **_kwargs: HungProcess()
    monkeypatch.setattr(runtime_module, '_terminate_process', lambda _process: None)
    with pytest.raises(runtime_module.RuntimeErrorSafe, match='timeout'):
        instance.query('Synthetic normal conversation', wait_for_slot=True)
    assert clock['now'] == 1060.0
    assert budgets == [runtime_module.QUERY_TIMEOUT_SECONDS]
    assert slot.released


class GreetingRuntime:
    def __init__(self, *, block=False):
        self.calls = []
        self.release = threading.Event()
        if not block:
            self.release.set()
    def authenticated(self):
        return True
    def greet_lumi(self, *, memory):
        self.calls.append(memory)
        self.release.wait(3)
        return 'Gut, dich zu hören. Der blaue Filter bleibt im Gedächtnis.'
    def query(self, *_args, **_kwargs):
        pytest.fail('Greeting must not become an invented question')
    query_lumi = query
    query_voice = query


@pytest.mark.asyncio
async def test_signed_launch_is_immediate_local_and_does_not_replace_question(monkeypatch):
    instance = GreetingRuntime()
    bridge = make_server(instance, INTERNAL_SECRET, host='127.0.0.1', port=0)
    thread = threading.Thread(target=bridge.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(gateway_module, 'INTERNAL_ORIGIN', f'http://127.0.0.1:{bridge.server_port}')
    try:
        async with aiohttp.ClientSession() as session:
            relay = LumiRelay(session, settings())
            app = web.Application()
            app.router.add_post('/lumi/voice', relay.handle)
            async with TestClient(TestServer(app)) as client:
                value = launch(person_id='verified-person-a')
                body, headers = relay_signature(value)
                response = await client.post('/lumi/voice', data=body, headers=headers)
                raw = await response.read()
                document = json.loads(raw)
                assert response.status == 200
                assert response.headers['X-Lumi-Response-Signature'] == mac(RELAY_SECRET, 'RESPONSE', headers['X-Lumi-Nonce'], 200, raw)
                text = document['response']['outputSpeech']['text']
                assert 0 < len(text) <= 180
                assert '?' not in text and 'Filter' not in text
                assert document['response']['shouldEndSession'] is False
                assert 'reprompt' not in document['response']
                _, repeat_headers = relay_signature(value, nonce='2' * 32)
                retry = await client.post('/lumi/voice', data=body, headers=repeat_headers)
                assert await retry.json() == document
                value['request']['requestId'] = 'launch-2'
                fresh_body, fresh_headers = relay_signature(value, nonce='3' * 32)
                fresh = await client.post('/lumi/voice', data=fresh_body, headers=fresh_headers)
                assert (await fresh.json())['response']['outputSpeech']['text'] != text
                assert relay.pending == {} and relay.last_jobs == {}
        assert instance.calls == []
    finally:
        instance.release.set()
        bridge.shutdown()
        bridge.server_close()
        thread.join(timeout=1)


@pytest.mark.asyncio
async def test_launch_never_starts_a_model_job_even_when_bridge_is_unavailable():
    relay = LumiRelay(None, settings())
    calls = []
    async def bridge(path, value):
        calls.append((path, value))
        return {'job_id': 'e' * 32, 'state': 'pending'} if path == '/v1/submit' else {'state': 'pending'}
    relay._bridge_call = bridge
    relay._submit_poll_seconds = 0
    assert relay.submit_poll_seconds == 0
    value = launch()
    result = await relay._dispatch(value['request'], value, PRINCIPAL)
    assert 0 < len(result['response']['outputSpeech']['text']) <= 180
    assert 'reprompt' not in result['response']
    assert calls == []
    assert relay.pending == {} and relay.last_jobs == {}
    async def failure(*_args):
        raise gateway_module.BridgeFailure('synthetic detail')
    relay._bridge_call = failure
    assert await relay._dispatch(value['request'], value, PRINCIPAL) == result


def test_greeting_queue_dedupe_binds_kind_and_memory_and_missing_memory_fails():
    instance = GreetingRuntime(block=True)
    queue = VoiceJobs(instance)
    try:
        status, first = queue.submit(PRINCIPAL, 'request', memory=MEMORY, kind='greeting')
        assert status == 202
        assert queue.submit(PRINCIPAL, 'request', memory=MEMORY, kind='greeting')[1]['job_id'] == first['job_id']
        assert queue.submit(PRINCIPAL, 'request', memory={**MEMORY, 'session_key': 'f' * 64}, kind='greeting')[0] == 409
        assert queue.submit(PRINCIPAL, 'request', 'An actual question', memory=MEMORY)[0] == 409
        assert queue.submit(PRINCIPAL, 'bad', kind='greeting')[0] == 400
        assert queue.submit(PRINCIPAL, 'bad', 'Invented question', memory=MEMORY, kind='greeting')[0] == 400
    finally:
        instance.release.set()
        queue.close()


def test_launch_shape_has_no_intent_and_session_person_does_not_approve_memory():
    value = launch()
    validate_event(value, SKILL)
    value['session']['person'] = {'personId': 'untrusted-session-only-person'}
    relay = LumiRelay(None, settings())
    assert relay._memory_value(value)['person_key'] == ''
    value['request']['intent'] = {'name': 'AskChatGPTIntent'}
    with pytest.raises(Rejected):
        validate_event(value, SKILL)


def test_authenticated_bridge_rejects_greeting_without_memory_or_with_a_question():
    instance = GreetingRuntime()
    bridge = make_server(instance, INTERNAL_SECRET, host='127.0.0.1', port=0)
    thread = threading.Thread(target=bridge.serve_forever, daemon=True)
    thread.start()
    base = {'principal': PRINCIPAL, 'request_id': 'launch-http', 'kind': 'greeting', 'memory': MEMORY}
    try:
        invalids = [{key: value for key, value in base.items() if key != 'memory'},
                    {**base, 'question': 'Invented question'}, {**base, 'kind': 'other'},
                    {**base, 'memory': None}, {**base, 'memory': {'person_key': OWNER}}]
        for number, value in enumerate(invalids):
            assert bridge_submit(bridge.server_port, value, f'{number:032x}') == 400
        assert instance.calls == []
    finally:
        bridge.shutdown()
        bridge.server_close()
        thread.join(timeout=1)


def load_lambda():
    path = Path(__file__).parents[1] / 'deployment/lumi_chat_lambda/lambda_function.py'
    if not path.is_file():
        pytest.skip('Lambda is a separate SmartHome repository; its live-package tests run there')
    spec = importlib.util.spec_from_file_location('lumi_greeting_candidate_lambda', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_lambda_launch_slim_contract_and_signed_dynamic_reply(monkeypatch):
    module = load_lambda()
    monkeypatch.setenv('EXPECTED_ALEXA_SKILL_ID', SKILL)
    monkeypatch.setenv('LUMI_CONVERSATION_BACKEND', 'codex')
    monkeypatch.setenv('LUMI_CODEX_RELAY_URL', 'https://example.invalid/lumi/voice')
    monkeypatch.setenv('LUMI_CODEX_RELAY_SECRET', RELAY_SECRET)
    monkeypatch.delenv('LUMI_PAIRING_DIAGNOSTIC_UNTIL', raising=False)
    monkeypatch.setattr(module, '_read_openai_api_key', lambda *_: pytest.fail('No API key read on launch'))
    value = launch(person_id='verified-person-a')
    value['context']['System']['user']['accessToken'] = 'synthetic-token-must-be-removed'
    seen = []
    class Reply:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False
        def geturl(self):
            return 'https://example.invalid/lumi/voice'
        def read(self, _limit):
            return self.raw
    def relay_open(request, *, timeout):
        outgoing = json.loads(request.data)
        assert outgoing['request'] == {'type': 'LaunchRequest', 'requestId': 'launch-1'}
        assert outgoing['session']['sessionId'] == 'session-a'
        assert 'synthetic-token' not in request.data.decode()
        seen.append(outgoing)
        reply = Reply()
        reply.raw = json.dumps({'version':'1.0','response':{'outputSpeech':{'type':'PlainText','text':'Schön, dich zu hören. Der Filter hat jetzt Blau.'},'shouldEndSession':False}}).encode()
        nonce = request.get_header('X-lumi-nonce')
        reply.headers = {'Content-Type':'application/json','X-Lumi-Response-Signature':mac(RELAY_SECRET,'RESPONSE',nonce,200,reply.raw)}
        return reply
    monkeypatch.setattr(module, '_relay_open', relay_open)
    from smarthome.alexa_conversation import handle_alexa_conversation
    expected_fallback = handle_alexa_conversation(value, expected_application_id=SKILL,
        responder=None).to_alexa_response()
    response = module.lambda_handler(value, None)
    assert response['response']['outputSpeech']['text'].startswith('Schön, dich zu hören.')
    assert 'reprompt' not in response['response']
    assert len(seen) == 1
    monkeypatch.setattr(module, '_relay_open', lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError('synthetic')))
    assert module.lambda_handler(value, None) == expected_fallback
    monkeypatch.setenv('LUMI_CONVERSATION_BACKEND', 'openai')
    monkeypatch.setattr(module, '_relay_open', lambda *_args, **_kwargs: pytest.fail('Non-Codex launch must remain local'))
    assert module.lambda_handler(value, None) == expected_fallback


def test_lambda_transport_failure_preserves_accepted_eight_variants(monkeypatch):
    module = load_lambda()
    from smarthome.alexa_conversation import GREETINGS, handle_alexa_conversation
    monkeypatch.setenv('EXPECTED_ALEXA_SKILL_ID', SKILL)
    monkeypatch.setenv('LUMI_CONVERSATION_BACKEND', 'codex')
    monkeypatch.setenv('LUMI_CODEX_RELAY_URL', 'https://example.invalid/lumi/voice')
    monkeypatch.setenv('LUMI_CODEX_RELAY_SECRET', RELAY_SECRET)
    monkeypatch.delenv('LUMI_PAIRING_DIAGNOSTIC_UNTIL', raising=False)
    monkeypatch.setattr(module, '_read_openai_api_key', lambda *_: pytest.fail('Launch never reads model secrets'))
    monkeypatch.setattr(module, '_relay_open', lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError('synthetic transport failure')))
    outputs = set()
    for index in range(128):
        value = launch()
        value['session']['sessionId'] = f'synthetic-session-{index}'
        accepted = handle_alexa_conversation(value, expected_application_id=SKILL,
            responder=None).to_alexa_response()
        assert module.lambda_handler(value, None) == accepted
        assert module.lambda_handler(value, None) == accepted
        outputs.add(accepted['response']['outputSpeech']['text'])
        assert accepted['response']['shouldEndSession'] is False
        assert 'reprompt' not in accepted['response']
    assert outputs == set(GREETINGS)


def test_signed_neutral_runtime_fallback_is_replaced_by_accepted_session_variant(monkeypatch):
    module = load_lambda()
    from smarthome.alexa_conversation import GREETINGS, handle_alexa_conversation
    monkeypatch.setenv('EXPECTED_ALEXA_SKILL_ID', SKILL)
    monkeypatch.setenv('LUMI_CONVERSATION_BACKEND', 'codex')
    monkeypatch.setenv('LUMI_CODEX_RELAY_URL', 'https://example.invalid/lumi/voice')
    monkeypatch.setenv('LUMI_CODEX_RELAY_SECRET', RELAY_SECRET)
    monkeypatch.delenv('LUMI_PAIRING_DIAGNOSTIC_UNTIL', raising=False)
    neutral = [GREETINGS[0]]
    class Reply:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False
        def geturl(self):
            return 'https://example.invalid/lumi/voice'
        def read(self, _limit):
            return self.raw
    def relay_open(request, **_kwargs):
        reply = Reply()
        reply.raw = json.dumps({'version': '1.0', 'response': {
            'outputSpeech': {'type': 'PlainText', 'text': neutral[0]},
            'shouldEndSession': False}}).encode()
        nonce = request.get_header('X-lumi-nonce')
        reply.headers = {'Content-Type': 'application/json', 'X-Lumi-Response-Signature':
                         mac(RELAY_SECRET, 'RESPONSE', nonce, 200, reply.raw)}
        return reply
    monkeypatch.setattr(module, '_relay_open', relay_open)
    for index, text in enumerate(GREETINGS):
        neutral[0] = text
        value = launch()
        value['session']['sessionId'] = f'neutral-fallback-session-{index}'
        accepted = handle_alexa_conversation(value, expected_application_id=SKILL,
            responder=None).to_alexa_response()
        assert module.lambda_handler(value, None) == accepted


def test_lumi_routine_reprompt_is_removed_without_changing_direct_voice():
    assert 'reprompt' not in LumiRelay._speech('Eine Antwort.', end=False, reprompt='Möchtest du noch etwas wissen?')['response']
    assert 'reprompt' in gateway_module.VoiceGateway._speech('Eine Antwort.', end=False, reprompt='Möchtest du noch etwas wissen?')['response']


def test_lambda_keeps_existing_question_route_and_preserves_slim_question(monkeypatch):
    module = load_lambda()
    monkeypatch.setenv('EXPECTED_ALEXA_SKILL_ID', SKILL)
    monkeypatch.setenv('LUMI_CONVERSATION_BACKEND', 'codex')
    monkeypatch.delenv('LUMI_PAIRING_DIAGNOSTIC_UNTIL', raising=False)
    value = event(person_id='verified-person-a')
    slim = module._slim_codex_event(value)
    assert slim['request']['intent'] == value['request']['intent']
    assert slim['request']['type'] == 'IntentRequest'
    assert slim['session']['sessionId'] == 'session-a'
    seen = []
    expected = {'version':'1.0','response':{'outputSpeech':{'type':'PlainText','text':'Antwort auf eine Frage.'},'shouldEndSession':False}}
    monkeypatch.setattr(module, '_codex_relay_response', lambda incoming: seen.append(incoming) or expected)
    assert module.lambda_handler(value, None) == expected
    assert seen == [value]
