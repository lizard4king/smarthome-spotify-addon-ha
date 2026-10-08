"""Bounded parser and immutable archive for ING/Postbank MT940 periods."""

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, localcontext
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import sys

from finance_control.bank_archive import (
    ArchiveResult, _directory, _publish_without_overwrite, _sync_directory,
)
from finance_control.connectors.fints_readonly import _without_bank_logs
from finance_control.connectors.mt940_statements import (
    _group_pages, _normalize,
)
from finance_control.statement_model import StatementError


MAX_BYTES = 16 * 1024 * 1024
MAX_ROWS = 100_000
MAX_TEXT = 8192
_CURRENCY = re.compile(r'[A-Z]{3}', re.ASCII)
_SOURCE_ACCOUNT = re.compile(r'[^\x00-\x20\x7f]{1,120}\Z')
_KINDS = {'ING': 'ing-period', 'POSTBANK': 'postbank-period'}


@dataclass(frozen=True)
class PeriodRow:
    booked_on: date
    value_on: date
    amount: Decimal
    currency: str
    counterparty: str = field(repr=False)
    description: str = field(repr=False)
    booking_text: str = field(default='', repr=False)


@dataclass(frozen=True)
class PeriodSnapshot:
    source_profile: str
    source_account: str = field(repr=False)
    month_start: date
    as_of: date
    opening_date: date
    closing_date: date
    opening_balance: Decimal
    closing_balance: Decimal
    currency: str
    rows: tuple[PeriodRow, ...] = field(repr=False)


def _fail():
    raise StatementError('Bank-Periodenabruf ist ungültig oder unvollständig.')


def _cash(value):
    if type(value) is not Decimal or not value.is_finite() or abs(value) >= Decimal('1e18'):
        _fail()
    with localcontext() as context:
        context.prec = 40
        if value != value.quantize(Decimal('.01')):
            _fail()
        return format(value if value else Decimal(0), '.2f')


def _exact_date(value):
    return type(value) is date


def _date_from_mt940(value):
    if not isinstance(value, date) or isinstance(value, datetime):
        _fail()
    return date(value.year, value.month, value.day)


def _text(value):
    if value is None:
        return ''
    if type(value) is not str or len(value) > MAX_TEXT:
        _fail()
    if any((ord(char) < 32 and char not in '\n\t') or ord(char) == 127
           or 0xD800 <= ord(char) <= 0xDFFF for char in value):
        _fail()
    return value


def _optional_text(data, key):
    return _text(data.get(key))


def validate_period(snapshot):
    if type(snapshot) is not PeriodSnapshot:
        _fail()
    if type(snapshot.source_profile) is not str or snapshot.source_profile not in _KINDS:
        _fail()
    if (type(snapshot.source_account) is not str
            or _SOURCE_ACCOUNT.fullmatch(snapshot.source_account) is None):
        _fail()
    if any(not _exact_date(value) for value in (
            snapshot.month_start, snapshot.as_of, snapshot.opening_date, snapshot.closing_date)):
        _fail()
    if (snapshot.month_start.day != 1
            or (snapshot.month_start.year, snapshot.month_start.month)
            != (snapshot.as_of.year, snapshot.as_of.month)
            or snapshot.as_of > date.today()
            or snapshot.opening_date > snapshot.month_start
            or snapshot.closing_date != snapshot.as_of):
        _fail()
    if type(snapshot.currency) is not str or _CURRENCY.fullmatch(snapshot.currency) is None:
        _fail()
    _cash(snapshot.opening_balance)
    _cash(snapshot.closing_balance)
    if type(snapshot.rows) is not tuple or len(snapshot.rows) > MAX_ROWS:
        _fail()
    total = snapshot.opening_balance
    for row in snapshot.rows:
        if (type(row) is not PeriodRow or not _exact_date(row.booked_on)
                or not _exact_date(row.value_on)
                or not snapshot.month_start <= row.booked_on <= snapshot.as_of
                or type(row.currency) is not str or row.currency != snapshot.currency):
            _fail()
        _cash(row.amount)
        _text(row.counterparty)
        _text(row.description)
        _text(row.booking_text)
        total += row.amount
    if total != snapshot.closing_balance:
        _fail()
    return snapshot


def period_source_key(snapshot):
    validate_period(snapshot)
    raw = json.dumps([snapshot.source_profile, snapshot.source_account], separators=(',', ':'),
                     ensure_ascii=False).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def period_key(snapshot):
    validate_period(snapshot)
    raw = json.dumps([snapshot.source_profile, snapshot.source_account, snapshot.month_start.isoformat()],
                     separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def row_payload(row):
    if (type(row) is not PeriodRow or not _exact_date(row.booked_on)
            or not _exact_date(row.value_on) or type(row.currency) is not str
            or _CURRENCY.fullmatch(row.currency) is None):
        _fail()
    return {
        'booked_on': row.booked_on.isoformat(),
        'value_on': row.value_on.isoformat(),
        'amount': _cash(row.amount),
        'currency': row.currency,
        'counterparty': _text(row.counterparty),
        'description': _text(row.description),
        'booking_text': _text(row.booking_text),
    }


def _payload(snapshot):
    validate_period(snapshot)
    return {
        'schema': 1,
        'kind': _KINDS[snapshot.source_profile],
        'source_profile': snapshot.source_profile,
        'source_account': snapshot.source_account,
        'month_start': snapshot.month_start.isoformat(),
        'as_of': snapshot.as_of.isoformat(),
        'opening_date': snapshot.opening_date.isoformat(),
        'closing_date': snapshot.closing_date.isoformat(),
        'opening_balance': _cash(snapshot.opening_balance),
        'closing_balance': _cash(snapshot.closing_balance),
        'currency': snapshot.currency,
        'rows': [row_payload(row) for row in snapshot.rows],
    }


def _encode(snapshot):
    return (json.dumps(_payload(snapshot), sort_keys=True, ensure_ascii=False,
                       separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')


def parse_bank_period(raw: bytes, *, source_profile: str, start: date, end: date) -> PeriodSnapshot:
    """Parse one complete supported bank account group through an as-of date."""
    if type(source_profile) is not str or source_profile not in _KINDS:
        _fail()
    if type(raw) is not bytes or not raw or len(raw) > MAX_BYTES:
        _fail()
    if not _exact_date(start) or not _exact_date(end):
        _fail()
    if (start.day != 1 or (start.year, start.month) != (end.year, end.month)
            or end > date.today()):
        _fail()
    groups = _group_pages(raw, allow_zero_number=True)
    if len(groups) != 1:
        _fail()
    (source_account, _number), pages = groups[0]
    first, last = pages[0], pages[-1]
    if first[3] > start or last[4] != end:
        _fail()

    normalized_pages = _normalize(raw)
    details = []
    with _without_bank_logs():
        try:
            import mt940
            options = mt940.Options(reversal_sign=True, unbounded_details=True)
            for page in normalized_pages:
                parsed = mt940.parse('\n'.join(':' + tag + ':' + value for tag, value in page),
                                     options=options)
                details.extend(transaction.data for transaction in parsed.transactions)
        except Exception:
            _fail()

    validated_rows = tuple(row for page in pages for row in page[8])
    if len(details) != len(validated_rows) or len(details) > MAX_ROWS:
        _fail()
    rows = []
    for validated, data in zip(validated_rows, details):
        if type(data) is not dict:
            _fail()
        try:
            booked_on = _date_from_mt940(data['entry_date'])
            value_on = _date_from_mt940(data['date'])
            amount = data['amount'].amount
            row_currency = data['currency']
        except Exception:
            _fail()
        if (type(amount) is not Decimal or booked_on != validated.booked_on
                or amount != validated.amount or row_currency != validated.currency
                or not start <= booked_on <= end):
            _fail()

        applicant = _optional_text(data, 'applicant_name')
        recipient = _optional_text(data, 'recipient_name')
        purpose = _optional_text(data, 'purpose')
        transaction_details = _optional_text(data, 'transaction_details')
        additional = _optional_text(data, 'additional_purpose')
        posting = _optional_text(data, 'posting_text')
        description = purpose or transaction_details
        if additional:
            description = '\n'.join(part for part in (description, additional) if part)
        booking_text = posting or transaction_details
        rows.append(PeriodRow(booked_on, value_on, amount, row_currency,
                              applicant or recipient, description, booking_text))

    snapshot = PeriodSnapshot(source_profile, source_account, start, end, first[3], last[4],
                              first[5], last[6], first[7], tuple(rows))
    return validate_period(snapshot)


def parse_ing_period(raw: bytes, *, start: date, end: date) -> PeriodSnapshot:
    """Preserve the original ING parser API and archive identity."""
    return parse_bank_period(raw, source_profile='ING', start=start, end=end)


def read_period_archive(path):
    try:
        path = Path(path)
        _directory(path.parent)
        if (path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES
                or re.fullmatch(r'[0-9a-f]{64}\.json', path.name) is None):
            _fail()
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != path.stem:
            _fail()
        payload = json.loads(raw)
        if (type(payload) is not dict or type(payload.get('schema')) is not int
                or payload.get('schema') != 1
                or payload.get('kind') != _KINDS.get(payload.get('source_profile'))
                or type(payload.get('rows')) is not list or len(payload['rows']) > MAX_ROWS):
            _fail()
        rows = tuple(PeriodRow(
            date.fromisoformat(item['booked_on']), date.fromisoformat(item['value_on']),
            Decimal(item['amount']), item['currency'], item['counterparty'],
            item['description'], item['booking_text']) for item in payload['rows'])
        snapshot = PeriodSnapshot(
            payload['source_profile'], payload['source_account'],
            date.fromisoformat(payload['month_start']), date.fromisoformat(payload['as_of']),
            date.fromisoformat(payload['opening_date']), date.fromisoformat(payload['closing_date']),
            Decimal(payload['opening_balance']), Decimal(payload['closing_balance']),
            payload['currency'], rows)
        if _encode(snapshot) != raw:
            _fail()
        return snapshot
    except StatementError:
        raise
    except Exception:
        _fail()


def archive_period(snapshot, directory):
    raw = _encode(snapshot)
    if len(raw) > MAX_BYTES:
        _fail()
    digest = hashlib.sha256(raw).hexdigest()
    temporary = None
    lock = None
    try:
        root = _directory(directory)
        root.mkdir(parents=True, exist_ok=True)
        lock = root / '.ing-period-archive.lock'
        try:
            lock.mkdir()
        except FileExistsError:
            lock = None
            raise StatementError('ING-Periodenarchiv ist gesperrt.') from None
        target = root / (digest + '.json')
        if target.exists():
            existing = read_period_archive(target)
            if _encode(existing) != raw:
                _fail()
            return ArchiveResult(target, digest, len(snapshot.rows), False)
        with tempfile.NamedTemporaryFile(dir=root, prefix='.pending-', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        _publish_without_overwrite(temporary, target)
        _sync_directory(root)
        return ArchiveResult(target, digest, len(snapshot.rows), True)
    except StatementError:
        raise
    except Exception:
        raise StatementError('ING-Periodenarchiv konnte nicht sicher geschrieben werden.') from None
    finally:
        active_error = sys.exc_info()[0] is not None
        failed = False
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                failed = True
        if lock is not None:
            try:
                lock.rmdir()
            except OSError:
                failed = True
        if failed and not active_error:
            raise StatementError('ING-Periodenarchivabschluss unklar; Sperre und Bestand prüfen.')
