"""Opt-in, owner-bound refresh of previously imported bank periods.

Only an explicit manual import can create a binding. Refresh runs in one
process-wide worker, never on construction or on a read-only status request.
"""

from __future__ import annotations

import re
import threading
import time
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .administration import AdministrationError, _id, _revision
from .connectors.server_bank_rules import valid_auth_selection
from .core import Store, valid_identifier


_FINGERPRINT = re.compile(r'[0-9a-f]{64}\Z')
_BINDING_FIELDS = frozenset({'connection_id', 'revision', 'account_id',
                             'account_fingerprint', 'tan_method', 'tan_medium', 'bank_id'})
_COOLDOWN_SECONDS = 60
_MAX_WAIT_SECONDS = 350
_POLL_SECONDS = 0.2
_GLOBAL_WORKER_LOCK = threading.Lock()
_ACTIVE_LOCK = threading.Lock()
_ACTIVE_CONTEXT = None  # (database, owner, frozenset((connection_id, revision)))
_SAFE_CODES = frozenset({
    'authorization_required', 'auth_rejected', 'bank_read_unavailable', 'bank_read_timeout',
    'bank_read_busy', 'bank_product_unavailable', 'bank_failure',
    'bank_import_unavailable', 'invalid_bank_result', 'invalid_period',
    'stale_revision', 'stale_preview', 'history_gap', 'refresh_interrupted',
    'PREVIOUS_MONTH_UNVERIFIED', 'OPENING_BALANCE_MISMATCH',
    'ACCOUNT_OPENING_MISMATCH', 'ACCOUNT_CURRENCY_MISMATCH',
    'MONTH_ALREADY_ADOPTED', 'MONTH_ALREADY_IMPORTED', 'PERIOD_CONFLICT',
    'PREFIX_CHANGED', 'SAME_DAY_CHANGED', 'LEDGER_PREFIX_CHANGED',
    'LEDGER_CONTROL_CHANGED', 'LEGACY_MONTH_OVERLAP', 'LEGACY_TEXT_MISMATCH',
    'LEGACY_PREFIX_AMBIGUOUS', 'SOURCE_BINDING_REQUIRED',
    'PREVIOUS_MONTH_CHANGED',
})


def _code(value):
    return value if type(value) is str and value in _SAFE_CODES else 'bank_import_unavailable'


def _month_start(day):
    return day.replace(day=1)


def _next_month(start):
    return (start.replace(day=28) + timedelta(days=4)).replace(day=1)


def _month_end(start):
    return _next_month(start) - timedelta(days=1)


class _RefreshError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class ServerBankRefresh:
    """Persistent refresh selections; all bank access remains in the jobs API."""

    def __init__(self, jobs, administration, database, clock=None):
        self.jobs = jobs
        self.administration = administration
        self.database = Path(database).expanduser().resolve()
        self.clock = clock or (lambda: datetime.now(UTC))
        # Apply the versioned schema at server startup, before any preview token.
        with closing(Store(self.database)):
            pass

    def _now(self):
        current = self.clock()
        if type(current) is not datetime or current.tzinfo is None:
            raise ValueError('refresh clock must return an aware datetime')
        return current.astimezone(UTC)

    def _owner(self, actor, db):
        return self.administration._actor(db, actor)['id']

    def _connections(self, actor):
        with self.administration._connection() as db:
            owner = self._owner(actor, db)
            rows = db.execute(
                "SELECT c.id,c.label,c.bank_id,c.revision AS connection_revision,"
                "b.owner_id AS binding_owner,b.revision AS binding_revision,"
                "b.account_id,b.account_fingerprint,b.tan_method,b.tan_medium,"
                "b.last_attempt_at,b.last_success_at,b.last_status,b.last_code,b.last_inserted "
                "FROM app_bank_connections c LEFT JOIN server_bank_refresh_bindings b "
                "ON b.connection_id=c.id "
                "WHERE c.user_id=? AND c.status!='REVOKED' ORDER BY c.rowid",
                (owner,)).fetchall()
        return owner, [dict(row) for row in rows]

    def _valid_binding(self, actor, owner, row):
        if (row['binding_owner'] != owner or row['binding_revision'] != row['connection_revision']
                or row['bank_id'] not in ('ING', 'POSTBANK')):
            return False
        try:
            return self.jobs._check(actor, row['id'], row['connection_revision']) == owner
        except AdministrationError:
            return False

    def _active(self, owner, row):
        with _ACTIVE_LOCK:
            context = _ACTIVE_CONTEXT
            return (context is not None and context[0] == self.database
                    and context[1] == owner
                    and (row['id'], row['connection_revision']) in context[2])

    def _acquire_active(self, owner, candidates):
        global _ACTIVE_CONTEXT
        with _ACTIVE_LOCK:
            if not _GLOBAL_WORKER_LOCK.acquire(blocking=False):
                return False
            _ACTIVE_CONTEXT = (self.database, owner,
                               frozenset((row['id'], row['connection_revision'])
                                         for row, _ in candidates))
            return True

    @staticmethod
    def _clear_active():
        global _ACTIVE_CONTEXT
        with _ACTIVE_LOCK:
            _ACTIVE_CONTEXT = None

    def _view(self, actor, owner, row, *, now, for_start=False):
        active = self._active(owner, row)
        if row['last_status'] == 'running' and not active:
            # The worker may have committed between the list query and this view.
            with self.administration._connection() as db:
                fresh = db.execute(
                    'SELECT last_status,last_code,last_inserted,last_success_at,last_attempt_at '
                    'FROM server_bank_refresh_bindings WHERE connection_id=? AND owner_id=?',
                    (row['id'], owner)).fetchone()
            if fresh is not None:
                row.update(dict(fresh))
        item = {'id': row['id'], 'label': row['label'], 'bank_id': row['bank_id'],
                'status': 'setup_required', 'last_success_at': row['last_success_at']}
        if not self._valid_binding(actor, owner, row):
            return item
        if active or self._active(owner, row):
            item['status'] = 'running'
            return item
        if row['last_status'] == 'running':
            item.update(status='error', code='refresh_interrupted')
            return item
        attempt = row['last_attempt_at']
        if attempt is not None and for_start:
            try:
                elapsed = (now - datetime.fromisoformat(attempt)).total_seconds()
            except ValueError:
                elapsed = 0
            if elapsed < _COOLDOWN_SECONDS:
                item['status'] = 'cooldown'
                if row['last_code']:
                    item['code'] = _code(row['last_code'])
                if (row['last_status'] in ('error', 'setup_required')
                        and type(row['last_inserted']) is int and row['last_inserted'] > 0):
                    item['inserted'] = row['last_inserted']
                return item
        status = row['last_status']
        if status in ('updated', 'setup_required', 'error'):
            item['status'] = status
        else:
            item['status'] = 'error'
            item['code'] = 'bank_import_unavailable'
        if status == 'error' and 'code' not in item:
            item['code'] = _code(row['last_code'])
        if status == 'setup_required' and row['last_code']:
            item['code'] = _code(row['last_code'])
        if (type(row['last_inserted']) is int
                and (status == 'updated' or status in ('error', 'setup_required')
                     and row['last_inserted'] > 0)):
            item['inserted'] = row['last_inserted']
        return item

    def _plan(self, account_id, bank_id, today):
        store = Store(self.database, readonly=True)
        try:
            row = store.db.execute(
                'SELECT p.month_start,p.as_of FROM ing_period_imports p '
                'JOIN bank_source_accounts s ON s.source_key=p.source_key '
                'WHERE p.account_id=? AND s.provider=? '
                'ORDER BY p.month_start DESC LIMIT 1', (account_id, bank_id)).fetchone()
        finally:
            store.close()
        if row is None:
            raise _RefreshError('authorization_required')
        try:
            latest_month = date.fromisoformat(row['month_start'])
            as_of = date.fromisoformat(row['as_of'])
        except (TypeError, ValueError):
            raise _RefreshError('history_gap') from None
        current_month = _month_start(today)
        if (latest_month.day != 1 or latest_month > current_month or as_of < latest_month
                or as_of > min(_month_end(latest_month), today)):
            raise _RefreshError('history_gap')
        planned = []
        month = (latest_month if latest_month == current_month
                 or as_of < _month_end(latest_month) else _next_month(latest_month))
        while month <= current_month:
            if (today - month).days > 90:
                raise _RefreshError('history_gap')
            planned.append((month.isoformat(), min(_month_end(month), today).isoformat()))
            month = _next_month(month)
        return planned

    def remember(self, actor, binding):
        if not _GLOBAL_WORKER_LOCK.acquire(blocking=False):
            raise AdministrationError('bank_read_busy', 409)
        try:
            return self._remember_locked(actor, binding)
        finally:
            _GLOBAL_WORKER_LOCK.release()

    def commit_and_remember(self, actor, data):
        """Reserve refresh exclusion before the one-use manual import commit."""
        if not _GLOBAL_WORKER_LOCK.acquire(blocking=False):
            raise AdministrationError('bank_read_busy', 409)
        try:
            result = self.jobs.commit(actor, data)
            if result.get('status') == 'imported':
                try:
                    self._remember_locked(actor, self.jobs.binding(actor, data['job_id']))
                    result['refresh_configured'] = True
                except Exception:
                    # The ledger commit is already durable; report binding failure honestly.
                    result['refresh_configured'] = False
            return result
        finally:
            _GLOBAL_WORKER_LOCK.release()

    def _remember_locked(self, actor, binding):
        if type(binding) is not dict or set(binding) != _BINDING_FIELDS:
            raise AdministrationError('invalid_action')
        connection_id, revision = _id(binding['connection_id']), _revision(binding['revision'])
        account_id, fingerprint, bank_id = (binding['account_id'], binding['account_fingerprint'],
                                            binding['bank_id'])
        if (not valid_identifier(account_id) or type(fingerprint) is not str
                or _FINGERPRINT.fullmatch(fingerprint) is None
                or bank_id not in ('ING', 'POSTBANK')
                or not valid_auth_selection(bank_id, binding['tan_method'], binding['tan_medium'])):
            raise AdministrationError('invalid_action')
        owner = self.jobs._check(actor, connection_id, revision)
        with self.administration._connection(write=True) as db:
            if self._owner(actor, db) != owner:
                raise AdministrationError('forbidden', 403)
            connection = db.execute(
                "SELECT user_id,bank_id,revision FROM app_bank_connections "
                "WHERE id=? AND status!='REVOKED'", (connection_id,)).fetchone()
            account = db.execute('SELECT kind,currency FROM accounts WHERE id=?',
                                 (account_id,)).fetchone()
            checkpoint = db.execute(
                'SELECT 1 FROM ing_period_imports p '
                'JOIN bank_source_accounts s ON s.source_key=p.source_key '
                'WHERE p.account_id=? AND s.provider=? LIMIT 1',
                (account_id, bank_id)).fetchone()
            if (connection is None or connection['user_id'] != owner
                    or connection['bank_id'] != bank_id or connection['revision'] != revision
                    or account is None or tuple(account) != ('CHECKING', 'EUR')
                    or checkpoint is None):
                raise AdministrationError('invalid_target', 409)
            db.execute(
                'INSERT INTO server_bank_refresh_bindings '
                '(connection_id,owner_id,revision,account_id,account_fingerprint,'
                'tan_method,tan_medium,bank_id,last_attempt_at,last_success_at,'
                'last_status,last_inserted) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) '
                'ON CONFLICT(connection_id) DO UPDATE SET owner_id=excluded.owner_id,'
                'revision=excluded.revision,account_id=excluded.account_id,'
                'account_fingerprint=excluded.account_fingerprint,'
                'tan_method=excluded.tan_method,tan_medium=excluded.tan_medium,'
                'bank_id=excluded.bank_id,last_attempt_at=excluded.last_attempt_at,'
                'last_success_at=excluded.last_success_at,'
                "last_status='updated',last_code=NULL,last_inserted=0",
                (connection_id, owner, revision, account_id, fingerprint,
                 binding['tan_method'], binding['tan_medium'], bank_id,
                 self._now().isoformat(), self._now().isoformat(), 'updated', 0))
        return {'remembered': True}

    def _record(self, row, *, status, now, code=None, inserted=None, attempt=False):
        with self.administration._connection(write=True) as db:
            if attempt:
                db.execute(
                    'UPDATE server_bank_refresh_bindings SET last_attempt_at=?,last_status=?, '
                    'last_code=NULL,last_inserted=NULL WHERE connection_id=? AND owner_id=? AND revision=?',
                    (now.isoformat(), 'running', row['id'], row['binding_owner'],
                     row['binding_revision']))
            else:
                db.execute(
                    'UPDATE server_bank_refresh_bindings SET last_status=?,last_code=?,last_attempt_at=?, '
                    'last_inserted=?,last_success_at=CASE WHEN ? THEN ? ELSE last_success_at END '
                    'WHERE connection_id=? AND owner_id=? AND revision=?',
                    (status, code, now.isoformat(), inserted, status == 'updated', now.isoformat(),
                     row['id'], row['binding_owner'], row['binding_revision']))

    def _run_binding(self, actor, owner, row, planned):
        inserted = 0
        try:
            for month_start, as_of in planned:
                if self.jobs._check(actor, row['id'], row['connection_revision']) != owner:
                    raise _RefreshError('stale_revision')
                request = {
                    'id': row['id'], 'revision': row['connection_revision'],
                    'confirmed': True, 'tan_method': row['tan_method'],
                    'tan_medium': row['tan_medium'], 'action': 'period',
                    'account_fingerprint': row['account_fingerprint'],
                    'account_id': row['account_id'], 'month_start': month_start,
                    'as_of': as_of,
                }
                started = self.jobs.start(actor, request)
                if type(started) is not dict or type(started.get('job_id')) is not str:
                    raise _RefreshError('invalid_bank_result')
                deadline = time.monotonic() + _MAX_WAIT_SECONDS
                while True:
                    if time.monotonic() >= deadline:
                        raise _RefreshError('bank_read_timeout')
                    if self.jobs._check(actor, row['id'], row['connection_revision']) != owner:
                        raise _RefreshError('stale_revision')
                    state = self.jobs.state(actor, {'job_id': started['job_id']})
                    if type(state) is not dict:
                        raise _RefreshError('invalid_bank_result')
                    if state.get('status') == 'running':
                        time.sleep(_POLL_SECONDS)
                        continue
                    if state.get('status') == 'error':
                        raise _RefreshError(_code(state.get('code')))
                    result = state.get('result')
                    if state.get('status') != 'complete' or type(result) is not dict:
                        raise _RefreshError('invalid_bank_result')
                    if result.get('status') in ('needs_method', 'needs_medium'):
                        raise _RefreshError('authorization_required')
                    token = result.get('review_token')
                    if result.get('status') != 'preview' or type(token) is not str:
                        raise _RefreshError('invalid_bank_result')
                    committed = self.jobs.commit(actor, {'job_id': started['job_id'],
                                                         'review_token': token, 'confirmed': True})
                    if (type(committed) is not dict or committed.get('status') != 'imported'
                            or type(committed.get('inserted')) is not int
                            or committed['inserted'] < 0):
                        raise _RefreshError('invalid_bank_result')
                    inserted += committed['inserted']
                    break
            if self.jobs._check(actor, row['id'], row['connection_revision']) != owner:
                raise _RefreshError('stale_revision')
            self._record(row, status='updated', now=self._now(), inserted=inserted)
        except AdministrationError as error:
            self._record(row, status='error', now=self._now(), code=_code(error.code), inserted=inserted)
        except _RefreshError as error:
            status = 'setup_required' if error.code == 'authorization_required' else 'error'
            self._record(row, status=status, now=self._now(), code=_code(error.code), inserted=inserted)
        except Exception:
            self._record(row, status='error', now=self._now(), code='bank_import_unavailable', inserted=inserted)

    def _worker(self, actor, owner, candidates):
        try:
            for row, planned in candidates:
                self._run_binding(actor, owner, row, planned)
        finally:
            self._clear_active()
            _GLOBAL_WORKER_LOCK.release()

    def _response(self, actor, *, for_start=False):
        now = self._now()
        owner, rows = self._connections(actor)
        return owner, rows, [self._view(actor, owner, row, now=now, for_start=for_start)
                             for row in rows]

    def start(self, actor, data):
        if type(data) is not dict or data:
            raise AdministrationError('invalid_action')
        owner, rows, views = self._response(actor, for_start=True)
        candidates = []
        for row, view in zip(rows, views):
            if not self._valid_binding(actor, owner, row) or view['status'] == 'cooldown':
                continue
            try:
                planned = self._plan(row['account_id'], row['bank_id'],
                                     self._now().astimezone(ZoneInfo('Europe/Berlin')).date())
            except _RefreshError as error:
                view.update(status='error', code=_code(error.code))
                continue
            if planned:
                candidates.append((row, planned))
            elif view['status'] not in ('error', 'running'):
                view['status'] = 'updated'
        if not candidates:
            return {'status': 'running' if any(item['status'] == 'running' for item in views)
                    else 'complete', 'connections': views}
        if not self._acquire_active(owner, candidates):
            if any(self._active(owner, row) for row, _ in candidates):
                return {'status': 'running', 'connections': views}
            # The previous worker may have ended between lock acquisition and
            # the active-context read. Give this request one immediate retry.
            if not self._acquire_active(owner, candidates):
                raise AdministrationError('bank_read_busy', 409)
        try:
            for row, _ in candidates:
                self._record(row, status='running', now=self._now(), attempt=True)
            worker = threading.Thread(target=self._worker,
                                      args=(dict(actor), owner, candidates), daemon=True)
            worker.start()
        except Exception:
            self._clear_active()
            _GLOBAL_WORKER_LOCK.release()
            raise AdministrationError('bank_import_unavailable', 503) from None
        ready = {row['id'] for row, _ in candidates}
        for view in views:
            if view['id'] in ready:
                view['status'] = 'running'
                view.pop('code', None)
                view.pop('inserted', None)
        return {'status': 'running', 'connections': views}

    def state(self, actor, data):
        if type(data) is not dict or data:
            raise AdministrationError('invalid_action')
        _, _, views = self._response(actor)
        return {'status': 'running' if any(item['status'] == 'running' for item in views)
                else 'complete', 'connections': views}
