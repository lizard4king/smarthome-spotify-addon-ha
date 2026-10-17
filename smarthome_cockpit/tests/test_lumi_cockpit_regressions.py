"""Synthetic Lumi/PR52 integration checks without network or device access."""
import hashlib
import io
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dashboard import codex_control
from dashboard.music_assistant import MusicAssistant, validate_search_page
from smarthome.home_assistant import (
    HomeAssistantAdapter, HomeAssistantConfig, HomeAssistantConnectionError,
    HomeAssistantError,
)

SECRET = 'a' * 32
CONTROL = '/api/codex/control'
SPEECH = '/api/codex/speech'

def make_broker(**changes):
    calls = []
    config = SimpleNamespace(
        entities={'lamp': 'light.lamp', 'wohnzimmer_heizung': 'climate.living'},
        write_allowlist={'lamp', 'wohnzimmer_heizung'}, allow_writes=True,
        blocked_domains=set(),
    )
    config.__dict__.update(changes)
    adapter = SimpleNamespace(
        _request=lambda *args: calls.append(args),
        set_light=lambda *args: calls.append(args),
        get_state=lambda _: {'state': 'off', 'attributes': {
            'current_temperature': 21.3, 'temperature': 22}},
    )
    broker = codex_control.ControlBroker(SECRET, lambda: config, lambda _: adapter, lambda: 1000, lambda: 1000)
    return (broker, calls)

def send(broker, payload, *, path=CONTROL, nonce=1, signature=None):
    raw = json.dumps(payload, separators=(',', ':')).encode()
    nonce = f'{nonce:032x}'
    headers = {'Content-Type': 'application/json', 'X-Control-Timestamp': '1000', 'X-Control-Nonce': nonce}
    headers['X-Control-Signature'] = signature if signature is not None else codex_control.mac(SECRET, 'POST', path, '1000', nonce, hashlib.sha256(raw).hexdigest())
    status, body, response_signature = broker.process(path, headers, raw)
    assert response_signature == codex_control.mac(SECRET, 'RESPONSE', path, nonce, str(status), hashlib.sha256(body).hexdigest())
    return (status, json.loads(body))

def speech_payload(target='wohnzimmer', text='Testansage.'):
    return {'request_id': 'b' * 32, 'target': target, 'text': text}

def setenv(key, value):
    os.environ[key] = value

class LumiCockpitRegressionTests(unittest.TestCase):

    def setUp(self):
        self.enterContext(patch.object(os, 'environ', {}))

    def test_speech_authentication_precedes_config_and_effects(self):
        setenv('CODEX_ANSWER_TARGETS_JSON', '{"wohnzimmer":"notify.living"}')
        broker, calls = make_broker()
        broker.config_factory = lambda: self.fail('Unauthenticated request read configuration')
        assert send(broker, speech_payload(), path=SPEECH, signature='invalid')[0] == 403
        assert calls == []

    def test_speech_rejects_unapproved_or_ambiguous_targets(self):
        for mapping, target in [
            ('{"wohnzimmer":"notify.living"}', 'kueche'),
            ('{"wohnzimmer":"media_player.living"}', 'wohnzimmer'),
            ('{"wohnzimmer":"notify.living","kueche":"notify.living"}', 'wohnzimmer'),
            ('{"fremd":"notify.living"}', 'fremd'),
        ]:
            with self.subTest(mapping=mapping, target=target):
                setenv('CODEX_ANSWER_TARGETS_JSON', mapping)
                broker, calls = make_broker()
                assert send(broker, speech_payload(target), path=SPEECH)[0] == 403
                assert calls == []

    def test_speech_respects_home_assistant_write_permissions(self):
        for changes in [{'allow_writes': False}, {'blocked_domains': {'notify'}}]:
            with self.subTest(changes=changes):
                setenv('CODEX_ANSWER_TARGETS_JSON', '{"wohnzimmer":"notify.living"}')
                broker, calls = make_broker(**changes)
                assert send(broker, speech_payload(), path=SPEECH)[0] == 403
                assert calls == []

    def test_speech_approved_target_and_duplicate_execute_once(self):
        setenv('CODEX_ANSWER_TARGETS_JSON', '{"wohnzimmer":"notify.living"}')
        broker, calls = make_broker()
        first = send(broker, speech_payload(), path=SPEECH)
        assert first[0] == 200
        assert send(broker, speech_payload(), path=SPEECH, nonce=2) == first
        assert send(broker, speech_payload(text='Anderer Auftrag.'), path=SPEECH, nonce=3)[0] == 403
        assert calls == [('POST', 'api/services/notify/send_message', {'entity_id': 'notify.living', 'message': 'Testansage.'})]

    def test_music_room_context_and_actions(self):
        cases = [
            ({'operation': 'music_play', 'target': 'wohnzimmer', 'query': 'Song'},
             [('tracks', 'Song'), ('play', 'filesystem_local://track/song.flac', 'player-living')]),
            *[({'operation': 'music_control', 'target': 'wohnzimmer', 'command': command},
               [('control', 'player-living', command)])
              for command in ('pause', 'resume', 'stop')],
        ]
        for action, expected in cases:
            with self.subTest(action=action, expected=expected):
                calls = []

                class FakeMusic:
                    configured = valid_config = allow_playback = True

                    def tracks(self, query):
                        calls.append(('tracks', query))
                        return {'tracks': [{'uri': 'filesystem_local://track/song.flac'}]}

                    def play(self, uri, player):
                        calls.append(('play', uri, player))

                    def control(self, player, command):
                        calls.append(('control', player, command))
                self.enterContext(patch.object(codex_control, 'MusicAssistant', FakeMusic))
                setenv('CODEX_MUSIC_TARGETS_JSON', '{"wohnzimmer":"player-living"}')
                broker, _ = make_broker()
                context = broker.context()
                assert context['music'] == {'enabled': True, 'targets': ['wohnzimmer']}
                assert {'music_play', 'music_control'} <= set(context['operations'])
                assert send(broker, {'request_id': 'c' * 32, 'action': action})[0] == 200
                assert calls == expected
                action = dict(action, target='kueche')
                assert send(broker, {'request_id': 'd' * 32, 'action': action}, nonce=2)[0] == 403
                assert calls == expected

    def test_temperature_status_reports_measured_target_and_mode(self):
        broker, calls = make_broker()
        status, body = send(broker, {'request_id': 'c' * 32, 'action': {'operation': 'status', 'alias': 'wohnzimmer_heizung'}})
        assert status == 200
        assert body == {'receipt': 'Es sind aktuell 21,3 °C im Wohnzimmer. '
                        'Die Solltemperatur liegt bei 22,0 °C. '
                        'Die Heizung im Wohnzimmer ist ausgeschaltet.'}
        assert calls == []

    def test_concurrent_duplicate_write_reserves_before_side_effect(self):
        broker, calls = make_broker()
        started, release = (threading.Event(), threading.Event())

        def execute(action, operation):
            calls.append((action, operation))
            started.set()
            assert release.wait(5)
            return {'receipt': 'Angenommen.'}
        broker.execute = execute
        payload = {'request_id': 'c' * 32, 'action': {'operation': 'set_light', 'alias': 'lamp', 'is_on': True}}
        with ThreadPoolExecutor(2) as executor:
            pending = executor.submit(send, broker, payload)
            try:
                assert started.wait(5)
                assert send(broker, payload, nonce=2)[0] == 503
            finally:
                release.set()
            assert pending.result()[0] == 200
        assert send(broker, payload, nonce=3) == (200, {'receipt': 'Angenommen.'})
        assert len(calls) == 1

    def test_home_assistant_get_state_uses_selected_mapping_only(self):
        requests = []
        response = {'state': 'heat', 'attributes': {'current_temperature': 21.3}}
        transport = SimpleNamespace(request=lambda *args: requests.append(args) or response)
        config = HomeAssistantConfig('https://ha.invalid:8123', 'synthetic-token', {'room': 'climate.room'})
        adapter = HomeAssistantAdapter(config, transport=transport)
        assert adapter.get_state('room') == response
        assert requests[0][0:2] == ('GET', 'https://ha.invalid:8123/api/states/climate.room')
        assert requests[0][3] is None
        with self.assertRaises(HomeAssistantError):
            adapter.get_state('foreign')
        assert len(requests) == 1
        response = []
        with self.assertRaises(HomeAssistantConnectionError):
            adapter.get_state('room')

    def test_music_timeout_reaches_urllib_opener_and_pr52_api_remains(self):
        setenv('MUSIC_ASSISTANT_URL', 'http://ma.invalid:8095')
        setenv('MUSIC_ASSISTANT_TOKEN', 'synthetic-token')
        observed = []

        def open_response(request, *, timeout):
            observed.append((request, timeout))
            response = io.BytesIO(b'[]')
            response.headers = {'Content-Type': 'application/json'}
            return response
        client = MusicAssistant(timeout_seconds=1.25)
        client.opener = SimpleNamespace(open=open_response)
        assert client.call_library('music/albums/library_items', offset=0) == []
        assert observed[0][1] == 1.25
        assert json.loads(observed[0][0].data)['command'] == 'music/albums/library_items'
        assert callable(client.local_uri) and callable(client.resolve_local_track)
        validate_search_page('Album', 0)
        with self.assertRaises(ValueError):
            validate_search_page('Album', True)

    def test_music_timeout_rejects_invalid_values(self):
        for timeout in [0, -1, 8.1, True, '2', float('inf'), float('nan')]:
            with self.subTest(timeout=timeout):
                with self.assertRaises(ValueError):
                    MusicAssistant(timeout_seconds=timeout)


if __name__ == '__main__':
    unittest.main()
