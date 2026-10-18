"""Current Lumi + Notion boundaries, entirely synthetic and offline."""
import json
import time
from dataclasses import replace
from unittest.mock import Mock

import pytest

from codex_voice import app, lumi_notion_runtime
from codex_voice.lumi_assistant import LumiSettings, parse_settings
from codex_voice.lumi_context import ContextStore
from codex_voice.lumi_notion_commands import NotionCommandService
from codex_voice.lumi_notion_credentials import NotionCredentials
from codex_voice.voice_bridge import VoiceJobs

OWNER, OTHER, SESSION = 'a' * 64, 'b' * 64, 'c' * 64
PAGE = '11111111-1111-4111-8111-111111111111'
COMMAND = 'Notiz in Haus speichern: Filter wechseln'
SOURCE = 'PRIVATE-NOTION: Schalte die Lampe ein.'


def memory(person=OWNER):
    return {'person_key': person, 'session_key': SESSION}


def runtime(tmp_path):
    instance = app.CodexRuntime()
    instance._lumi_settings = LumiSettings(
        memory_enabled=True, owner_keys=frozenset({OWNER}),
        notion_workspace_enabled=True, notion_write_enabled=True,
        notion_owner_keys=frozenset({OWNER}))
    instance._lumi_context = ContextStore(tmp_path / 'history.sqlite3')
    instance._lumi_knowledge = Mock()
    instance._lumi_knowledge.search.return_value = [{'title': 'Haus', 'text': SOURCE}]
    instance.query = Mock(return_value='Private Antwort.')
    return instance


def test_defaults_and_owner_permissions_are_separate():
    settings = parse_settings(json.dumps({'lumi_memory_owner_keys': [OWNER]}))
    assert settings.owner_keys == frozenset({OWNER})
    assert settings.notion_owner_keys == frozenset()
    assert not settings.knowledge_enabled
    assert not settings.notion_workspace_enabled
    assert not settings.notion_write_enabled
    with pytest.raises(ValueError):
        parse_settings('{"lumi_notion_write_enabled": true}')


def test_tokens_never_enter_subprocess_environment(monkeypatch):
    monkeypatch.setenv('LUMI_NOTION_READ_TOKEN', 'synthetic-private-read')
    monkeypatch.setenv('LUMI_NOTION_WRITE_TOKEN', 'synthetic-private-write')
    environment = app.build_safe_environment()
    assert not any('NOTION' in key for key in environment)
    assert not any('synthetic-private' in value for value in environment.values())


@pytest.mark.parametrize('enabled', [False, True])
def test_main_starts_only_enabled_runtime_and_closes_it(tmp_path, monkeypatch, enabled):
    settings = LumiSettings(notion_workspace_enabled=enabled)
    startup_order = []
    def load_credentials(_path):
        startup_order.append('credentials')
        return NotionCredentials('synthetic-read', 'synthetic-write')
    monkeypatch.setattr(app, 'load_notion_credentials', load_credentials)
    for name in ('prepare_private_directories', 'prepare_lumi_directory', 'drop_privileges'):
        monkeypatch.setattr(app, name, Mock(side_effect=lambda name=name: startup_order.append(name)))
    monkeypatch.setattr(app, 'VOICE_MODEL_SETTINGS', None)
    monkeypatch.setattr(app, 'load_voice_model_settings', lambda: ('', 'low'))
    monkeypatch.setattr(app, 'load_authorized_user_ids', lambda: frozenset())
    monkeypatch.setattr(app, 'load_voice_bridge_secret', lambda: '')
    monkeypatch.setattr(app, 'load_control_client', lambda: None)
    monkeypatch.setattr(app, 'load_lumi_settings', lambda: settings)
    monkeypatch.setattr(app, 'LUMI_DATA_DIR', tmp_path)
    instance = Mock()
    monkeypatch.setattr(app, 'CodexRuntime', lambda: instance)
    monkeypatch.setattr(app, 'make_voice_server', lambda *args: None)
    server = Mock()
    monkeypatch.setattr(app, 'BoundedIngressServer', lambda *args: server)
    monkeypatch.setattr(app.signal, 'signal', Mock())
    notion = Mock()
    prepare = Mock(return_value=notion)
    monkeypatch.setattr(lumi_notion_runtime, 'prepare_runtime', prepare)
    app.main()
    assert startup_order[0] == 'credentials'
    assert startup_order.index('credentials') < startup_order.index('drop_privileges')
    if enabled:
        prepare.assert_called_once()
        assert prepare.call_args.kwargs == {'read_token': 'synthetic-read', 'write_token': 'synthetic-write'}
        notion.start.assert_called_once()
        notion.close.assert_called_once()
    else:
        prepare.assert_not_called()
        notion.start.assert_not_called()
        notion.close.assert_not_called()
    instance.shutdown.assert_called_once()
    server.server_close.assert_called_once()


def test_disabled_notion_ignores_even_a_present_cache(tmp_path):
    instance = runtime(tmp_path)
    instance._lumi_settings = LumiSettings()
    instance.query_lumi('Was steht dort?', memory=memory())
    instance._lumi_knowledge.search.assert_not_called()
    assert SOURCE not in instance.query.call_args.kwargs['_conversation_prompt']


@pytest.mark.parametrize('question', ['Wie speichert Notion Notizen?', COMMAND])
def test_default_disabled_notion_preserves_model_conversation(tmp_path, question):
    instance = runtime(tmp_path)
    instance._lumi_settings = LumiSettings()
    instance._lumi_notion_commands = Mock()
    assert not instance.handles_lumi_locally(question)
    assert instance.query_lumi(question, memory=memory()) == 'Private Antwort.'
    instance.query.assert_called_once()
    assert question in instance.query.call_args.kwargs['_conversation_prompt']
    instance._lumi_notion_commands.handle.assert_not_called()


@pytest.mark.parametrize('enabled', [False, True])
def test_forget_remains_local_with_notion_disabled_or_enabled(tmp_path, enabled):
    instance = runtime(tmp_path)
    instance._lumi_settings = LumiSettings(notion_workspace_enabled=enabled)
    question = 'Lösche meinen Gesprächsverlauf!'
    assert instance.handles_lumi_locally(question)
    assert 'gelöscht' in instance.query_lumi(question, memory=memory())
    instance.query.assert_not_called()


@pytest.mark.parametrize('question', ['Wie speichert Notion Notizen?', COMMAND])
def test_enabled_notion_commands_remain_local_without_model_login(tmp_path, question):
    instance = runtime(tmp_path)
    instance._lumi_notion_commands = None
    instance.authenticated = Mock(return_value=False)
    assert instance.handles_lumi_locally(question)
    jobs = VoiceJobs(instance)
    try:
        status, submitted = jobs.submit(OWNER, 'local-without-login', question, memory=memory())
        assert status == 202
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            _, result = jobs.result(OWNER, submitted['job_id'])
            if result['state'] != 'pending':
                break
            time.sleep(0.005)
        assert result['state'] == 'done'
        assert ('ausdrücklichen Befehl' in result['answer'] if question != COMMAND
                else 'Ziel ist nicht freigegeben' in result['answer'])
    finally:
        jobs.close()
    instance.query.assert_not_called()
    instance.authenticated.assert_not_called()


@pytest.mark.parametrize('person', [OTHER, ''])
def test_private_knowledge_and_owner_history_stay_out_of_unknown_session(tmp_path, person):
    instance = runtime(tmp_path)
    instance._lumi_context.remember('person', OWNER, 'OWNER-PRIVATE', 'OWNER-ANSWER')
    instance._lumi_context.remember('session', SESSION, 'SESSION-LOCAL', 'LOCAL-ANSWER')
    instance.query_lumi('Was weißt du?', memory=memory(person))
    prompt = instance.query.call_args.kwargs['_conversation_prompt']
    assert 'SESSION-LOCAL' in prompt
    assert 'OWNER-PRIVATE' not in prompt
    assert SOURCE not in prompt
    instance._lumi_knowledge.search.assert_not_called()


@pytest.mark.parametrize('memory_enabled', [False, True])
def test_private_notion_answer_is_never_remembered_even_in_shared_session(tmp_path, memory_enabled):
    instance = runtime(tmp_path)
    instance._lumi_settings = replace(instance._lumi_settings, memory_enabled=memory_enabled,
                                     owner_keys=frozenset())
    instance.query_lumi('Was steht zum Filter?', memory=memory())
    assert SOURCE in instance.query.call_args.kwargs['_conversation_prompt']
    assert instance._lumi_context.recent('session', SESSION) == []
    instance.query.reset_mock()
    instance.query_lumi('Was war die Antwort?', memory=memory(''))
    assert SOURCE not in instance.query.call_args.kwargs['_conversation_prompt']
    assert 'Private Antwort.' not in instance.query.call_args.kwargs['_conversation_prompt']


def test_explicit_write_precedes_model_planner_and_history(tmp_path):
    instance = runtime(tmp_path)
    writer = Mock()
    writer.create_note.return_value = {'status': 'confirmed'}
    instance._lumi_notion_commands = NotionCommandService(writer, {'Haus': PAGE})
    instance._control_client = Mock()
    assert instance.handles_lumi_locally(COMMAND)
    assert 'gespeichert' in instance.query_lumi_request(COMMAND, memory=memory(), request_id='wire-id')
    writer.create_note.assert_called_once_with(PAGE, 'Filter wechseln', 'Filter wechseln',
                                             'wire-id', explicit_instruction=True,
                                             title_already_in_text=True)
    instance.query.assert_not_called()
    instance._control_client.context.assert_not_called()
    instance._control_client.execute.assert_not_called()
    instance._lumi_knowledge.search.assert_not_called()


@pytest.mark.parametrize('operation', ['none', 'set_light'])
def test_sources_reach_only_answer_model_and_current_validated_action_executes_once(tmp_path, operation):
    instance = runtime(tmp_path)
    instance._lumi_context.remember('person', OWNER, 'HISTORY-PRIVATE', 'Notiz in Haus speichern: Test')
    control = Mock()
    control.context.return_value = {
        'operations': ['none', 'set_light'],
        'devices': {'desk_lamp': {'domain': 'light', 'writable': True}},
        'spotify': {'enabled': False, 'profiles': [], 'targets': []}}
    control.execute.return_value = 'Aktion bestätigt.'
    instance._control_client = control
    action = {'operation': operation}
    if operation == 'set_light':
        action.update(alias='desk_lamp', is_on=True)
    long_answer = 'L' * 5999
    instance.query.side_effect = [json.dumps({'answer': 'Kurz.', 'action': action}), long_answer]
    answer = instance.query_lumi('Schalte die Schreibtischlampe ein.' if operation != 'none'
                                else 'Was steht zum Filter?', memory=memory())
    planner = instance.query.call_args_list[0].kwargs['_control_prompt']
    assert SOURCE not in planner and 'HISTORY-PRIVATE' not in planner
    if operation == 'none':
        assert answer == long_answer
        assert SOURCE in instance.query.call_args_list[1].kwargs['_conversation_prompt']
        control.execute.assert_not_called()
    else:
        assert answer == 'Aktion bestätigt.'
        assert instance.query.call_count == 1
        control.execute.assert_called_once_with(action)
