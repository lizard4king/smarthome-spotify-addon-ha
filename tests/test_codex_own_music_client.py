import json
import pytest
from codex_voice.control_client import validate_action, validate_context

ROOMS = ['kueche','wohnzimmer','badezimmer','buero','schlafzimmer']
def context(with_music=True):
    value = {'version':1,'devices':{},'spotify':{'enabled':False,'profiles':[],'targets':ROOMS},'operations':['none','status','set_light','set_temperature','spotify_play']}
    if with_music:
        value['music']={'enabled':True,'targets':['buero']}
        value['operations'] += ['music_play','music_control']
    return value

def test_accepts_old_context_without_music():
    validate_context(context(False))
    with pytest.raises(ValueError):
        validate_action({'operation':'music_play','target':'buero','query':'Test'},context(False))

@pytest.mark.parametrize('action',[
 {'operation':'music_play','target':'buero','query':'Test'},
 {'operation':'music_control','target':'buero','command':'pause'},
 {'operation':'music_control','target':'buero','command':'resume'},
 {'operation':'music_control','target':'buero','command':'stop'},
])
def test_supported_music_action(action):
    ctx=validate_context(context());assert validate_action(action,ctx)==action

@pytest.mark.parametrize('action',[
 {'operation':'music_play','target':'foreign','query':'Test'},
 {'operation':'music_play','target':'buero','query':'file:///secret'},
 {'operation':'music_control','target':'buero','command':'delete'},
 {'operation':'music_control','target':'buero','command':[]},
])
def test_rejects_unsupported_music_action(action):
    with pytest.raises(ValueError):validate_action(action,context())

def test_rejects_nonstring_music_targets():
    ctx=context();ctx['music']['targets']=[[]]
    with pytest.raises(ValueError):validate_context(ctx)

def test_action_schema_contains_music_operations():
    from pathlib import Path
    schema=json.loads((Path(__file__).resolve().parents[1]/'codex_voice/action_schema.json').read_text())
    actions=schema['properties']['action']['anyOf']
    assert {'music_play','music_control'} <= {a['properties']['operation']['const'] for a in actions}

@pytest.mark.parametrize("operation,expected", [("music_play",60),("music_control",60),("set_light",12)])
def test_music_only_uses_extended_async_client_deadline(operation, expected):
    from codex_voice.control_client import ControlClient
    calls=[]
    c=ControlClient('a'*32)
    def post(path,payload,**kwargs):
        calls.append(kwargs['timeout_seconds'])
        return {'receipt':'accepted'}
    c._post=post
    assert c.execute({'operation':operation}) == 'accepted'
    assert calls == [expected]

def test_prompt_advertises_only_current_capabilities():
    from types import SimpleNamespace
    from codex_voice.app import CodexRuntime
    runtime = CodexRuntime.__new__(CodexRuntime)
    runtime._control_client = SimpleNamespace(context=lambda: context(False))
    prompts=[]
    def query(question, **kwargs):
        prompts.append(kwargs['_control_prompt'])
        return json.dumps({'answer':'Test','action':{'operation':'none'}})
    runtime.query=query
    assert runtime.query_voice('Test') == 'Test'
    assert 'music_play(' not in prompts[0]
    assert 'music_control(' not in prompts[0]
    assert '480 characters' not in prompts[0]
    assert '2000 characters' in prompts[0]
