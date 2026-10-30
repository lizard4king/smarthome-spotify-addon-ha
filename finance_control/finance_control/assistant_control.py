"""Narrow administrative stdin commands; no shell, ledger access or secret output.

Home Assistant already provides authenticated administrative stdin delivery.
The listener is enabled only by the server launcher, not by HTTP requests.
"""
import json
import re

from .assistant_configuration import provision
from .assistant_client import status

MAX_LINE = 2048
PREFIX = 'Finance Control assistant control: '


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_key')
        result[key] = value
    return result


def serve(stream, output):
    while True:
        try:
            raw = stream.readline(MAX_LINE + 1)
        except (OSError, ValueError):
            return
        if not raw:
            return
        result = {'error': 'invalid_command'}
        request_id = None
        if len(raw) > MAX_LINE:
            while raw and not raw.endswith(b'\n'):
                raw = stream.readline(MAX_LINE + 1)
        else:
            try:
                data = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique,
                    parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                if type(data) is dict and 'request_id' in data:
                    request_id = data.pop('request_id')
                    if type(request_id) is not str or re.fullmatch('[0-9a-f]{32}', request_id) is None:
                        request_id = None
                        raise ValueError('invalid_request_id')
                if (type(data) is dict and set(data) == {'command', 'token', 'confirmed'}
                        and data['command'] == 'assistant-configure' and data['confirmed'] is True):
                    result = {'error': 'configuration_failed'}
                    if (type(data['token']) is str and 32 <= len(data['token']) <= 128
                            and data['token'].isascii()
                            and all(33 <= ord(c) <= 126 for c in data['token'])):
                        provision(data['token'])
                        result = {'configured': True}
                    else:
                        result = {'error': 'invalid_command'}
                elif (type(data) is dict and data == {'command': 'assistant-status'}):
                    result = {'error': 'status_failed'}
                    checked = status()
                    # Do not trust a future adapter to return only public fields.
                    keys = ('configured', 'reachable', 'codex_authenticated', 'qwen_ready')
                    if type(checked) is dict and all(type(checked.get(k)) is bool for k in keys):
                        result = {key: checked[key] for key in keys}
            except Exception:
                # Neither exception messages nor the request may enter logs.
                pass
        if request_id is not None:
            result['request_id'] = request_id
        output.write(PREFIX + json.dumps(result, separators=(',', ':')) + '\n')
        output.flush()


def start():
    import sys
    from threading import Thread
    thread = Thread(target=serve, args=(sys.stdin.buffer, sys.stdout),
                    name='assistant-control', daemon=True)
    thread.start()
    return thread
