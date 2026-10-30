"""Authenticated, bounded client for the fixed internal assistant service."""
import json
import os
from pathlib import Path
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from .assistant_engine import BackendFailure, _input

DEFAULT_URL = 'http://7b071411-finance-assistant:8786/api/classify'
_ERRORS = {'authentication_required', 'usage_limit_reached', 'quota_exhausted',
           'codex_unavailable', 'qwen_unavailable', 'invalid_backend_response', 'timeout',
           'unauthorized', 'busy', 'invalid_request', 'service_unavailable', 'backend_unavailable'}
_LIMITS = {'usage_limit_reached', 'quota_exhausted'}


def _url():
    value = os.environ.get('FINANCE_CONTROL_ASSISTANT_URL', DEFAULT_URL)
    try:
        parts = urlsplit(value)
    except ValueError as exc:
        raise BackendFailure('invalid_assistant_configuration') from exc
    if (parts.scheme != 'http' or parts.netloc not in
            ('7b071411-finance-assistant:8786', 'local-finance-assistant:8786')
            or parts.path != '/api/classify' or parts.query or parts.fragment
            or value != f'http://{parts.netloc}/api/classify'):
        raise BackendFailure('invalid_assistant_configuration')
    return value


def _token():
    try:
        with Path(os.environ['FINANCE_CONTROL_ASSISTANT_TOKEN_FILE']).open('rb') as stream:
            token = stream.read(514).rstrip(b'\r\n')
        if not 32 <= len(token) <= 512 or any(c < 33 or c > 126 for c in token):
            raise ValueError
        return token.decode('ascii')
    except (OSError, KeyError, TypeError, ValueError, UnicodeError) as exc:
        raise BackendFailure('invalid_assistant_configuration') from exc


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_json_key')
        result[key] = value
    return result


def _decode(raw):
    if len(raw) > 65536:
        raise ValueError('oversized_response')
    return json.loads(raw.decode('utf-8'), object_pairs_hook=_object,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))


def classify(payload):
    """Return one strictly validated, unconfirmed internal-service proposal."""
    try:
        allowed = _input(payload)
        body = json.dumps(payload, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise BackendFailure('invalid_request') from exc
    request = urllib.request.Request(_url(), data=body,
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + _token()}, method='POST')
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=255) as response:
            raw = response.read(65537)
        result = _decode(raw)
    except urllib.error.HTTPError as exc:
        fallback_reason = None
        try:
            error = _decode(exc.read(65537))
            code = error.get('error') if type(error) is dict and set(error) in (
                {'error'}, {'error', 'fallback_reason'}) else None
            if type(code) is not str or code not in _ERRORS:
                code = 'service_unavailable'
            if (type(error) is dict and set(error) == {'error', 'fallback_reason'}
                    and type(error.get('error')) is str and error['error'] in _ERRORS
                    and type(error.get('fallback_reason')) is str
                    and error['fallback_reason'] in _LIMITS):
                fallback_reason = error['fallback_reason']
        except (OSError, ValueError, UnicodeError):
            code = 'service_unavailable'
        raise BackendFailure(code, fallback_reason=fallback_reason) from None
    except (OSError, TimeoutError):
        raise BackendFailure('service_unavailable') from None
    except (ValueError, UnicodeError, TypeError):
        raise BackendFailure('invalid_backend_response') from None
    if type(result) is not dict or set(result) not in (
            {'status', 'engine', 'category', 'confidence', 'reason'},
            {'status', 'engine', 'category', 'confidence', 'reason', 'fallback_reason'}):
        raise BackendFailure('invalid_backend_response')
    reason = result['reason']
    if (result['status'] != 'proposed' or result['engine'] not in ('codex_cli', 'qwen')
            or type(result['category']) is not str or result['category'] not in allowed
            or result['confidence'] not in ('low', 'medium', 'high')
            or type(reason) is not str or not reason.strip() or len(reason) > 300
            or any(ord(c) < 32 for c in reason)
            or ('fallback_reason' in result and (result['engine'] != 'qwen'
                or type(result['fallback_reason']) is not str or result['fallback_reason'] not in _LIMITS))
           ):
        raise BackendFailure('invalid_backend_response')
    return result
