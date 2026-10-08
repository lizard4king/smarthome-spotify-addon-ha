"""Bounded unattended ING import with verified monthly control balances."""

import argparse
from contextlib import contextmanager
from datetime import date, timedelta
from calendar import monthrange
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import uuid

from finance_control.bank_archive import _directory
from finance_control.connectors.fints_readonly import (
    AccountRef, BankErrorCode, BankReadError, ReadOperation, create_reader,
)
from finance_control.connectors.probe import _registration
from finance_control.monthly_archive import archive_monthly_snapshot, validate_snapshot
from finance_control.security.credentials import WindowsCredentialStore
from finance_control.statement_import import _snapshot, import_monthly_archive, preview_monthly_import
from finance_control.statement_model import MonthlySnapshot


class SyncError(RuntimeError):
    """Fixed, private-data-free failure category."""


_FIELDS = {'schema', 'enabled', 'alias', 'registration_file', 'database',
           'archive_directory', 'state_file', 'account_id', 'bank_account',
           'source_account', 'mode', 'source_category', 'start_month',
           'legacy_prefix_ids', 'initial_month_archive'}
_REQUIRED = _FIELDS - {'source_category', 'start_month', 'legacy_prefix_ids', 'initial_month_archive'}
_REPOSITORY = Path(__file__).resolve().parents[3]
_SAFE_REASONS = frozenset({
    'IN_PROGRESS', 'FAILED', 'BANK_FAILED', 'AUTH_REJECTED', 'ONLINE_LOGIN_REQUIRED',
    'CREDENTIALS_REJECTED', 'AUTH_TEMPORARY', 'SCA_REQUIRED', 'HISTORY_GAP',
    'START_NOT_REACHED', 'ACCOUNT_SELECTION_FAILED', 'SNAPSHOT_INVALID',
    'SOURCE_BINDING_REQUIRED', 'PREVIOUS_MONTH_UNVERIFIED', 'OPENING_BALANCE_MISMATCH',
    'PREFIX_CHANGED', 'PERIOD_CONFLICT', 'LEDGER_PREFIX_CHANGED', 'LEDGER_CONTROL_CHANGED',
    'LEGACY_MONTH_OVERLAP', 'LEGACY_PREFIX_MISMATCH', 'IMPORT_FAILED', 'BACKUP_INVALID',
    'SOURCE_NOT_ADOPTED', 'INVALID_TARGET', 'INITIAL_ARCHIVE_MISMATCH',
    'ADOPTION_MISMATCH', 'STATE_WRITE_FAILED', 'ACCOUNT_OPENING_MISMATCH',
    'SOURCE_CONFIRMATION_REQUIRED', 'ACCOUNT_INVALID', 'CATEGORY_INVALID',
    'LEGACY_PREFIX_INVALID', 'TRANSACTION_ACTIVE', 'INITIAL_ARCHIVE_INVALID',
    'ACCOUNT_CURRENCY_MISMATCH', 'MONTH_ALREADY_ADOPTED', 'MONTH_ALREADY_IMPORTED',
    'LEGACY_PREFIX_ALREADY_BOUND', 'CHECKPOINT_INVALID', 'SAME_DAY_CHANGED',
    'IMPORT_INCOMPLETE',
})


def _external_path(value, *, existing=False, directory=False):
    if type(value) is not str or not 1 <= len(value) <= 1024:
        raise SyncError('INVALID_CONFIG')
    raw = Path(value).expanduser()
    if (not raw.is_absolute() or any(part in ('.', '..') for part in raw.parts)
            or any(part.is_symlink() for part in (raw, *raw.parents))):
        raise SyncError('INVALID_CONFIG')
    try:
        path = _directory(raw if directory else raw.parent)
        path = path if directory else path / raw.name
        resolved = path.resolve(strict=existing)
        if _REPOSITORY == resolved or _REPOSITORY in resolved.parents:
            raise SyncError('INVALID_CONFIG')
        if existing and (not resolved.is_file() or resolved.is_symlink()):
            raise SyncError('INVALID_CONFIG')
        if not existing and resolved.exists() and resolved.is_symlink():
            raise SyncError('INVALID_CONFIG')
        return resolved
    except (OSError, ValueError):
        raise SyncError('INVALID_CONFIG') from None


def _config(path):
    source = _external_path(path, existing=True)
    try:
        if source.stat().st_size > 8192:
            raise SyncError('INVALID_CONFIG')
        cfg = json.loads(source.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, ValueError):
        raise SyncError('INVALID_CONFIG') from None
    if type(cfg) is not dict or set(cfg) - _FIELDS or not _REQUIRED <= set(cfg):
        raise SyncError('INVALID_CONFIG')
    if type(cfg['schema']) is not int or cfg['schema'] != 1 or type(cfg['enabled']) is not bool:
        raise SyncError('INVALID_CONFIG')
    if type(cfg['alias']) is not str or re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', cfg['alias']) is None:
        raise SyncError('INVALID_CONFIG')
    if type(cfg['account_id']) is not str or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,119}', cfg['account_id']) is None:
        raise SyncError('INVALID_CONFIG')
    if type(cfg['bank_account']) is not str or re.fullmatch(r'[A-Z]{2}[0-9A-Z]{13,32}', cfg['bank_account']) is None:
        raise SyncError('INVALID_CONFIG')
    source_account = cfg['source_account']
    if type(source_account) is not str or not 1 <= len(source_account) <= 120 or any(
            char.isspace() or not char.isprintable() for char in source_account):
        raise SyncError('INVALID_CONFIG')
    if type(cfg['mode']) is not str or cfg['mode'] not in ('preview', 'apply', 'daily-preview', 'daily-apply'):
        raise SyncError('INVALID_CONFIG')
    category = cfg.get('source_category', 'FinTS · ungeprüft')
    if type(category) is not str or not category or category != category.strip() or len(category) > 240 or any(not c.isprintable() for c in category):
        raise SyncError('INVALID_CONFIG')
    cfg['source_category'] = category
    for name in ('registration_file', 'database'):
        cfg[name] = _external_path(cfg[name], existing=True)
    cfg['archive_directory'] = _external_path(cfg['archive_directory'], directory=True)
    cfg['state_file'] = _external_path(cfg['state_file'])
    if (cfg['state_file'].suffix.lower() != '.json' or
            len({source, cfg['state_file'], cfg['database'], cfg['registration_file']}) != 4 or
            not cfg['state_file'].parent.is_dir()):
        raise SyncError('INVALID_CONFIG')
    if cfg['mode'].startswith('daily-'):
        value = cfg.get('start_month')
        if type(value) is not str or re.fullmatch(r'[0-9]{4}-[0-9]{2}', value) is None:
            raise SyncError('INVALID_CONFIG')
        try:
            cfg['start_month'] = date.fromisoformat(value + '-01')
        except ValueError:
            raise SyncError('INVALID_CONFIG') from None
        legacy = cfg.get('legacy_prefix_ids', [])
        if (type(legacy) is not list or len(legacy) > 1000
                or any(type(v) is not str or not 1 <= len(v) <= 256
                       or any(not c.isprintable() for c in v) for v in legacy)
                or len(set(legacy)) != len(legacy)):
            raise SyncError('INVALID_CONFIG')
        cfg['legacy_prefix_ids'] = tuple(legacy)
        initial = cfg.get('initial_month_archive')
        if initial is not None:
            cfg['initial_month_archive'] = _external_path(initial, existing=True)
            from finance_control.monthly_archive import read_monthly_archive
            s = read_monthly_archive(cfg['initial_month_archive'])
            if (s.source_profile != 'ING' or s.source_account != cfg['source_account']
                    or s.period_end != cfg['start_month'] - timedelta(days=1)):
                raise SyncError('INVALID_CONFIG')
    elif any(key in cfg for key in ('start_month', 'legacy_prefix_ids', 'initial_month_archive')):
        raise SyncError('INVALID_CONFIG')
    return cfg


def _closed_month(today=None):
    today = today or date.today()
    end = date(today.year, today.month, 1) - timedelta(days=1)
    return date(end.year, end.month, 1), end


def _source_key(source_account):
    raw = json.dumps(['ING', source_account], separators=(',', ':'), ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest()


def _preflight(cfg, *, binding=False):
    """Open the original strictly read-only; never invoke Store migrations."""
    try:
        db = sqlite3.connect(cfg['database'].as_uri() + '?mode=ro', uri=True)
        try:
            db.execute('PRAGMA query_only=ON')
            account = db.execute('SELECT currency FROM accounts WHERE id=?', (cfg['account_id'],)).fetchone()
            if account is None or type(account[0]) is not str or re.fullmatch(r'[A-Z]{3}', account[0]) is None:
                raise SyncError('INVALID_TARGET')
            if binding:
                key = _source_key(cfg['source_account'])
                direct = db.execute('SELECT provider,account_id FROM bank_source_accounts WHERE source_key=?', (key,)).fetchone()
                reverse = db.execute('SELECT provider,source_key FROM bank_source_accounts WHERE account_id=?', (cfg['account_id'],)).fetchone()
                adopted = db.execute('SELECT 1 FROM bank_monthly_adoptions WHERE source_key=? AND account_id=? LIMIT 1',
                                     (key, cfg['account_id'])).fetchone()
                statement = db.execute('SELECT account_id FROM bank_statement_account_bindings WHERE source_key=?', (key,)).fetchone()
                statement_reverse = db.execute('SELECT source_key FROM bank_statement_account_bindings WHERE account_id=?', (cfg['account_id'],)).fetchone()
                if (direct != ('ING', cfg['account_id']) or reverse != ('ING', key) or adopted is None
                        or statement is not None and statement[0] != cfg['account_id']
                        or statement_reverse is not None and statement_reverse[0] != key):
                    raise SyncError('SOURCE_NOT_ADOPTED')
            return account[0]
        finally:
            db.close()
    except SyncError:
        raise
    except (sqlite3.Error, OSError):
        raise SyncError('INVALID_TARGET') from None


def _adopted_period(cfg, start):
    try:
        db = sqlite3.connect(cfg['database'].as_uri() + '?mode=ro', uri=True)
        try:
            db.execute('PRAGMA query_only=ON')
            row = db.execute('SELECT 1 FROM bank_monthly_adoptions WHERE account_id=? AND source_key=? AND period_start=?',
                             (cfg['account_id'], _source_key(cfg['source_account']), start.isoformat())).fetchone()
            return row is not None
        finally:
            db.close()
    except sqlite3.Error:
        raise SyncError('INVALID_TARGET') from None


def _adoption_replay(cfg, archive_path):
    """Reconcile on a consistent copy; original ledger and adoption stay untouched."""
    from finance_control.bank_source_policy import adopt_monthly_archive
    from finance_control.core import Store
    with tempfile.TemporaryDirectory(prefix='.ing-adoption-preview-', dir=cfg['database'].parent) as temp:
        copy = Path(temp) / 'preview.sqlite'
        _snapshot(cfg['database'], copy)
        store = Store(copy)
        try:
            if adopt_monthly_archive(store, archive_path, cfg['account_id'], cfg['source_account']) != 0:
                raise SyncError('ADOPTION_MISMATCH')
        finally:
            store.close()


def _verified_backup(database, target):
    _snapshot(database, target)
    try:
        db = sqlite3.connect(target.as_uri() + '?mode=ro', uri=True)
        try:
            if db.execute('PRAGMA integrity_check').fetchone() != ('ok',):
                raise SyncError('BACKUP_INVALID')
        finally:
            db.close()
    except sqlite3.Error:
        raise SyncError('BACKUP_INVALID') from None


def _state(path):
    if not path.exists():
        return 'READY'
    try:
        if path.is_symlink() or path.stat().st_size > 1024:
            raise SyncError('STATE_INVALID')
        value = json.loads(path.read_text(encoding='utf-8'))
        if (type(value) is not dict or not {'schema', 'status'} <= set(value)
                or set(value) - {'schema', 'status', 'reason'}
                or type(value['schema']) is not int or value['schema'] != 1
                or type(value['status']) is not str or value['status'] not in ('READY', 'PAUSED')
                or 'reason' in value and (type(value['reason']) is not str or value['reason'] not in _SAFE_REASONS)):
            raise SyncError('STATE_INVALID')
        return value['status']
    except (OSError, UnicodeError, ValueError):
        raise SyncError('STATE_INVALID') from None


def _write_state(path, status, reason=None):
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.ing-state-', delete=False) as stream:
            temporary = Path(stream.name)
            state = {'schema': 1, 'status': status}
            if reason in _SAFE_REASONS:
                state['reason'] = reason
            json.dump(state, stream, separators=(',', ':'))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        raise SyncError('STATE_WRITE_FAILED') from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@contextmanager
def _job_lock(state_path):
    """Hold a non-blocking OS lock; a killed process releases it automatically."""
    import stat

    lock = state_path.with_name(state_path.name + '.lock')
    descriptor = None
    acquired = False
    try:
        _directory(state_path.parent).mkdir(parents=True, exist_ok=True)
        if any(part.is_symlink() for part in (lock, *lock.parents)):
            raise SyncError('LOCKED')
        flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
        descriptor = os.open(lock, flags, 0o600)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 1
                or lock.is_symlink()):
            raise SyncError('LOCKED')
        os.lseek(descriptor, 0, os.SEEK_SET)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired = True
        if info.st_size == 0:
            os.lseek(descriptor, 0, os.SEEK_SET)
            os.write(descriptor, b'\0')
    except (OSError, SyncError):
        if descriptor is not None:
            os.close(descriptor)
        raise SyncError('LOCKED') from None
    try:
        yield
    finally:
        if descriptor is not None:
            try:
                if acquired:
                    if os.name == 'nt':
                        os.lseek(descriptor, 0, os.SEEK_SET)
                        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def _abort_challenge(_challenge):
    raise BankReadError(code=BankErrorCode.SCA_REQUIRED)


def _daily_periods(cfg, today=None):
    """Continue an unfinished month before moving on; never skip a missing month."""
    today = today or date.today()
    first = cfg['start_month']
    db = sqlite3.connect(cfg['database'].as_uri() + '?mode=ro', uri=True)
    try:
        table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ing_period_imports'").fetchone()
        latest = None if table is None else db.execute(
            'SELECT month_start,as_of FROM ing_period_imports WHERE account_id=? AND source_key=? ORDER BY month_start DESC LIMIT 1',
            (cfg['account_id'], _source_key(cfg['source_account']))).fetchone()
        if latest is not None:
            first = date.fromisoformat(latest[0])
            end = date(first.year, first.month, monthrange(first.year, first.month)[1])
            if date.fromisoformat(latest[1]) == end and end < today:
                first = end + timedelta(days=1)
    finally:
        db.close()
    if first > today:
        raise SyncError('START_NOT_REACHED')
    if (today - first).days >= 90:
        raise SyncError('HISTORY_GAP')
    periods = []
    while first <= today:
        end = date(first.year, first.month, monthrange(first.year, first.month)[1])
        periods.append((first, min(today, end)))
        if len(periods) > 3:
            raise SyncError('HISTORY_GAP')
        first = end + timedelta(days=1)
    return periods


def _daily_imports(store, cfg, archives):
    from finance_control.ing_period_import import import_period_archive
    from .ing_period_snapshot import period_key, read_period_archive
    results = []
    for archive in archives:
        s = read_period_archive(archive)
        known = store.db.execute('SELECT 1 FROM ing_period_imports WHERE period_key=?', (period_key(s),)).fetchone()
        legacy = cfg['legacy_prefix_ids'] if known is None and s.month_start == cfg['start_month'] else ()
        result = import_period_archive(
            store, archive, account_id=cfg['account_id'],
            confirmed_source_account=cfg['source_account'], source_category=cfg['source_category'],
            confirmed_legacy_prefix=legacy, initial_month_archive=cfg.get('initial_month_archive'))
        results.append(result)
    return results


def _run_daily(cfg):
    from finance_control.core import Store
    from .ing_period_snapshot import archive_period, validate_period
    currency = _preflight(cfg)
    periods = _daily_periods(cfg)
    product_id = _registration(cfg['registration_file'])
    credentials = WindowsCredentialStore().load(cfg['alias'])
    reader = create_reader('ING', '50010517', product_id, credentials, _abort_challenge)
    reader.configure_auth(None, None)
    accounts = reader.read(ReadOperation.ACCOUNTS)
    matching = [a for a in accounts if type(a) is AccountRef and a.iban == cfg['bank_account']]
    if len(matching) != 1:
        raise SyncError('ACCOUNT_SELECTION_FAILED')
    archives = []
    for start, end in periods:
        values = reader.read(ReadOperation.PERIOD_SNAPSHOT, matching[0], start, end)
        if type(values) not in (tuple, list) or len(values) != 1:
            raise SyncError('SNAPSHOT_INVALID')
        s = validate_period(values[0])
        if (s.source_account != cfg['source_account'] or s.currency != currency
                or s.month_start != start or s.as_of != end):
            raise SyncError('SNAPSHOT_INVALID')
        archives.append(archive_period(s, cfg['archive_directory']).path)
    # Simulate the complete catch-up sequence on one consistent copy. Later
    # months must see the preceding finalized month, even during preview.
    with tempfile.TemporaryDirectory(prefix='.ing-daily-preview-', dir=cfg['database'].parent) as temp:
        copied = Path(temp) / 'preview.sqlite'
        _snapshot(cfg['database'], copied)
        store = Store(copied)
        try:
            results = _daily_imports(store, cfg, archives)
        finally:
            store.close()
    if cfg['mode'] == 'daily-apply':
        backup = _directory(cfg['database'].parent) / ('ing-before-import-' + uuid.uuid4().hex + '.sqlite')
        _verified_backup(cfg['database'], backup)
        store = Store(cfg['database'])
        try:
            results = _daily_imports(store, cfg, archives)
        finally:
            store.close()
    return {'status': 'APPLIED' if cfg['mode'] == 'daily-apply' else 'PREVIEW',
            'period': periods[-1][0].strftime('%Y-%m'),
            'bookings': results[-1].count, 'inserted': sum(r.inserted for r in results),
            'skipped': sum(r.skipped for r in results)}


def _run(cfg):
    if cfg['mode'].startswith('daily-'):
        return _run_daily(cfg)
    start, end = _closed_month()
    currency = _preflight(cfg, binding=cfg['mode'] == 'apply')
    product_id = _registration(cfg['registration_file'])
    credentials = WindowsCredentialStore().load(cfg['alias'])
    reader = create_reader('ING', '50010517', product_id, credentials, _abort_challenge)
    reader.configure_auth(None, None)
    accounts = reader.read(ReadOperation.ACCOUNTS)
    if type(accounts) not in (tuple, list):
        raise SyncError('ACCOUNT_SELECTION_FAILED')
    matching = [a for a in accounts if type(a) is AccountRef and a.iban == cfg['bank_account']]
    if len(matching) != 1:
        raise SyncError('ACCOUNT_SELECTION_FAILED')
    values = reader.read(ReadOperation.MONTHLY_SNAPSHOT, matching[0], start, end)
    if type(values) not in (tuple, list) or len(values) != 1 or type(values[0]) is not MonthlySnapshot:
        raise SyncError('SNAPSHOT_INVALID')
    snapshot = validate_snapshot(values[0])
    if (snapshot.source_profile != 'ING' or snapshot.source_account != cfg['source_account']
            or snapshot.period_start != start or snapshot.period_end != end or snapshot.currency != currency):
        raise SyncError('SNAPSHOT_INVALID')
    archive = archive_monthly_snapshot(snapshot, cfg['archive_directory'])
    kwargs = {'account_id': cfg['account_id'], 'confirmed_source_account': cfg['source_account'],
              'source_category': cfg['source_category'],
              'require_adopted_source': cfg['mode'] == 'apply'}
    if _adopted_period(cfg, start):
        _adoption_replay(cfg, archive.path)
        if cfg['mode'] == 'apply':
            _preflight(cfg, binding=True)
            _adoption_replay(cfg, archive.path)
        return {'status': 'ADOPTED', 'period': start.strftime('%Y-%m'),
                'bookings': len(snapshot.rows), 'inserted': 0, 'skipped': len(snapshot.rows)}
    preview = preview_monthly_import(cfg['database'], archive.path, **kwargs)
    if cfg['mode'] == 'preview':
        return {'status': 'PREVIEW', 'period': start.strftime('%Y-%m'), 'bookings': preview.booking_count,
                'inserted': preview.inserted, 'skipped': preview.skipped}
    _preflight(cfg, binding=True)
    backup = _directory(cfg['database'].parent) / ('ing-before-import-' + uuid.uuid4().hex + '.sqlite')
    _verified_backup(cfg['database'], backup)
    from finance_control.core import Store
    store = Store(cfg['database'])
    try:
        result = import_monthly_archive(store, archive.path, **kwargs)
    finally:
        store.close()
    return {'status': 'APPLIED', 'period': start.strftime('%Y-%m'), 'bookings': result.booking_count,
            'inserted': result.inserted, 'skipped': result.skipped}


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise SyncError('INVALID_ARGUMENTS')


def main(argv=None):
    parser = _Parser()
    parser.add_argument('--config', required=True)
    flags = parser.add_mutually_exclusive_group()
    flags.add_argument('--check-only', action='store_true')
    flags.add_argument('--resume', action='store_true')
    try:
        args = parser.parse_args(argv)
        cfg = _config(args.config)
        with _job_lock(cfg['state_file']):
            if args.resume:
                _state(cfg['state_file'])
                _write_state(cfg['state_file'], 'READY')
                result = {'status': 'RESUMED'}
            elif not cfg['enabled']:
                result = {'status': 'DISABLED'}
            elif args.check_only:
                _preflight(cfg, binding=cfg['mode'] == 'apply')
                _registration(cfg['registration_file'])
                _state(cfg['state_file'])
                if cfg['mode'].startswith('daily-'):
                    _daily_periods(cfg)
                result = {'status': 'CHECKED', 'period': _closed_month()[0].strftime('%Y-%m')}
            elif _state(cfg['state_file']) == 'PAUSED':
                # _state has already validated the exact finite reason set.
                result = {'status': 'PAUSED', 'reason': json.loads(
                    cfg['state_file'].read_text(encoding='utf-8')).get('reason', 'FAILED')}
            else:
                # Persist the stop BEFORE loading a credential. A killed worker
                # or a failure to persist its final diagnostic cannot trigger
                # repeated unattended attempts with the same rejected PIN.
                _write_state(cfg['state_file'], 'PAUSED', 'IN_PROGRESS')
                try:
                    result = _run(cfg)
                    _write_state(cfg['state_file'], 'READY')
                except BankReadError as error:
                    allowed = {BankErrorCode.AUTH_REJECTED, BankErrorCode.ONLINE_LOGIN_REQUIRED,
                               BankErrorCode.CREDENTIALS_REJECTED, BankErrorCode.AUTH_TEMPORARY,
                               BankErrorCode.SCA_REQUIRED}
                    reason = error.code.name if error.code in allowed else 'BANK_FAILED'
                    _write_state(cfg['state_file'], 'PAUSED', reason)
                    result = {'status': 'PAUSED', 'reason': reason}
                except Exception as error:
                    from finance_control.ing_period_import import PeriodImportError
                    reason = error.args[0] if type(error) in (SyncError, PeriodImportError) and error.args else 'FAILED'
                    reason = reason if reason in _SAFE_REASONS else 'FAILED'
                    _write_state(cfg['state_file'], 'PAUSED', reason)
                    result = {'status': 'PAUSED', 'reason': reason}
        print(json.dumps(result, separators=(',', ':')))
        return 0 if result['status'] != 'PAUSED' else 1
    except Exception as error:
        status = error.args[0] if type(error) is SyncError and error.args[0] in {
            'INVALID_CONFIG', 'INVALID_ARGUMENTS', 'INVALID_TARGET', 'SOURCE_NOT_ADOPTED',
            'STATE_INVALID', 'STATE_WRITE_FAILED', 'LOCKED', 'ACCOUNT_SELECTION_FAILED',
            'SNAPSHOT_INVALID'} else 'FAILED'
        print(json.dumps({'status': status}, separators=(',', ':')))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
