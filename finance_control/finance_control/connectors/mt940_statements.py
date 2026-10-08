"""Strict, bounded MT940 statement parser with balance verification."""

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import re

from finance_control.statement_model import (
    BankStatement, MonthlySnapshot, StatementError, StatementParseError, StatementRow,
)
from .profiles import PROFILES
from .fints_readonly import _without_bank_logs


_MAX_BYTES = 16 * 1024 * 1024
_TAG = re.compile(r'^:(\d{2}[A-Z]?):(.*)$', re.ASCII)
_BALANCE = re.compile(r'^([CD])(\d{6})([A-Z]{3})(\d+),(\d*)$', re.ASCII)
_ACCOUNT = re.compile(r'[^\x00-\x1f\x7f]{1,120}\Z')


def _invalid(reason='INCOMPLETE'):
    if reason not in StatementParseError._REASONS:
        raise StatementError()
    raise StatementParseError(reason)


def _date(value):
    if not re.fullmatch(r'\d{6}', value, re.ASCII):
        _invalid()
    yy, mm, dd = int(value[:2]), int(value[2:4]), int(value[4:])
    year = 2000 + yy if yy <= 69 else 1900 + yy
    try:
        return date(year, mm, dd)
    except ValueError:
        _invalid()


def _balance(value):
    match = _BALANCE.fullmatch(value)
    if match is None:
        _invalid()
    sign, raw_date, currency, whole, fraction = match.groups()
    if len(whole) > 18 or len(fraction) > 2:
        _invalid()
    try:
        amount = Decimal(whole + ('.' + fraction if fraction else ''))
    except InvalidOperation:
        _invalid()
    if sign == 'D':
        amount = amount.copy_negate()
    return _date(raw_date), amount, currency


def _normalize(raw):
    if not raw or b'\x00' in raw:
        _invalid('FORMAT')
    try:
        text = raw.decode('iso-8859-1')
    except UnicodeDecodeError:
        _invalid('FORMAT')
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    if text.startswith('{'):
        # Accept the conventional single SWIFT block envelope, with no prefix/suffix.
        match = re.fullmatch(r'\{1:[^{}\n]+\}\{2:[^{}\n]+\}\{4:\n?(.*?)(?:\n)?-\}', text, re.DOTALL)
        if match is None:
            _invalid('FORMAT')
        text = match.group(1)
    pages = []
    current = []
    terminated = False
    for line in text.split('\n'):
        if line == '':
            continue
        match = _TAG.fullmatch(line)
        if match is not None and match.group(1) == '20':
            if current:
                pages.append(current)
            current = [match.groups()]
            terminated = False
            continue
        if line == '-':
            # The MT940 block terminator may follow a final-page closing balance
            # and its optional available-balance / forward-available tags.
            if (not current or current[-1][0] not in ('62F', '64', '65') or
                    not any(tag == '62F' for tag, _ in current)):
                _invalid('FORMAT')
            pages.append(current)
            current = []
            terminated = True
            continue
        if terminated:
            _invalid('FORMAT')
        if match is not None:
            if not current:
                _invalid('FORMAT')
            current.append(match.groups())
        else:
            # Only indented continuation lines belonging to the current tag are permitted.
            if not current or (not line.startswith((' ', '\t')) and current[-1][0] not in ('61', '86')):
                _invalid('FORMAT')
            tag, value = current[-1]
            current[-1] = (tag, value + '\n' + line)
    if current:
        pages.append(current)
    if not pages:
        _invalid('FORMAT')
    return pages


def _single(page, tag):
    found = [value for key, value in page if key == tag]
    if len(found) != 1:
        _invalid()
    return found[0]


def _parse_page(page, *, allow_zero_number=False):
    if not page or page[0][0] != '20':
        _invalid()
    account = _single(page, '25')
    if not _ACCOUNT.fullmatch(account) or account != account.strip():
        _invalid()
    statement = _single(page, '28C')
    if not re.fullmatch(r'[0-9]{1,9}(?:/[0-9]{1,9})?', statement, re.ASCII):
        _invalid()
    bits = statement.split('/')
    number = int(bits[0])
    if number < (0 if allow_zero_number else 1):
        _invalid('INCOMPLETE' if allow_zero_number else 'IDENTITY_MISSING')
    page_no = int(bits[1]) if len(bits) == 2 else 1
    if page_no < 1:
        _invalid()
    opening_tags = [(tag, value) for tag, value in page if tag in ('60F', '60M')]
    closing_tags = [(tag, value) for tag, value in page if tag in ('62F', '62M')]
    if len(opening_tags) != 1 or len(closing_tags) != 1:
        _invalid()
    opening_tag, opening_value = opening_tags[0]
    closing_tag, closing_value = closing_tags[0]
    if opening_tag not in ('60F', '60M') or closing_tag not in ('62F', '62M'):
        _invalid()
    if (opening_tag, page_no == 1) not in (('60F', True), ('60M', False)):
        _invalid()
    opening_date, opening, currency = _balance(opening_value)
    closing_date, closing, close_currency = _balance(closing_value)
    if currency != close_currency or closing_date < opening_date:
        _invalid()
    for forbidden in ('60F', '60M', '62F', '62M'):
        if sum(tag == forbidden for tag, _ in page) > 1:
            _invalid()
    rows = []
    # mt940 is deliberately given only this complete page; source fields stay out of errors/logs.
    with _without_bank_logs():
        try:
            import mt940
            parsed = mt940.parse('\n'.join(':' + tag + ':' + value for tag, value in page),
                                 options=mt940.Options(reversal_sign=True))
        except Exception:
            _invalid()
    raw_entries = [value for tag, value in page if tag == '61']
    transactions = parsed.transactions
    if len(transactions) != len(raw_entries):
        _invalid()
    for raw_entry, transaction in zip(raw_entries, transactions):
        entry_date_match = re.match(r'^\d{6}(\d{4})?R?[CD]', raw_entry, re.ASCII)
        if entry_date_match is None or entry_date_match.group(1) is None:
            _invalid()
        data = transaction.data
        try:
            booked = date(data['entry_date'].year, data['entry_date'].month, data['entry_date'].day)
            amount = data['amount'].amount
            if type(amount) is not Decimal:
                _invalid()
        except Exception:
            _invalid()
        row_currency = data.get('currency')
        if (not amount.is_finite() or amount.as_tuple().exponent < -2 or
                row_currency != currency or booked < opening_date or booked > closing_date):
            _invalid()
        rows.append(StatementRow(booked, amount, currency))
    if opening + sum((row.amount for row in rows), Decimal('0')) != closing:
        _invalid()
    return (account, number, page_no, opening_date, closing_date, opening, closing,
            currency, rows, opening_tag, closing_tag)


def _group_pages(raw, *, allow_zero_number=False):
    pages = [_parse_page(page, allow_zero_number=allow_zero_number) for page in _normalize(raw)]
    groups = []
    for parsed in pages:
        account, number, page_no, *_ = parsed
        if page_no == 1:
            if groups and groups[-1][1][-1][10] != '62F':
                _invalid()
            groups.append(((account, number), [parsed]))
        else:
            if not groups or groups[-1][0] != (account, number) or groups[-1][1][-1][10] != '62M':
                _invalid()
            groups[-1][1].append(parsed)
    for _, group in groups:
        if [page[2] for page in group] != list(range(1, len(group) + 1)):
            _invalid()
        if group[0][9] != '60F' or group[-1][10] != '62F':
            _invalid()
        if len(group) > 1 and (group[0][10] != '62M' or group[-1][9] != '60M'):
            _invalid()
        if any(page[9] != '60M' or page[10] != '62M' for page in group[1:-1]):
            _invalid()
        for previous, current in zip(group, group[1:]):
            if (current[7] != previous[7] or previous[4] != current[3] or
                    previous[6] != current[5]):
                _invalid()
        first, last = group[0], group[-1]
        rows = tuple(row for page in group for row in page[8])
        if first[7] != last[7] or first[5] + sum((row.amount for row in rows), Decimal('0')) != last[6]:
            _invalid()
    return groups


def parse_mt940_statements(raw: bytes, source_profile: str) -> tuple[BankStatement, ...]:
    """Parse complete MT940 source bytes and reject incomplete or inconsistent statements."""
    if type(raw) is not bytes or not raw or len(raw) > _MAX_BYTES:
        _invalid()
    if type(source_profile) is not str or source_profile not in PROFILES:
        _invalid()
    groups = _group_pages(raw)
    statements = []
    for (account, number), group in groups:
        first, last = group[0], group[-1]
        year = last[4].year
        rows = tuple(row for page in group for row in page[8])
        statements.append(BankStatement(source_profile, account, year, number,
                                        first[3], last[4], first[5], last[6], first[7], rows))
    return tuple(statements)


def parse_monthly_snapshot(raw: bytes, source_profile: str, *, start: date, end: date) -> MonthlySnapshot:
    """Parse one complete calendar-month snapshot without assigning a statement identity."""
    if type(raw) is not bytes or not raw or len(raw) > _MAX_BYTES:
        _invalid()
    if type(source_profile) is not str or source_profile not in PROFILES:
        _invalid()
    if (type(start) is not date or type(end) is not date or start.day != 1 or
            (start.year, start.month) != (end.year, end.month)):
        _invalid()
    next_month = date(end.year + (end.month == 12), 1 if end.month == 12 else end.month + 1, 1)
    if end != next_month - timedelta(days=1):
        _invalid()
    groups = _group_pages(raw, allow_zero_number=True)
    if len(groups) != 1:
        _invalid()
    (account, _number), group = groups[0]
    first, last = group[0], group[-1]
    rows = tuple(row for page in group for row in page[8])
    if (first[3] > start or last[4] != end or
            any(row.booked_on < start or row.booked_on > end for row in rows)):
        _invalid()
    return MonthlySnapshot(source_profile, account, start, end, first[3], last[4],
                           first[5], last[6], first[7], rows)
