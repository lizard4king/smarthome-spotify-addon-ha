"""Transactional bank statement import with local ordinal identities."""

import csv
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext
import io
import json
from pathlib import Path
import tempfile

from .bank_archive import _directory
from .bank_account_policy import bank_account_kind_supported
from .connectors.ing_period_snapshot import (
    period_key, period_source_key, read_period_archive, row_payload, validate_period,
)
from .core import Store, money, valid_identifier
from .statement_import import _snapshot


class PeriodImportError(ValueError):
    """Static diagnostic; source rows and local paths never appear in errors."""


@dataclass(frozen=True)
class PeriodImportResult:
    count: int
    inserted: int
    skipped: int


@dataclass(frozen=True)
class BankBalanceRecord:
    """An explicit bank-reported balance captured at read time."""

    amount: Decimal
    currency: str
    booked_on: date
    retrieved_at: datetime


def _cash(value):
    return format(money(value), '.2f')


def _canonical_rows(rows):
    return json.dumps([row_payload(row) for row in rows], sort_keys=True,
                      ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def _previous_month(month_start):
    end = month_start - timedelta(days=1)
    return end.replace(day=1).isoformat(), end.isoformat()


def _source_guard(db, snapshot, account_id, source_key):
    direct = db.execute('SELECT provider,account_id FROM bank_source_accounts WHERE source_key=?',
                        (source_key,)).fetchone()
    reverse = db.execute('SELECT provider,source_key FROM bank_source_accounts WHERE account_id=?',
                         (account_id,)).fetchone()
    statement = db.execute('SELECT account_id FROM bank_statement_account_bindings WHERE source_key=?',
                           (source_key,)).fetchone()
    statement_reverse = db.execute('SELECT source_key FROM bank_statement_account_bindings WHERE account_id=?',
                                   (account_id,)).fetchone()
    if (direct is None or tuple(direct) != (snapshot.source_profile, account_id) or reverse is None
            or tuple(reverse) != (snapshot.source_profile, source_key)
            or statement is not None and statement[0] != account_id
            or statement_reverse is not None and statement_reverse[0] != source_key):
        raise PeriodImportError('SOURCE_BINDING_REQUIRED')
    before_start, before_end = _previous_month(snapshot.month_start)
    adopted = db.execute(
        'SELECT 1 FROM bank_monthly_adoptions WHERE source_key=? AND account_id=? AND period_start=? AND period_end=?',
        (source_key, account_id, before_start, before_end)).fetchone()
    prior = db.execute(
        'SELECT as_of FROM ing_period_imports WHERE source_key=? AND account_id=? AND month_start=?',
        (source_key, account_id, before_start)).fetchone()
    if adopted is None and (prior is None or prior['as_of'] != before_end):
        raise PeriodImportError('PREVIOUS_MONTH_UNVERIFIED')


def _ledger_control(db, account, account_id, start, opening):
    if account['opening_date'] >= start:
        raise PeriodImportError('ACCOUNT_OPENING_MISMATCH')
    with localcontext() as context:
        context.prec = 40
        total = Decimal(account['opening'])
        for row in db.execute('SELECT amount FROM transactions WHERE account_id=? AND date<?',
                              (account_id, start)):
            total += Decimal(row[0])
    if total != opening:
        raise PeriodImportError('OPENING_BALANCE_MISMATCH')


def _current_rows(db, account_id, start, next_start):
    return db.execute(
        'SELECT external_id,date,amount,currency,category,transfer_id FROM transactions '
        'WHERE account_id=? AND date>=? AND date<?', (account_id, start, next_start)).fetchall()


def import_period_archive(store, archive, *, account_id, confirmed_source_account, source_category,
                          confirmed_legacy_prefix=(), initial_month_archive=None,
                          bank_balance: BankBalanceRecord | None = None):
    """Append only a verified suffix; preserve earlier classifications and context."""
    snapshot = read_period_archive(archive)
    validate_period(snapshot)
    if (snapshot.source_profile not in ('ING', 'POSTBANK', 'NASPA') or type(confirmed_source_account) is not str
            or confirmed_source_account != snapshot.source_account):
        raise PeriodImportError('SOURCE_CONFIRMATION_REQUIRED')
    if not valid_identifier(account_id):
        raise PeriodImportError('ACCOUNT_INVALID')
    if (type(source_category) is not str or not source_category or source_category != source_category.strip()
            or len(source_category) > 240 or any(not char.isprintable() for char in source_category)):
        raise PeriodImportError('CATEGORY_INVALID')
    if (type(confirmed_legacy_prefix) is not tuple or any(
            type(value) is not str or not 1 <= len(value) <= 256 or not value.strip()
            for value in confirmed_legacy_prefix)
            or len(set(confirmed_legacy_prefix)) != len(confirmed_legacy_prefix)):
        raise PeriodImportError('LEGACY_PREFIX_INVALID')
    if store.db.in_transaction:
        raise PeriodImportError('TRANSACTION_ACTIVE')
    if initial_month_archive is not None and not isinstance(initial_month_archive, (str, Path)):
        raise PeriodImportError('INITIAL_ARCHIVE_INVALID')
    if bank_balance is not None:
        if (type(bank_balance) is not BankBalanceRecord
                or type(bank_balance.amount) is not Decimal
                or type(bank_balance.currency) is not str
                or bank_balance.currency != snapshot.currency
                or type(bank_balance.booked_on) is not date
                or type(bank_balance.retrieved_at) is not datetime
                or bank_balance.retrieved_at.utcoffset() is None):
            raise PeriodImportError('BANK_BALANCE_INVALID')
        try:
            balance_amount = _cash(bank_balance.amount)
        except ValueError:
            raise PeriodImportError('BANK_BALANCE_INVALID') from None
    key = period_key(snapshot)
    source_key = period_source_key(snapshot)
    namespace = f'{snapshot.source_profile.lower()}-period'
    start = snapshot.month_start.isoformat()
    next_start = (snapshot.month_start.replace(day=28) + timedelta(days=4)).replace(day=1).isoformat()
    end = snapshot.as_of.isoformat()
    archive_sha256 = Path(archive).stem
    row_json = _canonical_rows(snapshot.rows)
    count = len(snapshot.rows)
    try:
        db = store.db
        db.execute('BEGIN IMMEDIATE')
        account = db.execute('SELECT * FROM accounts WHERE id=?', (account_id,)).fetchone()
        if account is None or account['currency'] != snapshot.currency:
            raise PeriodImportError('ACCOUNT_CURRENCY_MISMATCH')
        # Recheck the bank-specific account kind inside the write transaction;
        # the preview is not an authorization to change the target later.
        if not bank_account_kind_supported(snapshot.source_profile, account['kind'],
                                           account['currency']):
            raise PeriodImportError('invalid_target')
        if initial_month_archive is not None:
            already_bound = db.execute('SELECT 1 FROM bank_source_accounts WHERE source_key=?',
                                       (source_key,)).fetchone()
            if already_bound is None:
                from .bank_source_policy import adopt_monthly_archive
                from .monthly_archive import read_monthly_archive
                initial = read_monthly_archive(initial_month_archive)
                if (initial.source_profile != snapshot.source_profile
                        or initial.source_account != snapshot.source_account
                        or initial.period_end != snapshot.month_start - timedelta(days=1)):
                    raise PeriodImportError('INITIAL_ARCHIVE_MISMATCH')
                adopt_monthly_archive(store, initial_month_archive, account_id,
                                      confirmed_source_account, manage_transaction=False)
        _source_guard(db, snapshot, account_id, source_key)
        _ledger_control(db, account, account_id, start, snapshot.opening_balance)
        if db.execute('SELECT 1 FROM bank_monthly_adoptions WHERE account_id=? AND period_start=?',
                      (account_id, start)).fetchone():
            raise PeriodImportError('MONTH_ALREADY_ADOPTED')
        if db.execute('SELECT 1 FROM bank_statement_imports WHERE account_id=? AND period_start<? AND period_end>=?',
                      (account_id, next_start, start)).fetchone():
            raise PeriodImportError('MONTH_ALREADY_IMPORTED')
        existing = db.execute('SELECT * FROM ing_period_imports WHERE period_key=?', (key,)).fetchone()
        other = db.execute('SELECT 1 FROM ing_period_imports WHERE account_id=? AND month_start=? AND period_key<>?',
                           (account_id, start, key)).fetchone()
        if other:
            raise PeriodImportError('PERIOD_CONFLICT')
        month_rows = _current_rows(db, account_id, start, next_start)
        previous_count = 0
        ledger_ids = []
        if existing is not None:
            if confirmed_legacy_prefix:
                raise PeriodImportError('LEGACY_PREFIX_ALREADY_BOUND')
            previous_count = existing['booking_count']
            if (existing['account_id'] != account_id or existing['source_key'] != source_key
                    or existing['month_start'] != start or existing['currency'] != snapshot.currency
                    or existing['source_category'] != source_category
                    or existing['opening_balance'] != _cash(snapshot.opening_balance)
                    or existing['as_of'] > end or count < previous_count):
                raise PeriodImportError('PERIOD_CONFLICT')
            try:
                previous_rows = json.loads(existing['row_payload_json'])
                current_rows = json.loads(row_json)
            except (ValueError, TypeError):
                raise PeriodImportError('CHECKPOINT_INVALID') from None
            if (type(previous_rows) is not list or len(previous_rows) != previous_count
                    or previous_rows != current_rows[:previous_count]):
                raise PeriodImportError('PREFIX_CHANGED')
            try:
                ledger_ids = json.loads(existing['ledger_ids_json'])
            except (ValueError, TypeError):
                raise PeriodImportError('CHECKPOINT_INVALID') from None
            if ledger_ids == [] and previous_count:
                # Migration 046 upgrades the original 045 checkpoint, whose
                # sole ID contract was this deterministic local ordinal. Recover
                # only those exact IDs; the complete financial/prefix checks
                # below still have to succeed before persisting the mapping.
                if snapshot.source_profile != 'ING':
                    raise PeriodImportError('CHECKPOINT_INVALID')
                ledger_ids = [f'ing-period:{key}:{ordinal}'
                              for ordinal in range(1, previous_count + 1)]
            if (type(ledger_ids) is not list or len(ledger_ids) != previous_count
                    or any(type(value) is not str for value in ledger_ids)
                    or len(set(ledger_ids)) != previous_count):
                raise PeriodImportError('CHECKPOINT_INVALID')
            if end == existing['as_of'] and count == previous_count and (row_json != existing['row_payload_json']
                                             or _cash(snapshot.closing_balance) != existing['closing_balance']):
                raise PeriodImportError('SAME_DAY_CHANGED')
        elif month_rows:
            if not confirmed_legacy_prefix:
                raise PeriodImportError('LEGACY_MONTH_OVERLAP')
            if len(confirmed_legacy_prefix) != len(month_rows) or len(confirmed_legacy_prefix) > count:
                raise PeriodImportError('LEGACY_PREFIX_MISMATCH')
            previous_count = len(confirmed_legacy_prefix)
            ledger_ids = list(confirmed_legacy_prefix)
        elif confirmed_legacy_prefix:
            raise PeriodImportError('LEGACY_PREFIX_MISMATCH')
        expected_ids = set(ledger_ids)
        if len(month_rows) != previous_count or {row['external_id'] for row in month_rows} != expected_ids:
            raise PeriodImportError('LEDGER_PREFIX_CHANGED')
        old_control = snapshot.opening_balance
        for index, row in enumerate(snapshot.rows[:previous_count], 1):
            actual = db.execute('SELECT date,amount,currency FROM transactions WHERE account_id=? AND external_id=?',
                                (account_id, ledger_ids[index - 1])).fetchone()
            if actual is None or tuple(actual) != (row.booked_on.isoformat(), _cash(row.amount), row.currency):
                raise PeriodImportError('LEDGER_PREFIX_CHANGED')
            old_control += row.amount
        if existing is not None and _cash(old_control) != existing['closing_balance']:
            raise PeriodImportError('LEDGER_CONTROL_CHANGED')
        suffix = snapshot.rows[previous_count:]
        if suffix:
            columns = ['external_id', 'account_id', 'date', 'amount', 'currency', 'category', 'transfer_id']
            stream = io.StringIO(newline='')
            writer = csv.DictWriter(stream, fieldnames=columns, lineterminator='\n')
            writer.writeheader()
            for ordinal, row in enumerate(suffix, previous_count + 1):
                writer.writerow({'external_id': f'{namespace}:{key}:{ordinal}', 'account_id': account_id,
                                 'date': row.booked_on.isoformat(), 'amount': _cash(row.amount),
                                 'currency': row.currency, 'category': source_category, 'transfer_id': ''})
            if store.import_csv(stream.getvalue(), manage_transaction=False) != len(suffix):
                raise PeriodImportError('IMPORT_INCOMPLETE')
            for ordinal, row in enumerate(suffix, previous_count + 1):
                # Unstructured MT940 :86: text may fill both source fields.
                # Keep it once in the display context; the archive preserves
                # both original fields for exact prefix validation.
                description = row.description
                if row.booking_text and row.booking_text != row.description:
                    description = ' | '.join(part for part in (description, row.booking_text) if part)
                db.execute('INSERT INTO transaction_context(account_id,external_id,counterparty,description) VALUES (?,?,?,?)',
                           (account_id, f'{namespace}:{key}:{ordinal}', row.counterparty, description))
                ledger_ids.append(f'{namespace}:{key}:{ordinal}')
        ledger_ids_json = json.dumps(ledger_ids, ensure_ascii=False, separators=(',', ':'))
        if existing is None:
            db.execute('INSERT INTO ing_period_imports '
                       '(period_key,account_id,source_key,month_start,as_of,archive_sha256,source_category,'
                       'opening_balance,closing_balance,currency,row_payload_json,ledger_ids_json,booking_count) '
                       'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (key, account_id, source_key, start, end, archive_sha256, source_category,
                        _cash(snapshot.opening_balance), _cash(snapshot.closing_balance),
                        snapshot.currency, row_json, ledger_ids_json, count))
        else:
            db.execute('UPDATE ing_period_imports SET as_of=?,archive_sha256=?,closing_balance=?,row_payload_json=?,ledger_ids_json=?,booking_count=? '
                       'WHERE period_key=?', (end, archive_sha256, _cash(snapshot.closing_balance), row_json,
                                            ledger_ids_json, count, key))
        if bank_balance is not None:
            db.execute(
                'INSERT INTO bank_account_balances '
                '(account_id,source_key,amount,currency,booked_on,retrieved_at) '
                'VALUES (?,?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET '
                'source_key=excluded.source_key,amount=excluded.amount,'
                'currency=excluded.currency,booked_on=excluded.booked_on,'
                'retrieved_at=excluded.retrieved_at '
                'WHERE excluded.booked_on>bank_account_balances.booked_on '
                'OR (excluded.booked_on=bank_account_balances.booked_on '
                'AND excluded.retrieved_at>bank_account_balances.retrieved_at)',
                (account_id, source_key, balance_amount, bank_balance.currency,
                 bank_balance.booked_on.isoformat(),
                 bank_balance.retrieved_at.astimezone(UTC).isoformat()))
        db.commit()
        return PeriodImportResult(count, len(suffix), previous_count)
    except PeriodImportError:
        store.db.rollback()
        raise
    except Exception:
        store.db.rollback()
        raise PeriodImportError('IMPORT_FAILED') from None


def preview_period_import(database, archive, *, account_id, confirmed_source_account, source_category,
                          confirmed_legacy_prefix=(), initial_month_archive=None):
    """Run the entire importer on a consistent copy; original remains read-only."""
    database = Path(database).resolve(strict=True)
    parent = _directory(database.parent)
    with tempfile.TemporaryDirectory(prefix='.ing-period-preview-', dir=parent) as temporary:
        copied = Path(temporary) / 'preview.sqlite'
        _snapshot(database, copied)
        store = Store(copied)
        try:
            return import_period_archive(store, archive, account_id=account_id,
                                         confirmed_source_account=confirmed_source_account,
                                         source_category=source_category,
                                         confirmed_legacy_prefix=confirmed_legacy_prefix,
                                         initial_month_archive=initial_month_archive)
        finally:
            store.close()
