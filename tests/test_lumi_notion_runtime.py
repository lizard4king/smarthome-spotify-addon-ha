"""Synthetic runtime boundary checks; no Notion connection or credentials."""
import hashlib
import json
import threading
from unittest.mock import Mock

import pytest

from codex_voice.app import CodexRuntime
from codex_voice.lumi_assistant import LumiSettings
from codex_voice.lumi_knowledge import KnowledgeStore
from codex_voice.lumi_notion_commands import NotionCommandService
from codex_voice.lumi_context import ContextStore
from codex_voice import lumi_notion_runtime as orchestration
from codex_voice.voice_bridge import VoiceJobs, _json_bytes


OWNER = 'a' * 64
OTHER = 'b' * 64
SESSION = 'c' * 64
PAGE = '11111111-1111-4111-8111-111111111111'
COMMAND = 'Notiz in Haus speichern: Filter wechseln'


@pytest.fixture
def runtime():
    runtime = CodexRuntime()
    runtime._lumi_settings = LumiSettings(
        memory_enabled=False, notion_workspace_enabled=True,
        notion_write_enabled=True, notion_owner_keys=frozenset({OWNER}))
    writer = Mock()
    writer.create_note.return_value = {'status': 'confirmed', 'operation_id': 'op'}
    runtime._lumi_notion_commands = NotionCommandService(writer, {'Haus': PAGE})
    runtime.query = Mock(return_value='Antwort')
    return runtime


def test_owner_can_write_with_conversation_memory_disabled(runtime):
    assert not runtime._lumi_settings.memory_enabled
    answer = runtime.query_lumi_request(
        COMMAND, memory={'person_key': OWNER, 'session_key': SESSION}, request_id='wire-id')
    assert 'gespeichert' in answer
    runtime._lumi_notion_commands.writer.create_note.assert_called_once_with(
        PAGE, 'Filter wechseln', 'Filter wechseln', 'wire-id', explicit_instruction=True,
        title_already_in_text=True)
    runtime.query.assert_not_called()


@pytest.mark.parametrize('person', [OTHER, ''])
def test_nonowner_cannot_write(runtime, person):
    answer = runtime.query_lumi_request(
        COMMAND, memory={'person_key': person, 'session_key': SESSION}, request_id='wire-id')
    assert 'Freigabe' in answer
    runtime._lumi_notion_commands.writer.create_note.assert_not_called()
    runtime.query.assert_not_called()


def test_direct_call_without_wire_id_cannot_write(runtime):
    answer = runtime.query_lumi(COMMAND, memory={'person_key': OWNER, 'session_key': SESSION})
    assert 'Auftragskennung' in answer
    runtime._lumi_notion_commands.writer.create_note.assert_not_called()


def test_current_control_action_executes_once_without_history_or_notion_in_planner(runtime):
    action = {'operation': 'set_light', 'alias': 'desk_lamp', 'is_on': True}
    control = Mock()
    control.context.return_value = {
        'operations': ['none', 'set_light'],
        'devices': {'desk_lamp': {'domain': 'light', 'writable': True}},
        'spotify': {'enabled': False, 'profiles': [], 'targets': []}}
    control.execute.return_value = 'Aktion bestätigt.'
    runtime._control_client = control
    runtime.query.return_value = json.dumps({'answer': 'Kurz.', 'action': action})
    question = 'Schalte die Schreibtischlampe ein.'
    answer_context = 'HISTORY_PRIVATE\nNOTION_PRIVATE'

    assert runtime.query_voice(question, _context_prompt=answer_context) == 'Aktion bestätigt.'

    runtime.query.assert_called_once()
    call = runtime.query.call_args
    assert call.args[0] == question
    planner = call.kwargs['_control_prompt']
    assert question in planner
    assert 'HISTORY_PRIVATE' not in planner
    assert 'NOTION_PRIVATE' not in planner
    assert '_conversation_prompt' not in call.kwargs
    control.execute.assert_called_once_with(action)


def test_history_and_notion_context_reach_answer_only_when_planner_selects_none(runtime):
    control = Mock()
    control.context.return_value = {
        'operations': ['none', 'set_light'],
        'devices': {'desk_lamp': {'domain': 'light', 'writable': True}},
        'spotify': {'enabled': False, 'profiles': [], 'targets': []}}
    runtime._control_client = control
    runtime.query.side_effect = [
        json.dumps({'answer': 'Planantwort.', 'action': {'operation': 'none'}}),
        'Antwort mit Kontext.',
    ]
    question = 'Was steht in meinen Notizen?'
    answer_context = 'HISTORY_PRIVATE\nNOTION_PRIVATE'

    assert runtime.query_voice(question, _context_prompt=answer_context) == 'Antwort mit Kontext.'

    assert runtime.query.call_count == 2
    planner = runtime.query.call_args_list[0].kwargs['_control_prompt']
    assert question in planner
    assert 'HISTORY_PRIVATE' not in planner
    assert 'NOTION_PRIVATE' not in planner
    assert '_conversation_prompt' not in runtime.query.call_args_list[0].kwargs
    answer_prompt = runtime.query.call_args_list[1].kwargs['_conversation_prompt']
    assert 'HISTORY_PRIVATE' in answer_prompt
    assert 'NOTION_PRIVATE' in answer_prompt
    assert f'Aktuelle Frage:\n{question}' in answer_prompt
    control.execute.assert_not_called()


def test_forget_request_is_handled_before_notion_parser(runtime):
    runtime._lumi_notion_commands = Mock()
    runtime.query_lumi_request(
        'Lösche meinen Gesprächsverlauf!', memory={'person_key': OWNER, 'session_key': SESSION},
        request_id='wire-id')
    runtime._lumi_notion_commands.handle.assert_not_called()
    runtime.query.assert_not_called()


@pytest.mark.parametrize('person,visible', [(OWNER, True), (OTHER, False)])
def test_workspace_notes_only_reach_owner_and_never_execute(runtime, tmp_path, person, visible):
    runtime._lumi_knowledge = KnowledgeStore(tmp_path / 'notes.sqlite3')
    runtime._lumi_knowledge.replace_pages([{
        'page_id': PAGE, 'title': 'Haus', 'url': 'https://www.notion.so/' + PAGE,
        'text': COMMAND, 'edited_at': '2026-10-11T10:00:00Z',
    }], [PAGE])
    runtime.query_lumi_request(
        'Was steht zum Filter in Haus?', memory={'person_key': person, 'session_key': SESSION},
        request_id='wire-id')
    prompt = runtime.query.call_args.kwargs['_conversation_prompt']
    assert (COMMAND in prompt) is visible
    runtime._lumi_notion_commands.writer.create_note.assert_not_called()
    runtime._lumi_notion_commands.writer.update_note.assert_not_called()


def test_wire_jobs_transport_principal_bound_request_id():
    finished = threading.Event()
    calls = []

    class Runtime:
        def authenticated(self):
            return True

        def query_lumi(self, *args, **kwargs):
            pytest.fail('Wire jobs must select request-aware entry point')

        def query_lumi_request(self, question, *, memory, request_id):
            calls.append((question, memory, request_id))
            finished.set()
            return 'ok'

    jobs = VoiceJobs(Runtime())
    try:
        memory = {'person_key': OWNER, 'session_key': SESSION}
        status, _ = jobs.submit(OWNER, 'signed-wire-request', COMMAND, memory=memory)
        assert status == 202
        assert finished.wait(2)
        expected = hashlib.sha256(_json_bytes({
            'principal': OWNER, 'request_id': 'signed-wire-request',
        })).hexdigest()
        assert calls == [(COMMAND, memory, expected)]
        assert expected != hashlib.sha256(_json_bytes({
            'principal': OTHER, 'request_id': 'signed-wire-request',
        })).hexdigest()
    finally:
        jobs.close()


@pytest.fixture
def orchestrated(tmp_path, monkeypatch):
    catalog = {'page_ids': [PAGE], 'targets': {'Haus': PAGE}}
    monkeypatch.setattr(orchestration, 'load_catalog', lambda _: catalog)
    store, discovery, sync, writer = Mock(), Mock(), Mock(), Mock()
    writer.create_note.return_value = {'status': 'confirmed', 'operation_id': 'op'}
    catalog_factory = Mock(return_value=discovery)
    sync_factory = Mock(return_value=sync)
    writer_factory = Mock(return_value=writer)
    now = [1000.0]
    instance = orchestration.NotionRuntime(
        tmp_path, store, read_token='synthetic-read', write_token='synthetic-write',
        write_enabled=True, catalog_factory=catalog_factory,
        sync_factory=sync_factory, writer_factory=writer_factory, clock=lambda: now[0])
    return instance, catalog, store, discovery, sync, catalog_factory, sync_factory, writer_factory, now


def test_full_catalog_revokes_old_pages_and_syncs_with_explicit_bounds(orchestrated):
    instance, catalog, store, discovery, sync, _, sync_factory, _, _ = orchestrated
    assert instance.refresh()
    discovery.sync.assert_called_once_with(instance.catalog_path)
    store.revoke_except.assert_called_once_with([PAGE])
    sync_factory.assert_called_once_with(
        store, 'synthetic-read', max_pages=10000, max_total_seconds=900, max_blocks=100000)
    sync.sync.assert_called_once_with(catalog['page_ids'])
    assert instance.status == 'ready'


def test_transient_catalog_error_preserves_previous_cache(orchestrated):
    instance, _, store, discovery, sync, _, _, _, _ = orchestrated
    assert instance.refresh()
    store.reset_mock()
    sync.reset_mock()
    discovery.sync.side_effect = OSError('synthetic transient outage')
    assert not instance.refresh()
    store.revoke_except.assert_not_called()
    sync.sync.assert_not_called()
    assert instance.status == 'refresh-failed'


def test_no_read_token_never_connects_or_writes(tmp_path):
    factory = Mock(side_effect=AssertionError('No network without token'))
    instance = orchestration.NotionRuntime(
        tmp_path, Mock(), read_token='', write_token='', write_enabled=True,
        catalog_factory=factory, sync_factory=factory, writer_factory=factory)
    assert not instance.refresh()
    assert instance.status == 'not-configured'
    assert 'gespeichert' not in instance.handle(COMMAND, 'wire-id', authorized=True)
    factory.assert_not_called()


def test_writes_require_live_confirmed_catalog(orchestrated):
    instance, _, _, _, _, _, _, writer_factory, _ = orchestrated
    assert 'gespeichert' not in instance.handle(COMMAND, 'wire-id', authorized=True)
    writer_factory.assert_not_called()


def test_fresh_catalog_supplies_writer_allowlist_and_request_id(orchestrated):
    instance, _, _, _, _, _, _, writer_factory, _ = orchestrated
    assert instance.refresh()
    assert 'gespeichert' in instance.handle(COMMAND, 'wire-id', authorized=True)
    writer_factory.assert_called_once_with(
        instance.directory / 'notion-writes.sqlite3', 'synthetic-write', [PAGE])
    writer_factory.return_value.create_note.assert_called_once_with(
        PAGE, 'Filter wechseln', 'Filter wechseln', 'wire-id', explicit_instruction=True,
        title_already_in_text=True)


@pytest.mark.parametrize('elapsed', [900, 901, -1])
def test_stale_or_clock_reversed_catalog_refuses_writes(orchestrated, elapsed):
    instance, _, _, _, _, _, _, writer_factory, now = orchestrated
    assert instance.refresh()
    now[0] += elapsed
    assert 'gespeichert' not in instance.handle(COMMAND, 'wire-id', authorized=True)
    writer_factory.assert_not_called()


def test_new_catalog_removes_revoked_alias_and_page(orchestrated):
    instance, catalog, store, _, _, _, _, writer_factory, _ = orchestrated
    assert instance.refresh()
    catalog.update(page_ids=[], targets={})
    assert instance.refresh()
    assert 'gespeichert' not in instance.handle(COMMAND, 'wire-id', authorized=True)
    store.revoke_except.assert_called_with([])
    writer_factory.return_value.create_note.assert_not_called()


def test_private_workspace_answer_never_leaks_through_shared_session(runtime, tmp_path):
    runtime._lumi_settings = LumiSettings(
        memory_enabled=True, owner_keys=frozenset(), notion_workspace_enabled=True,
        notion_write_enabled=True, notion_owner_keys=frozenset({OWNER}))
    runtime._lumi_context = ContextStore(tmp_path / 'conversation.sqlite3')
    runtime._lumi_knowledge = KnowledgeStore(tmp_path / 'private.sqlite3')
    runtime._lumi_knowledge.replace_pages([{
        'page_id': PAGE, 'title': 'Privater Filter', 'url': 'https://www.notion.so/' + PAGE,
        'text': 'Filter Passwort UNIQUE_PRIVATE_SECRET', 'edited_at': '2026-10-11T10:00:00Z',
    }], [PAGE])
    runtime.query.return_value = 'UNIQUE_PRIVATE_SECRET'
    runtime.query_lumi_request('Wo ist das Filter Passwort?',
        memory={'person_key': OWNER, 'session_key': SESSION}, request_id='owner-question')
    assert runtime._lumi_context.recent('session', SESSION) == []
    runtime.query.return_value = 'Keine privaten Quellen.'
    runtime.query_lumi_request('Was weißt du noch?',
        memory={'person_key': OTHER, 'session_key': SESSION}, request_id='other-question')
    assert 'UNIQUE_PRIVATE_SECRET' not in runtime.query.call_args.kwargs['_conversation_prompt']
