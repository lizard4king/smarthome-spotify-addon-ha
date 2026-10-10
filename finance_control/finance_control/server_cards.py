"""Owner-bound Postbank card setup and short-lived read previews.

SQLite holds only masked card metadata. PAN and optional FinTS request fields
are kept in the encrypted server card store; bank results stay in bounded RAM.
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
from datetime import date

from .administration import AdministrationError, _id, _revision
from .connectors.server_bank_rules import valid_auth_selection
from .security.server_card_store import CardSecret, ServerCardStore


_FIELDS = {
    'card-state': {'id', 'revision'},
    'card-save': {'id', 'revision', 'confirmed', 'account_id', 'card_number',
                  'card_account_number', 'account_fingerprint'},
    'card-delete': {'id', 'revision', 'confirmed', 'card_id'},
    'card-read': {'id', 'revision', 'confirmed', 'card_id', 'tan_method',
                  'tan_medium', 'start', 'end'},
    'card-read-state': {'id', 'revision', 'card_id'},
}
_FINGERPRINT = re.compile(r'[0-9a-f]{64}\Z', re.ASCII)
_MAX_ROWS = 10_000
_MAX_OUTPUT = 8 * 1024 * 1024
_MAX_FINISHED = 8
_TTL = 15 * 60
_SAFE_GATEWAY_CODES = frozenset({
    'invalid_request', 'vault_unavailable', 'authorization_required',
    'auth_rejected', 'bank_failure', 'invalid_bank_result',
    'unsupported_platform', 'unknown_connection', 'stale_revision',
    'bank_read_busy', 'bank_read_timeout', 'bank_read_unavailable',
    'unknown_account', 'bank_product_unavailable',
    'invalid_bank_auth_selection', 'server_banking_unsupported',
    'card_operation_unsupported', 'forbidden', 'invalid_bank',
    'invalid_action', 'confirmation_required',
})


def _real_store():
    from .security.server_vault import APPROVED_DIRECTORY
    return ServerCardStore(APPROVED_DIRECTORY)


def _day(value):
    if type(value) is not str or len(value) != 10:
        raise AdministrationError('invalid_period')
    try:
        day = date.fromisoformat(value)
    except ValueError:
        raise AdministrationError('invalid_period') from None
    if day.isoformat() != value:
        raise AdministrationError('invalid_period')
    return day


def _account_id(value):
    if (type(value) is not str or not 1 <= len(value) <= 120 or
            any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise AdministrationError('invalid_target')
    return value


def _secret(number, account_number, fingerprint):
    if (fingerprint is not None and
            (type(fingerprint) is not str or _FINGERPRINT.fullmatch(fingerprint) is None)):
        raise AdministrationError('invalid_card_metadata')
    secret = CardSecret(number, account_number, fingerprint)
    from .security.server_card_store import _payload
    try:
        _payload(secret)
    except Exception:
        raise AdministrationError('invalid_card_metadata') from None
    return secret


def _bounded_result(raw, request):
    """Fail closed if a gateway returns a secret or an unbounded preview."""
    if type(raw) is not dict:
        raise ValueError('invalid result')
    try:
        serialized = json.dumps(raw, ensure_ascii=False, separators=(',', ':'),
                                allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, UnicodeError):
        raise ValueError('invalid result') from None
    if len(serialized) > _MAX_OUTPUT:
        raise ValueError('oversized result')
    from .server_card_gateway import _validated_output
    result = _validated_output(serialized, request)
    if result['status'] == 'ok' and len(result['transactions']) > _MAX_ROWS:
        raise ValueError('oversized result')
    return result


class ServerCards:
    """Explicit local card mapping and asynchronous, read-only bank preview."""

    def __init__(self, administration, gateway, store_factory=None):
        self.administration = administration
        self.gateway = gateway
        self.store_factory = store_factory or _real_store
        self._lock = threading.Lock()
        self._jobs = {}
        self._active = None

    def _store(self):
        if sys.platform != 'linux':
            raise AdministrationError('server_cards_unsupported', 503)
        try:
            return self.store_factory()
        except Exception:
            raise AdministrationError('server_cards_unavailable', 503) from None

    def _connection(self, db, actor, connection_id, revision=None):
        owner = self.administration._actor(db, actor)
        row = db.execute(
            "SELECT id,user_id,bank_id,revision FROM app_bank_connections "
            "WHERE id=? AND status!='REVOKED'", (connection_id,)).fetchone()
        if row is None:
            raise AdministrationError('unknown_connection', 404)
        if row['user_id'] != owner['id']:
            raise AdministrationError('forbidden', 403)
        if row['bank_id'] != 'POSTBANK':
            raise AdministrationError('invalid_bank', 409)
        if revision is not None and row['revision'] != revision:
            raise AdministrationError('stale_revision', 409)
        return owner, row

    @staticmethod
    def _card(db, connection_id, owner_id, card_id):
        row = db.execute(
            'SELECT card_id,connection_id,owner_id,account_id,masked_number '
            'FROM server_cards WHERE card_id=? AND connection_id=? AND owner_id=?',
            (card_id, connection_id, owner_id)).fetchone()
        if row is None:
            raise AdministrationError('unknown_card', 404)
        return row

    def _state(self, actor, data):
        connection_id, revision = _id(data['id']), _revision(data['revision'])
        with self.administration._connection() as db:
            owner, _ = self._connection(db, actor, connection_id, revision)
            cards = [dict(row) for row in db.execute(
                'SELECT k.card_id,k.connection_id,k.account_id,k.masked_number '
                'FROM server_cards k JOIN app_bank_connections c ON c.id=k.connection_id '
                "WHERE k.owner_id=? AND c.user_id=? AND c.id=? AND c.bank_id='POSTBANK' "
                "AND c.status!='REVOKED' ORDER BY k.rowid",
                (owner['id'], owner['id'], connection_id))]
            targets = [dict(row) for row in db.execute(
                "SELECT id,COALESCE(NULLIF(display_name,''),id) AS name,owner,currency "
                "FROM accounts WHERE kind='CREDIT_CARD' AND currency='EUR' ORDER BY id")]
        if sys.platform == 'linux' and cards:
            store = self._store()
            for card in cards:
                try:
                    present = store.has(card['connection_id'], owner['id'], card['card_id'])
                except Exception:
                    raise AdministrationError('server_cards_unavailable', 503) from None
                if present is not True:
                    raise AdministrationError('server_cards_unavailable', 503)
        return {'supported': sys.platform == 'linux',
                'cards': cards if sys.platform == 'linux' else [], 'targets': targets}

    def _save(self, actor, data):
        connection_id, revision = _id(data['id']), _revision(data['revision'])
        account_id = _account_id(data['account_id'])
        secret = _secret(data['card_number'], data['card_account_number'],
                         data['account_fingerprint'])
        masked = '•••• ' + secret.card_number[-4:]
        attempted = None
        try:
            with self.administration._connection(write=True) as db:
                owner, _ = self._connection(db, actor, connection_id, revision)
                target = db.execute(
                    "SELECT id FROM accounts WHERE id=? AND kind='CREDIT_CARD' AND currency='EUR'",
                    (account_id,)).fetchone()
                if target is None:
                    raise AdministrationError('invalid_target', 409)
                if db.execute(
                        'SELECT 1 FROM server_cards WHERE connection_id=? AND account_id=?',
                        (connection_id, account_id)).fetchone() is not None:
                    raise AdministrationError('duplicate_card_target', 409)
                store = self._store()
                for _ in range(3):
                    card_id = store.new_card_id()
                    _id(card_id)
                    if (db.execute('SELECT 1 FROM server_cards WHERE card_id=?',
                                   (card_id,)).fetchone() is None and
                            store.has(connection_id, owner['id'], card_id) is False):
                        break
                else:
                    raise AdministrationError('server_cards_unavailable', 503)
                attempted = (store, connection_id, owner['id'], card_id)
                store.save(connection_id, owner['id'], card_id, secret)
                db.execute(
                    'INSERT INTO server_cards(card_id,connection_id,owner_id,account_id,masked_number) '
                    'VALUES (?,?,?,?,?)',
                    (card_id, connection_id, owner['id'], account_id, masked))
                result = {'id': connection_id, 'revision': revision,
                          'card_id': card_id, 'account_id': account_id,
                          'masked_number': masked}
        except Exception as error:
            if attempted is not None:
                try:
                    if attempted[0].has(*attempted[1:]) is True:
                        attempted[0].delete(*attempted[1:])
                except Exception:
                    pass
            if isinstance(error, AdministrationError):
                raise
            raise AdministrationError('server_cards_unavailable', 503) from None
        return result

    def _delete(self, actor, data):
        connection_id, revision = _id(data['id']), _revision(data['revision'])
        card_id = _id(data['card_id'])
        with self.administration._connection(write=True) as db:
            owner, _ = self._connection(db, actor, connection_id, revision)
            self._card(db, connection_id, owner['id'], card_id)
            store = self._store()
            try:
                store.delete(connection_id, owner['id'], card_id)
            except Exception:
                raise AdministrationError('server_cards_unavailable', 503) from None
            db.execute('DELETE FROM server_cards WHERE card_id=?', (card_id,))
            with self._lock:
                self._jobs.pop(card_id, None)
                # Keep _active until the in-flight gateway thread exits. Deleting
                # a card must not permit a parallel bank request to start.
            return {'id': connection_id, 'revision': revision,
                    'card_id': card_id, 'deleted': True}

    def _prune(self):
        now = time.monotonic()
        for card_id, job in list(self._jobs.items()):
            if job['finished_at'] is not None and now - job['finished_at'] >= _TTL:
                del self._jobs[card_id]
        finished = sorted((job for job in self._jobs.values() if job['finished_at'] is not None),
                          key=lambda job: job['finished_at'])
        for job in finished[:-_MAX_FINISHED]:
            del self._jobs[job['card_id']]

    def _run(self, card_id, actor, request, expected_mask):
        try:
            result = _bounded_result(self.gateway.read(actor, request), request)
            if result['status'] == 'ok' and result['card_masked'] != expected_mask:
                raise ValueError('card mismatch')
            if result['status'] == 'error':
                status, value = 'error', {'code': result['code']}
            elif result['status'] in ('needs_method', 'needs_medium'):
                status, value = result['status'], {'options': result['options']}
            else:
                status, value = 'complete', {'result': result}
        except AdministrationError as error:
            status, value = 'error', {'code': error.code if error.code in _SAFE_GATEWAY_CODES
                                     else 'bank_read_unavailable'}
        except Exception:
            status, value = 'error', {'code': 'bank_read_unavailable'}
        with self._lock:
            job = self._jobs.get(card_id)
            if job is not None and job['request'] == request:
                job.update(status=status, finished_at=time.monotonic(), **value)
            if self._active == card_id:
                self._active = None
            self._prune()

    def _read(self, actor, data):
        connection_id, revision, card_id = (_id(data['id']), _revision(data['revision']),
                                            _id(data['card_id']))
        start, end = _day(data['start']), _day(data['end'])
        if (start > end or end > date.today() or (end - start).days > 366 or
                not valid_auth_selection(
                    'POSTBANK', data['tan_method'], data['tan_medium'])):
            raise AdministrationError('invalid_request')
        with self.administration._connection() as db:
            owner, _ = self._connection(db, actor, connection_id, revision)
            card = self._card(db, connection_id, owner['id'], card_id)
            store = self._store()
            try:
                present = store.has(connection_id, owner['id'], card_id)
            except Exception:
                raise AdministrationError('server_cards_unavailable', 503) from None
            if present is not True:
                raise AdministrationError('server_cards_unavailable', 503)
        with self._lock:
            self._prune()
            if self._active is not None:
                raise AdministrationError('bank_read_busy', 409)
            request = dict(data)
            self._jobs[card_id] = {
                'card_id': card_id, 'connection_id': connection_id,
                'owner_id': owner['id'], 'revision': revision,
                'request': request, 'status': 'running', 'finished_at': None,
            }
            self._active = card_id
            try:
                thread = threading.Thread(target=self._run,
                                          args=(card_id, dict(actor), request,
                                                card['masked_number']),
                                          daemon=True)
                thread.start()
            except Exception:
                del self._jobs[card_id]
                self._active = None
                raise AdministrationError('bank_read_unavailable', 503) from None
        return {'card_id': card_id, 'status': 'running'}

    def _read_state(self, actor, data):
        connection_id, revision, card_id = (_id(data['id']), _revision(data['revision']),
                                            _id(data['card_id']))
        with self.administration._connection() as db:
            owner, _ = self._connection(db, actor, connection_id, revision)
            self._card(db, connection_id, owner['id'], card_id)
            with self._lock:
                self._prune()
                job = self._jobs.get(card_id)
                if (job is None or job['connection_id'] != connection_id or
                        job['owner_id'] != owner['id'] or job['revision'] != revision):
                    return {'card_id': card_id, 'status': 'idle'}
                response = {'card_id': card_id, 'status': job['status']}
                if job['status'] == 'complete':
                    response['result'] = job['result']
                elif job['status'] == 'error':
                    response['code'] = job['code']
                elif job['status'] in ('needs_method', 'needs_medium'):
                    response['options'] = job['options']
                return response

    def cleanup_connections(self, connections):
        """Purge secrets before revoke; caller supplies DB-verified connections.

        This hook may run inside the administration writer transaction. Historical
        masked metadata remains in SQLite but is hidden by the active-connection
        join and cannot authorize a read.
        """
        if not connections:
            return
        if sys.platform != 'linux':
            return
        checked = []
        for connection in connections:
            if type(connection) is not dict:
                raise AdministrationError('invalid_action')
            checked.append((_id(connection.get('id')), _id(connection.get('user_id'))))
        store = self._store()
        for connection_id, owner_id in checked:
            try:
                store.delete_connection(connection_id, owner_id)
            except Exception:
                raise AdministrationError('server_cards_unavailable', 503) from None
        with self._lock:
            for job_id, job in list(self._jobs.items()):
                if (job['connection_id'], job['owner_id']) in checked:
                    del self._jobs[job_id]

    def dispatch(self, actor, action, data):
        if action not in _FIELDS or type(data) is not dict or set(data) != _FIELDS[action]:
            raise AdministrationError('invalid_action')
        if action == 'card-state':
            return self._state(actor, data)
        if action == 'card-read-state':
            return self._read_state(actor, data)
        if data['confirmed'] is not True:
            raise AdministrationError('confirmation_required')
        if action == 'card-save':
            return self._save(actor, data)
        if action == 'card-delete':
            return self._delete(actor, data)
        return self._read(actor, data)
