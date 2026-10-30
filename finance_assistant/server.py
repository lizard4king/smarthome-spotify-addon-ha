"""Private classification endpoint; no ledger, Supervisor or host access."""
import hmac
import base64
import hashlib
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import threading
from urllib.parse import urlsplit

from finance_control.assistant_engine import BackendFailure, classify

MAX_BODY = 6000
REQUEST_LOCK = threading.Lock()
PUBLIC_BACKEND_ERRORS = frozenset({
    'authentication_required', 'usage_limit_reached', 'quota_exhausted',
    'codex_unavailable', 'qwen_unavailable', 'invalid_backend_response', 'timeout',
})
LOGIN_PAGE = b'''<!doctype html><html lang="de"><meta charset="utf-8"><title>Finance Assistant</title>
<h1>Finance Assistant</h1><p>OpenAI Codex zuerst, Qwen lokal als Fallback.</p>
<button id="login">Codex anmelden</button><p id="state"></p>
<a id="link" href="https://auth.openai.com/codex/device" rel="noopener noreferrer" target="_blank" hidden>Geraeteanmeldung oeffnen</a>
<script>
const state=document.getElementById('state'), link=document.getElementById('link');
async function poll(){try {const r=await fetch('./ingress/login-state');const x=await r.json();
state.textContent=x.code ? 'Code: '+x.code : x.state;link.hidden=!(x.url==='https://auth.openai.com/codex/device' && x.code);
if(x.state==='pending')setTimeout(poll,2000);}catch(e){state.textContent='Anmeldung nicht erreichbar';}}
document.getElementById('login').onclick=async()=>{try{const r=await fetch('./ingress/login-start',
{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({confirmed:true})});
if(!r.ok){state.textContent='Anmeldung konnte nicht gestartet werden';return;}await poll();}
catch(e){state.textContent='Anmeldung nicht erreichbar';}};
</script></html>'''


def read_token(path=Path('/data/options.json')):
    """Fail closed when the local add-on option is absent or invalid."""
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        token = data['assistant_token']
        if (type(token) is not str or len(token) < 32 or len(token) > 512
                or not token.isascii() or any(ord(c) < 33 or ord(c) > 126 for c in token)):
            raise ValueError
        return token.encode('ascii')
    except (OSError, KeyError, TypeError, ValueError, UnicodeError) as exc:
        raise ValueError('invalid_assistant_configuration') from exc


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_key')
        result[key] = value
    return result


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, format, *args):
        pass  # Neither request paths nor credentials nor input enter access logs.

    def reply(self, status, body):
        encoded = json.dumps(body, ensure_ascii=True, separators=(',', ':')).encode('ascii')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(encoded)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(encoded)
        self.close_connection = True

    def authorized(self):
        headers = self.headers.get_all('Authorization', [])
        expected = b'Bearer ' + self.server.assistant_token
        try:
            supplied = headers[0].encode('ascii') if len(headers) == 1 else b''
        except UnicodeError:
            supplied = b''
        return hmac.compare_digest(supplied, expected)

    def trusted_ingress(self):
        paths = self.headers.get_all('X-Ingress-Path', [])
        return (self.client_address[0] == '172.30.32.2' and len(paths) == 1
                and re.fullmatch(r'/api/hassio_ingress/[A-Za-z0-9_-]+/?', paths[0]) is not None)

    def ingress_origin(self):
        origins = self.headers.get_all('Origin', [])
        hosts = self.headers.get_all('X-Forwarded-Host', [])
        if not hosts:
            hosts = self.headers.get_all('Host', [])
        if (len(origins) != 1 or len(hosts) != 1
                or self.headers.get_all('Sec-Fetch-Site', []) != ['same-origin']):
            return False
        try:
            origin = urlsplit(origins[0])
            host = urlsplit('//' + hosts[0])
            return (origin.scheme in ('http', 'https') and bool(origin.hostname)
                    and not origin.username and not origin.password and not origin.path
                    and not origin.query and not origin.fragment and bool(host.hostname)
                    and not host.username and not host.password and not host.path
                    and not host.query and not host.fragment and origin.hostname == host.hostname
                    and origin.port == host.port)
        except ValueError:
            return False

    def page(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(LOGIN_PAGE)))
        self.send_header('Cache-Control', 'no-store')
        script = LOGIN_PAGE.split(b'<script>', 1)[1].split(b'</script>', 1)[0]
        digest = base64.b64encode(hashlib.sha256(script).digest()).decode('ascii')
        self.send_header('Content-Security-Policy',
                         "default-src 'none'; script-src 'sha256-" + digest + "'; "
                         "connect-src 'self'; frame-ancestors 'self'; base-uri 'none'; form-action 'self'")
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(LOGIN_PAGE)
        self.close_connection = True

    def login_state(self, start=False):
        try:
            from finance_control import assistant_login
            result = assistant_login.start() if start else assistant_login.state()
            if type(result) is not dict or result.get('state') not in ('idle', 'pending', 'completed', 'failed', 'expired'):
                raise ValueError('invalid_state')
            public = {'state': result['state']}
            if result.get('url') == 'https://auth.openai.com/codex/device' and type(result.get('code')) is str:
                if re.fullmatch(r'[A-Z0-9]{4}-[A-Z0-9]{4,5}', result['code']):
                    public.update(url=result['url'], code=result['code'])
            self.reply(200, public)
        except Exception:
            self.reply(503, {'error': 'service_unavailable'})

    def do_GET(self):
        if self.path == '/health':
            self.reply(200, {'status': 'ok'})
        elif self.path == '/':
            if not self.trusted_ingress():
                self.reply(403, {'error': 'forbidden'})
            else:
                self.page()
        elif self.path == '/ingress/login-state':
            if not self.trusted_ingress():
                self.reply(403, {'error': 'forbidden'})
            else:
                self.login_state()
        elif self.path == '/api/status':
            if not self.authorized():
                self.reply(401, {'error': 'unauthorized'})
                return
            try:
                from finance_control import assistant_runtime
                status = assistant_runtime.status()
                if (type(status) is not dict or type(status.get('codex_authenticated')) is not bool
                        or type(status.get('qwen_ready')) is not bool
                        or status.get('preferred_engine') not in ('codex_cli', 'qwen', 'none')):
                    raise ValueError('invalid_status')
                self.reply(200, {key: status[key] for key in
                                 ('codex_authenticated', 'qwen_ready', 'preferred_engine')})
            except Exception:
                self.reply(503, {'error': 'service_unavailable'})
        else:
            self.reply(404, {'error': 'not_found'})

    def do_POST(self):
        if self.path not in ('/api/classify', '/api/setup-model', '/api/self-test', '/ingress/login-start'):
            self.reply(404, {'error': 'not_found'})
            return
        if self.path == '/ingress/login-start':
            if not self.trusted_ingress() or not self.ingress_origin():
                self.reply(403, {'error': 'forbidden'})
                return
        elif not self.authorized():
            self.reply(401, {'error': 'unauthorized'})
            return
        lengths = self.headers.get_all('Content-Length', [])
        if (self.headers.get_all('Transfer-Encoding', []) or len(lengths) != 1
                or not lengths[0].isascii() or not lengths[0].isdigit()):
            self.reply(400, {'error': 'invalid_length'})
            return
        if len(lengths[0]) > 5 or int(lengths[0]) > MAX_BODY:
            self.reply(413, {'error': 'body_too_large'})
            return
        length = int(lengths[0])
        if not REQUEST_LOCK.acquire(blocking=False):
            self.reply(429, {'error': 'busy'})
            return
        try:
            self.connection.settimeout(15)
            raw = self.rfile.read(length)
            if len(raw) != length:
                self.reply(400, {'error': 'invalid_body'})
                return
            payload = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object,
                                 parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))
            if self.path in ('/api/setup-model', '/api/self-test', '/ingress/login-start'):
                if type(payload) is not dict or set(payload) != {'confirmed'} or payload['confirmed'] is not True:
                    raise ValueError('confirmation_required')
                if self.path == '/ingress/login-start':
                    self.login_state(start=True)
                    return
                from finance_control import assistant_runtime
                if self.path == '/api/self-test':
                    # Fixed synthetic input only: never force fallback for an owner's booking.
                    def synthetic_limit(_):
                        raise BackendFailure('usage_limit_reached')
                    sample = {'task': 'classify', 'transaction': {
                        'counterparty': 'Synthetic supermarket',
                        'description': 'Synthetic grocery purchase', 'amount': '-12.34',
                        'currency': 'EUR', 'date': '2026-01-01', 'direction': 'expense'},
                        'allowed_categories': [{'id': 'groceries', 'label': 'Groceries'},
                                               {'id': 'travel', 'label': 'Travel'}]}
                    result = classify(sample, codex=synthetic_limit, qwen=assistant_runtime.qwen)
                    self.reply(200, {'synthetic': True, **result})
                    return
                assistant_runtime.prepare_qwen()
                self.reply(200, {'status': 'prepared'})
                return
            from finance_control import assistant_runtime
            proposal = classify(payload, codex=assistant_runtime.codex, qwen=assistant_runtime.qwen)
            self.reply(200, proposal)
        except (socket.timeout, TimeoutError):
            self.reply(504, {'error': 'timeout'})
        except (UnicodeError, ValueError, RecursionError):
            self.reply(400, {'error': 'invalid_request'})
        except BackendFailure as exc:
            code = exc.code if exc.code in PUBLIC_BACKEND_ERRORS else 'backend_unavailable'
            result = {'error': code}
            if exc.fallback_reason is not None:
                result['fallback_reason'] = exc.fallback_reason
            self.reply(504 if code == 'timeout' else 503, result)
        except Exception:
            self.reply(503, {'error': 'service_unavailable'})
        finally:
            REQUEST_LOCK.release()


def main():
    token = read_token()
    server = ThreadingHTTPServer(('0.0.0.0', 8786), Handler)
    server.assistant_token = token
    server.serve_forever()


if __name__ == '__main__':
    main()
