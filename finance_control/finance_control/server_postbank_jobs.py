"""Owner-bound, bounded Postbank preview jobs with an explicit one-use commit."""

from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
import threading
import time
from collections import Counter
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from .administration import AdministrationError, _id, _revision
from .connectors.fints_readonly import Balance
from .connectors.ing_period_snapshot import PeriodSnapshot, archive_period
from .core import Store
from .ing_period_import import PeriodImportError, import_period_archive, preview_period_import
from .monthly_archive import archive_monthly_snapshot
from .statement_model import MonthlySnapshot, StatementError


_FIELDS = {'id', 'revision', 'confirmed', 'tan_method', 'tan_medium', 'action',
           'account_fingerprint', 'account_id', 'month_start', 'as_of'}
_TTL = 15 * 60
_MAX_FINISHED = 8
_MAX_PREVIEW_ROWS = 50
_FP = re.compile(r'[0-9a-f]{64}\Z')
_MASK = re.compile(r'••••(?:[A-Za-z0-9]{4})?\Z')
_SAFE_ERRORS = frozenset({
    'bank_read_unavailable', 'bank_read_timeout', 'server_banking_unsupported',
    'bank_product_unavailable', 'invalid_bank_auth_selection', 'bank_read_busy',
    'invalid_request', 'vault_unavailable', 'authorization_required',
    'bank_failure', 'invalid_bank_result', 'unknown_account',
    'unsupported_platform', 'unknown_connection', 'forbidden',
    'stale_revision', 'unauthorized', 'invalid_bank',
    'LEGACY_PREFIX_AMBIGUOUS', 'LEGACY_TEXT_MISMATCH', 'LEGACY_MONTH_OVERLAP',
    'PREVIOUS_MONTH_UNVERIFIED', 'SOURCE_BINDING_REQUIRED',
    'OPENING_BALANCE_MISMATCH', 'ACCOUNT_OPENING_MISMATCH',
    'ACCOUNT_CURRENCY_MISMATCH', 'MONTH_ALREADY_ADOPTED',
    'MONTH_ALREADY_IMPORTED', 'LEDGER_PREFIX_CHANGED', 'LEDGER_CONTROL_CHANGED',
    'PREFIX_CHANGED', 'SAME_DAY_CHANGED', 'PERIOD_CONFLICT',
    'INITIAL_ARCHIVE_MISMATCH', 'LEGACY_PREFIX_MISMATCH',
    'LEGACY_PREFIX_ALREADY_BOUND', 'CHECKPOINT_INVALID',
    'invalid_target', 'invalid_period', 'stale_preview',
})


def _fail(code='bank_import_unavailable', status=503):
    raise AdministrationError(code, status)


def _date(value):
    if type(value) is not str or len(value) != 10:
        raise AdministrationError('invalid_action')
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise AdministrationError('invalid_action') from None
    if parsed.isoformat() != value:
        raise AdministrationError('invalid_action')
    return parsed


def _masked(source):
    suffix = ''.join(char for char in source if char.isalnum())[-4:]
    return '••••' + suffix


def _cash(value):
    return format(value, '.2f')


def _text(value):
    return ' '.join(value.split()).casefold() if type(value) is str else ''


def _safe_option_result(raw):
    kind = raw['status']
    options = raw.get('options')
    if set(raw) != {'status', 'options'} or type(options) is not list or not 1 <= len(options) <= 20:
        _fail()
    method = kind == 'needs_method'
    clean = []
    seen = set()
    for option in options:
        fields = {'key', 'label'} if method else {'label'}
        if type(option) is not dict or set(option) != fields:
            _fail()
        label = option['label']
        if type(label) is not str or not 1 <= len(label) <= 80 or not label.isprintable():
            _fail()
        key = option['key'] if method else label
        if (key in seen or (method and (type(key) is not str
                                     or re.fullmatch(r'[0-9]{1,8}', key) is None))):
            _fail()
        seen.add(key)
        clean.append({'key': key, 'label': label} if method else {'label': label})
    return {'status': kind, 'options': clean}


def _safe_accounts(raw):
    accounts = raw.get('accounts')
    if set(raw) != {'status', 'accounts'} or type(accounts) is not list or len(accounts) > 20:
        _fail()
    clean = []
    seen = set()
    for account in accounts:
        if type(account) is not dict or set(account) != {'fingerprint', 'masked_account'}:
            _fail()
        fingerprint, masked = account['fingerprint'], account['masked_account']
        if (type(fingerprint) is not str or _FP.fullmatch(fingerprint) is None
                or fingerprint in seen or type(masked) is not str
                or _MASK.fullmatch(masked) is None):
            _fail()
        seen.add(fingerprint)
        clean.append({'fingerprint': fingerprint, 'masked_account': masked})
    return {'status': 'ok', 'accounts': clean}


def _database_digest(database):
    """Hash a consistent SQLite snapshot, including WAL and dependent tables."""
    original = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)
    snapshot = sqlite3.connect(':memory:')
    try:
        original.backup(snapshot)
        return hashlib.sha256(snapshot.serialize()).hexdigest()
    finally:
        snapshot.close()
        original.close()


def _target(database, account_id):
    store = Store(database, readonly=True)
    try:
        row = store.db.execute(
            'SELECT id,display_name,owner,currency,kind FROM accounts WHERE id=?',
            (account_id,)).fetchone()
        if row is None or row['kind'] != 'CHECKING':
            raise AdministrationError('invalid_target')
        return {'id': row['id'], 'name': row['display_name'] or row['id'],
                'owner': row['owner'], 'currency': row['currency']}
    finally:
        store.close()


def _legacy_prefix(database, account_id, period):
    """Match only a unique complete current-month prefix; never infer identities."""
    store = Store(database, readonly=True)
    try:
        checkpoint = store.db.execute(
            'SELECT 1 FROM ing_period_imports WHERE account_id=? AND month_start=?',
            (account_id, period.month_start.isoformat())).fetchone()
        if checkpoint is not None:
            return ()
        existing = store.db.execute(
            'SELECT t.external_id,t.date,t.amount,t.currency,c.description '
            'FROM transactions t LEFT JOIN transaction_context c '
            'ON c.account_id=t.account_id AND c.external_id=t.external_id '
            'WHERE t.account_id=? AND t.date>=? AND t.date<=?',
            (account_id, period.month_start.isoformat(), period.as_of.isoformat())).fetchall()
        if not existing:
            return ()
        if len(existing) > len(period.rows):
            _fail('LEGACY_MONTH_OVERLAP', 409)
        bank_keys = [(row.booked_on.isoformat(), _cash(row.amount), row.currency)
                     for row in period.rows]
        counts = Counter(bank_keys)
        bank_by_key = dict(zip(bank_keys, period.rows))
        by_key = {}
        for row in existing:
            key = (row['date'], _cash(Decimal(row['amount'])), row['currency'])
            if key in by_key or counts[key] != 1:
                _fail('LEGACY_PREFIX_AMBIGUOUS', 409)
            by_key[key] = row['external_id']
        prefix = bank_keys[:len(existing)]
        if len(set(prefix)) != len(prefix) or set(prefix) != set(by_key):
            _fail('LEGACY_MONTH_OVERLAP', 409)
        for row in existing:
            key = (row['date'], _cash(Decimal(row['amount'])), row['currency'])
            bank = bank_by_key[key]
            bank_description = _text(bank.description)
            local_description = _text(row['description'])
            merged = _text(' | '.join(part for part in (bank.description, bank.booking_text)
                                      if part)) if bank.booking_text != bank.description else bank_description
            if (not bank_description or not local_description
                    or local_description not in {bank_description, merged}):
                _fail('LEGACY_TEXT_MISMATCH', 409)
        return tuple(by_key[key] for key in prefix)
    finally:
        store.close()


class ServerPostbankJobs:
    """The gateway owns bank access; only sanitized DTOs leave this service."""

    def __init__(self, gateway, database):
        self.gateway = gateway
        self.database = Path(database).expanduser().resolve()
        self._lock = threading.Lock()
        self._jobs = {}
        self._active = None

    def _check(self, actor, connection_id, revision):
        try:
            return _id(self.gateway.check(actor, connection_id, revision))
        except AdministrationError:
            raise
        except Exception:
            _fail()

    def _prune(self):
        now = time.monotonic()
        for job_id, job in list(self._jobs.items()):
            if job['finished_at'] is not None and now - job['finished_at'] >= _TTL:
                del self._jobs[job_id]
        finished = sorted((job for job in self._jobs.values() if job['finished_at'] is not None),
                          key=lambda job: job['finished_at'])
        for job in finished[:-_MAX_FINISHED]:
            del self._jobs[job['job_id']]

    def targets(self, actor, data):
        if type(data) is not dict or set(data) != {'id', 'revision'}:
            raise AdministrationError('invalid_action')
        connection_id, revision = _id(data['id']), _revision(data['revision'])
        self._check(actor, connection_id, revision)
        try:
            store = Store(self.database, readonly=True)
            try:
                rows = store.db.execute(
                    "SELECT id,display_name,owner,currency FROM accounts WHERE kind='CHECKING' ORDER BY id")
                targets = [{'id': row['id'], 'name': row['display_name'] or row['id'],
                            'owner': row['owner'], 'currency': row['currency']} for row in rows]
            finally:
                store.close()
            self._check(actor, connection_id, revision)
            return {'accounts': targets}
        except AdministrationError:
            raise
        except Exception:
            _fail()

    def start(self, actor, data):
        if type(data) is not dict or set(data) != _FIELDS or data['confirmed'] is not True:
            raise AdministrationError('invalid_action')
        connection_id, revision = _id(data['id']), _revision(data['revision'])
        owner_id = self._check(actor, connection_id, revision)
        if data['action'] == 'accounts':
            if any(data[key] is not None for key in
                   ('account_fingerprint', 'account_id', 'month_start', 'as_of')):
                raise AdministrationError('invalid_action')
        elif data['action'] == 'period':
            if (type(data['account_fingerprint']) is not str
                    or _FP.fullmatch(data['account_fingerprint']) is None
                    or type(data['account_id']) is not str):
                raise AdministrationError('invalid_action')
            try:
                start, end = _date(data['month_start']), _date(data['as_of'])
            except AdministrationError:
                raise AdministrationError('invalid_period') from None
            today = date.today()
            if (start.day != 1 or (start.year, start.month) != (end.year, end.month)
                    or end > today or (today - start).days > 90):
                raise AdministrationError('invalid_period')
            _target(self.database, data['account_id'])
        else:
            raise AdministrationError('invalid_action')
        with self._lock:
            self._prune()
            if self._active is not None:
                raise AdministrationError('bank_read_busy', 409)
            job_id = uuid4().hex
            self._jobs[job_id] = {'job_id': job_id, 'owner_id': owner_id,
                                  'connection_id': connection_id, 'revision': revision,
                                  'status': 'running', 'finished_at': None,
                                  'account_id': data['account_id'], 'committing': False,
                                  'committed': False}
            self._active = job_id
            request = {key: value for key, value in data.items() if key != 'account_id'}
            try:
                worker = threading.Thread(target=self._run,
                                          args=(job_id, dict(actor), request), daemon=True)
                worker.start()
            except Exception:
                del self._jobs[job_id]
                self._active = None
                _fail()
            return {'job_id': job_id, 'status': 'running'}

    def _run(self, job_id, actor, request):
        try:
            raw = self.gateway.read(actor, request)
            with self._lock:
                job = self._jobs.get(job_id)
                account_id = job['account_id'] if job else None
                connection_id = job['connection_id'] if job else None
                revision = job['revision'] if job else None
                owner_id = job['owner_id'] if job else None
            if job is None or self._check(actor, connection_id, revision) != owner_id:
                _fail('stale_revision', 409)
            if type(raw) is not dict:
                _fail()
            if raw.get('status') in ('needs_method', 'needs_medium'):
                status, value = 'complete', {'result': _safe_option_result(raw)}
            elif raw.get('status') == 'error':
                status, value = 'error', {'code': raw.get('code') if set(raw) == {'status', 'code'} and raw.get('code') in _SAFE_ERRORS
                                           else 'bank_import_unavailable'}
            elif raw.get('status') == 'ok' and request['action'] == 'accounts':
                status, value = 'complete', {'result': _safe_accounts(raw)}
            elif raw.get('status') == 'ok' and request['action'] == 'period':
                status, value = 'complete', self._preview(raw, account_id)
            else:
                _fail()
        except AdministrationError as error:
            status, value = 'error', {'code': error.code if error.code in _SAFE_ERRORS
                                       else 'bank_import_unavailable'}
        except PeriodImportError as error:
            status, value = 'error', {'code': str(error) if str(error) in _SAFE_ERRORS
                                       else 'bank_import_unavailable'}
        except Exception:
            status, value = 'error', {'code': 'bank_import_unavailable'}
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job.update(status=status, finished_at=time.monotonic(), **value)
            if self._active == job_id:
                self._active = None
            self._prune()

    def _preview(self, raw, account_id):
        if (set(raw) != {'status', 'monthly', 'period', 'balance'}
                or type(raw['monthly']) is not MonthlySnapshot
                or type(raw['period']) is not PeriodSnapshot
                or type(raw['balance']) is not Balance):
            _fail()
        monthly, period, balance = raw['monthly'], raw['period'], raw['balance']
        if (monthly.source_profile != 'POSTBANK' or period.source_profile != 'POSTBANK'
                or monthly.source_account != period.source_account
                or monthly.currency != period.currency
                or monthly.closing_balance != period.opening_balance
                or balance.currency != period.currency):
            _fail()
        target = _target(self.database, account_id)
        if target['currency'] != period.currency:
            _fail('invalid_target', 409)
        archives = self.database.parent / 'bank-archives' / 'POSTBANK'
        # Monthly and period archives have different schemas and must not scan
        # each other's JSON files while checking their immutable history.
        month_path = archive_monthly_snapshot(monthly, archives / 'monthly').path
        period_path = archive_period(period, archives / 'period').path
        digest = _database_digest(self.database)
        legacy = _legacy_prefix(self.database, account_id, period)
        result = preview_period_import(
            self.database, period_path, account_id=account_id,
            confirmed_source_account=period.source_account,
            source_category='Bankabruf', confirmed_legacy_prefix=legacy,
            initial_month_archive=month_path)
        # A preview can take time; do not issue a token for a changed ledger.
        if _database_digest(self.database) != digest:
            _fail('stale_preview', 409)
        token = secrets.token_urlsafe(32)
        dto = {'status': 'preview', 'review_token': token,
               'count': result.count, 'inserted': result.inserted,
               'skipped': result.skipped,
               'opening_balance': _cash(period.opening_balance),
               'closing_balance': _cash(period.closing_balance),
               'currency': period.currency,
               'month_start': period.month_start.isoformat(),
               'as_of': period.as_of.isoformat(), 'account_id': account_id,
               'masked_account': _masked(period.source_account),
               'bank_balance': {'amount': _cash(balance.amount), 'currency': balance.currency,
                                'booked_on': balance.booked_on.isoformat()},
               'control_month': {'start': monthly.period_start.isoformat(),
                                 'end': monthly.period_end.isoformat(),
                                 'opening_balance': _cash(monthly.opening_balance),
                                 'closing_balance': _cash(monthly.closing_balance),
                                 'count': len(monthly.rows)},
               'rows': [{'booked_on': row.booked_on.isoformat(),
                         'value_on': row.value_on.isoformat(),
                         'counterparty': row.counterparty,
                         'description': row.description,
                         'booking_text': row.booking_text,
                         'amount': _cash(row.amount), 'currency': row.currency}
                        for row in period.rows[:_MAX_PREVIEW_ROWS]]}
        return {'result': dto, 'review_token': token, 'db_digest': digest,
                'period_archive': period_path, 'monthly_archive': month_path,
                'source_account': period.source_account, 'legacy_prefix': legacy}

    def _job(self, actor, job_id):
        job_id = _id(job_id)
        with self._lock:
            self._prune()
            job = self._jobs.get(job_id)
            if job is None:
                raise AdministrationError('unknown_job', 404)
            owner_id, connection_id, revision = (job['owner_id'], job['connection_id'],
                                                 job['revision'])
        if self._check(actor, connection_id, revision) != owner_id:
            raise AdministrationError('forbidden', 403)
        return job

    def state(self, actor, data):
        if type(data) is not dict or set(data) != {'job_id'}:
            raise AdministrationError('invalid_action')
        job = self._job(actor, data['job_id'])
        with self._lock:
            return {'job_id': job['job_id'], 'status': job['status'],
                    **({'result': job['result']} if job['status'] == 'complete' else {}),
                    **({'code': job['code']} if job['status'] == 'error' else {})}

    def commit(self, actor, data):
        if type(data) is not dict or set(data) != {'job_id', 'review_token', 'confirmed'}:
            raise AdministrationError('invalid_action')
        if data['confirmed'] is not True:
            raise AdministrationError('confirmation_required')
        job = self._job(actor, data['job_id'])
        with self._lock:
            if (job['status'] != 'complete' or job['committing'] or job['committed']
                    or 'period_archive' not in job or type(data['review_token']) is not str
                    or not secrets.compare_digest(data['review_token'], job['review_token'])):
                raise AdministrationError('invalid_review_token', 409)
            job['committing'] = True
            job['committed'] = True
        try:
            self._check(actor, job['connection_id'], job['revision'])
            if _database_digest(self.database) != job['db_digest']:
                raise AdministrationError('stale_preview', 409)
            backup_dir = self.database.parent / 'bank-backups'
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_path = backup_dir / ('postbank-before-import-' + uuid4().hex + '.sqlite')
            store = Store(self.database, readonly=True)
            try:
                store.backup(backup_path)
            finally:
                store.close()
            self._check(actor, job['connection_id'], job['revision'])
            if _database_digest(self.database) != job['db_digest']:
                raise AdministrationError('stale_preview', 409)
            writable = Store(self.database)
            try:
                result = import_period_archive(
                    writable, job['period_archive'], account_id=job['account_id'],
                    confirmed_source_account=job['source_account'],
                    source_category='Bankabruf', confirmed_legacy_prefix=job['legacy_prefix'],
                    initial_month_archive=job['monthly_archive'])
            finally:
                writable.close()
            dto = {'status': 'imported', 'count': result.count, 'inserted': result.inserted,
                   'skipped': result.skipped, 'account_id': job['account_id'],
                   'as_of': job['result']['as_of']}
            with self._lock:
                job['result'] = dto
            return dto
        except AdministrationError:
            raise
        except PeriodImportError as error:
            _fail(str(error) if str(error) in _SAFE_ERRORS else 'bank_import_unavailable', 409)
        except Exception:
            _fail()
        finally:
            with self._lock:
                job['committing'] = False
