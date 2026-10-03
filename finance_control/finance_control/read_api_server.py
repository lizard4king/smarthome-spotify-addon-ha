"""Small authenticated GET-only HTTP interface for local finance summaries."""
import argparse
from collections import defaultdict, deque
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from pathlib import Path
import sys
from threading import Lock
import time
from urllib.parse import parse_qs, urlsplit

from .security.api_token import ApiTokenError, WindowsApiTokenStore, generate_token


class InMemoryRateLimiter:
    """Per-client sliding-window limiter; client addresses are never logged."""

    def __init__(self, limit=60, window_seconds=60, clock=time.monotonic):
        if limit < 1 or window_seconds <= 0:
            raise ValueError('Invalid limiter settings')
        self.limit = limit
        self.window_seconds = window_seconds
        self.clock = clock
        self._events = defaultdict(deque)
        self._lock = Lock()

    def retry_after(self, client):
        with self._lock:
            now = self.clock()
            events = self._events[client]
            while events and now - events[0] >= self.window_seconds:
                events.popleft()
            if len(events) >= self.limit:
                return max(1, int(self.window_seconds - (now - events[0]) + 0.999))
            events.append(now)
            return None


def _period(value):
    try:
        parsed = datetime.strptime(value, '%Y-%m')
    except (TypeError, ValueError):
        raise ValueError('invalid period') from None
    if parsed.strftime('%Y-%m') != value:
        raise ValueError('invalid period')
    return value


def _as_of(value):
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError('invalid date') from None
    if parsed.isoformat() != value:
        raise ValueError('invalid date')
    return value


def make_handler(api, token, limiter=None):
    """Build an isolated handler; api implements FinanceReadApi's four methods."""
    secret = token.value if hasattr(token, 'value') else token
    if not isinstance(secret, str) or len(secret) < 32:
        raise ValueError('A valid API token is required')
    limiter = limiter or InMemoryRateLimiter()

    class Handler(BaseHTTPRequestHandler):
        server_version = ''
        sys_version = ''

        def log_message(self, fmt, *args):
            # Requests may contain private query values; deliberately emit no logs.
            return

        def send_response(self, code, message=None):
            # BaseHTTPRequestHandler normally emits a Server header identifying Python.
            self.send_response_only(code, message)
            self.send_header('Date', self.date_time_string())

        def _send(self, status, payload, extra_headers=None):
            body = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            if extra_headers:
                for name, value in extra_headers.items():
                    self.send_header(name, str(value))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            retry = limiter.retry_after(self.client_address[0])
            if retry is not None:
                self._send(429, {'error': 'rate_limited'}, {'Retry-After': retry})
                return
            supplied = self.headers.get('Authorization', '').encode('utf-8')
            expected = ('Bearer ' + secret).encode('utf-8')
            if not hmac.compare_digest(supplied, expected):
                self._send(401, {'error': 'unauthorized'}, {'WWW-Authenticate': 'Bearer'})
                return
            try:
                parsed = urlsplit(self.path)
                query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
                routes = {
                    '/api/v1/accounts': ('as_of', lambda q: api.accounts(_as_of(q or date.today().isoformat()))),
                    '/api/v1/monthly-status': ('period', lambda q: api.monthly_status(_period(q))),
                    '/api/v1/amazon/summary': (None, lambda q: api.amazon_summary()),
                    '/api/v1/sources/status': (None, lambda q: api.source_status()),
                }
                route = routes.get(parsed.path)
                if route is None:
                    self._send(404, {'error': 'not_found'})
                    return
                parameter, callback = route
                if parameter is None:
                    if parsed.query:
                        raise ValueError('unexpected query')
                    value = None
                else:
                    if set(query) - {parameter} or len(query.get(parameter, [])) > 1:
                        raise ValueError('invalid query')
                    values = query.get(parameter, [])
                    if parameter == 'period' and not values:
                        raise ValueError('missing period')
                    value = values[0] if values else None
                result = callback(value)
                if not isinstance(result, dict):
                    raise TypeError('invalid response')
                self._send(200, result)
            except (ValueError, TypeError):
                self._send(400, {'error': 'invalid_request'})
            except Exception:
                self._send(500, {'error': 'internal_error'})

        def _method_not_allowed(self):
            self._send(405, {'error': 'method_not_allowed'}, {'Allow': 'GET'})

        do_POST = _method_not_allowed
        do_PUT = _method_not_allowed
        do_PATCH = _method_not_allowed
        do_DELETE = _method_not_allowed
        do_OPTIONS = _method_not_allowed

        def __getattr__(self, name):
            if name.startswith('do_'):
                return self._method_not_allowed
            raise AttributeError(name)

    return Handler


def make_server(api, token, host='127.0.0.1', port=8786, limiter=None):
    """Create the API server. Remote binding is deliberately unsupported."""
    if host not in ('127.0.0.1', 'localhost'):
        raise ValueError('The read API may bind only to loopback.')
    return ThreadingHTTPServer((host, port), make_handler(api, token, limiter))


def _default_database(data_directory=None):
    if data_directory:
        root = Path(data_directory).resolve()
    else:
        local = os.environ.get('LOCALAPPDATA')
        if not local:
            raise ValueError('Lokaler Datenordner nicht gefunden.')
        root = (Path(local) / 'FinanceControl' / 'data').resolve()
    database = root / 'finance.sqlite'
    if not root.is_dir() or not database.is_file():
        raise ValueError('Initialisierte lokale Datenbank nicht gefunden.')
    return database


def main(argv=None):
    parser = argparse.ArgumentParser(description='Finance Control Read API (nur lokal, GET-only)')
    sub = parser.add_subparsers(dest='command', required=True)
    serve = sub.add_parser('serve', help='Read API starten')
    serve.add_argument('--data-directory')
    serve.add_argument('--token-alias', default='home-assistant')
    serve.add_argument('--port', type=int, default=8786)
    create = sub.add_parser('create-token', help='Token lokal erzeugen und speichern')
    create.add_argument('alias')
    remove = sub.add_parser('delete-token', help='Token aus dem Credential Manager entfernen')
    remove.add_argument('alias')
    args = parser.parse_args(argv)
    try:
        store = WindowsApiTokenStore()
        if args.command == 'create-token':
            if not sys.stdin.isatty() or not sys.stdout.isatty():
                raise ApiTokenError('Ein lokales interaktives Terminal ist erforderlich.')
            token = generate_token()
            store.save(args.alias, token)
            print('Token (jetzt sicher in Home Assistant übernehmen; er wird nicht erneut angezeigt):')
            print(token.value)
            return 0
        if args.command == 'delete-token':
            store.delete(args.alias)
            print('Token entfernt.')
            return 0
        token = store.load(args.token_alias)
        database = _default_database(args.data_directory)
        from .read_api import FinanceReadApi
        api = FinanceReadApi(database)
        server = make_server(api, token, port=args.port)
        print(f'Read API läuft lokal auf 127.0.0.1:{server.server_port}.', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
            close = getattr(api, 'close', None)
            if close:
                close()
    except (ApiTokenError, ValueError, OSError, ImportError):
        parser.exit(2, 'Read API konnte nicht gestartet werden; lokale Konfiguration prüfen.\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
