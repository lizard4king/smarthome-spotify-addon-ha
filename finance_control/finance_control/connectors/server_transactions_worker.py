"""Isolated Giro statement read; JSON never carries credentials."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, timedelta
from decimal import Decimal

from .fints_readonly import BankErrorCode, BankReadError, ReadOperation
from .ing_period_snapshot import validate_period
from .mt940_statements import MonthlySnapshot, StatementRow
from .server_balance_worker import _account_identity, _fingerprint, _masked, _balance
from .server_bank_rules import valid_auth_selection, valid_product_id


_FIELDS = {'connection_id', 'owner_user_id', 'bank_id', 'bank_code', 'product_id',
           'tan_method', 'tan_medium', 'action', 'account_fingerprint', 'month_start', 'as_of'}
_ID = re.compile(r'[0-9a-f]{32}\Z')
_FP = re.compile(r'[0-9a-f]{64}\Z')
_MONEY = re.compile(r'-?[0-9]{1,18}\.[0-9]{2}\Z')
_CURRENCY = re.compile(r'[A-Z]{3}\Z')
_MAX_INPUT = 16 * 1024
_MAX_OUTPUT = 8 * 1024 * 1024
_MAX_ROWS = 10_000
_MAX_TEXT = 8192
_BANK_CODES = {'POSTBANK': '50010060', 'ING': '50010517',
               'NASPA': '51050015'}
_DIAGNOSTIC_BANK_CODES = frozenset({
    'ONLINE_LOGIN_REQUIRED', 'CREDENTIALS_REJECTED', 'AUTH_TEMPORARY',
    'UNSUPPORTED', 'CONNECTION', 'TIMEOUT', 'TLS', 'DIALOG_INIT',
    'NO_RESPONSE', 'BANK_REJECTED', 'UNKNOWN', 'DATA_FORMAT',
    'IDENTIFICATION_FORMAT', 'PRODUCT_FORMAT', 'STATEMENT_INCOMPLETE',
    'STATEMENT_FORMAT', 'STATEMENT_ID_MISSING',
})


def _error(code):
    return {'status': 'error', 'code': code}


def _diagnostic_error(code, stage, bank_error=None):
    diagnostic = {'stage': stage}
    if (type(bank_error) is BankErrorCode
            and bank_error.value in _DIAGNOSTIC_BANK_CODES):
        diagnostic['bank_error_code'] = bank_error.value
    return {'status': 'error', 'code': code, 'diagnostic': diagnostic}


def _date(value):
    if type(value) is not str or len(value) != 10:
        raise ValueError('date')
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError('date')
    return parsed


def _valid(request):
    if type(request) is not dict or set(request) != _FIELDS:
        return False
    if any(type(request[key]) is not str or _ID.fullmatch(request[key]) is None
           for key in ('connection_id', 'owner_user_id')):
        return False
    bank_id = request['bank_id']
    if (type(bank_id) is not str or _BANK_CODES.get(bank_id) != request['bank_code']
            or not valid_product_id(request['product_id'])
            or not valid_auth_selection(bank_id, request['tan_method'], request['tan_medium'])):
        return False
    if request['action'] == 'accounts':
        return all(request[key] is None for key in ('account_fingerprint', 'month_start', 'as_of'))
    if request['action'] != 'period' or type(request['account_fingerprint']) is not str or _FP.fullmatch(request['account_fingerprint']) is None:
        return False
    try:
        start, end = _date(request['month_start']), _date(request['as_of'])
    except (ValueError, TypeError):
        return False
    return (start.day == 1 and (start.year, start.month) == (end.year, end.month)
            and end <= date.today() and (date.today() - start).days <= 90)


def _cash(value):
    if type(value) is not Decimal or not value.is_finite() or abs(value) >= Decimal('1e18'):
        raise ValueError('amount')
    raw = format(value, '.2f')
    if Decimal(raw) != value or _MONEY.fullmatch(raw) is None:
        raise ValueError('amount')
    return raw


def _text(value):
    if (type(value) is not str or len(value) > _MAX_TEXT
            or any((ord(c) < 32 and c not in '\n\t') or ord(c) == 127
                   or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        raise ValueError('text')
    return value


def _source_matches(source, account):
    if type(source) is not str or not 1 <= len(source) <= 120:
        return False
    parts = source.split('/')
    return (source in (account.iban, account.accountnumber)
            or (len(parts) == 2 and parts[0] == account.blz
                and parts[1].lstrip('0') == account.accountnumber.lstrip('0')))


def _monthly(snapshot, account, start, end, bank_id):
    if (type(snapshot) is not MonthlySnapshot or snapshot.source_profile != bank_id
            or not _source_matches(snapshot.source_account, account)
            or snapshot.period_start != start or snapshot.period_end != end
            or type(snapshot.opening_date) is not date or snapshot.opening_date > start
            or type(snapshot.closing_date) is not date or snapshot.closing_date != end
            or type(snapshot.currency) is not str or _CURRENCY.fullmatch(snapshot.currency) is None
            or type(snapshot.rows) is not tuple or len(snapshot.rows) > _MAX_ROWS):
        raise ValueError('monthly')
    total = snapshot.opening_balance
    rows = []
    for row in snapshot.rows:
        if (type(row) is not StatementRow or type(row.booked_on) is not date
                or not start <= row.booked_on <= end or row.currency != snapshot.currency):
            raise ValueError('monthly row')
        total += row.amount
        rows.append({'booked_on': row.booked_on.isoformat(), 'amount': _cash(row.amount),
                     'currency': snapshot.currency})
    if total != snapshot.closing_balance:
        raise ValueError('monthly sum')
    return {'source_profile': bank_id, 'source_account': snapshot.source_account,
            'period_start': start.isoformat(), 'period_end': end.isoformat(),
            'opening_date': snapshot.opening_date.isoformat(),
            'closing_date': snapshot.closing_date.isoformat(),
            'opening_balance': _cash(snapshot.opening_balance),
            'closing_balance': _cash(snapshot.closing_balance),
            'currency': snapshot.currency, 'rows': rows}


def _period(snapshot, account, start, end, bank_id):
    validate_period(snapshot)
    if (snapshot.source_profile != bank_id or not _source_matches(snapshot.source_account, account)
            or snapshot.month_start != start or snapshot.as_of != end
            or len(snapshot.rows) > _MAX_ROWS):
        raise ValueError('period')
    return {'source_profile': bank_id, 'source_account': snapshot.source_account,
            'month_start': start.isoformat(), 'as_of': end.isoformat(),
            'opening_date': snapshot.opening_date.isoformat(),
            'closing_date': snapshot.closing_date.isoformat(),
            'opening_balance': _cash(snapshot.opening_balance),
            'closing_balance': _cash(snapshot.closing_balance), 'currency': snapshot.currency,
            'rows': [{'booked_on': row.booked_on.isoformat(), 'value_on': row.value_on.isoformat(),
                      'amount': _cash(row.amount), 'currency': row.currency,
                      'counterparty': _text(row.counterparty), 'description': _text(row.description),
                      'booking_text': _text(row.booking_text)} for row in snapshot.rows]}


def _options(options, *, medium=False):
    result = []
    for option in options:
        key, label = getattr(option, 'key', None), getattr(option, 'label', None)
        if (type(key) is not str or type(label) is not str or len(key) > 20
                or not key.isascii() or not key.isprintable() or not 1 <= len(label) <= 80
                or not label.isprintable()):
            raise ValueError('auth options')
        result.append({'label': label} if medium else {'key': key, 'label': label})
    if not result or len(result) > 20:
        raise ValueError('auth options')
    return result


def run_request(request, vault_factory, reader_factory):
    if not _valid(request):
        return _error('invalid_request')
    try:
        credentials = vault_factory().load(request['connection_id'], request['owner_user_id'])
    except Exception:
        return _error('vault_unavailable')
    missing = {}
    bestsign = [False]
    naspa_auth_configured = [False]
    bank_id = request['bank_id']
    stage = 'accounts'
    try:
        def challenge(value):
            if not value.decoupled:
                return None
            if bank_id == 'POSTBANK':
                if not bestsign[0]:
                    return None
            elif bank_id == 'NASPA':
                if not naspa_auth_configured[0]:
                    return None
            else:
                return None
            # FinTS subsequently polls the bank through send_tan; True supplies no TAN.
            time.sleep(5)
            return True

        reader = reader_factory(bank_id, request['bank_code'], request['product_id'], credentials,
                                challenge, tan_method=request['tan_method'])

        def method(options):
            safe = _options(options)
            if request['tan_method'] is None:
                missing['method'] = safe
                return None
            matches = [i for i, value in enumerate(safe) if value['key'] == request['tan_method']]
            return matches[0] if len(matches) == 1 else None

        def medium(options):
            safe = _options(options, medium=True)
            if request['tan_medium'] is None:
                missing['medium'] = safe
                return None
            matches = [i for i, value in enumerate(safe) if value['label'] == request['tan_medium']]
            return matches[0] if len(matches) == 1 else None

        reader.configure_auth(method, medium, force_selection=(bank_id in ('POSTBANK', 'NASPA')))
        if bank_id == 'NASPA':
            naspa_auth_configured[0] = True
        if bank_id == 'POSTBANK':
            client = getattr(reader, '_client', None)
            selected = client.get_current_tan_mechanism()
            mechanism = client.get_tan_mechanisms().get(selected)
            bestsign[0] = 'bestsign' in str(getattr(mechanism, 'name', '')).casefold()
        accounts = reader.read(ReadOperation.ACCOUNTS)
        if type(accounts) not in (tuple, list) or len(accounts) > 20:
            raise ValueError('accounts')
        identities = [(_account_identity(account), account) for account in accounts]
        fingerprints = [_fingerprint(request, values) for values, _ in identities]
        if len(set(fingerprints)) != len(fingerprints):
            raise ValueError('duplicate accounts')
        if request['action'] == 'accounts':
            return {'status': 'ok', 'accounts': [
                {'fingerprint': fp, 'masked_account': _masked(values)}
                for fp, (values, _) in zip(fingerprints, identities)]}
        matches = [account for fp, (_, account) in zip(fingerprints, identities)
                   if fp == request['account_fingerprint']]
        if len(matches) != 1:
            return _error('unknown_account')
        account = matches[0]
        start, end = _date(request['month_start']), _date(request['as_of'])
        previous_end = start - timedelta(days=1)
        previous_start = date(previous_end.year, previous_end.month, 1)
        stage = 'control_month'
        monthly_values = reader.read(ReadOperation.MONTHLY_SNAPSHOT, account, previous_start, previous_end)
        if type(monthly_values) not in (tuple, list) or len(monthly_values) != 1:
            raise ValueError('snapshot count')
        monthly = _monthly(monthly_values[0], account, previous_start, previous_end, bank_id)
        stage = 'period'
        period_values = reader.read(ReadOperation.PERIOD_SNAPSHOT, account, start, end)
        if type(period_values) not in (tuple, list) or len(period_values) != 1:
            raise ValueError('snapshot count')
        period = _period(period_values[0], account, start, end, bank_id)
        if (monthly['currency'] != period['currency']
                or monthly['closing_balance'] != period['opening_balance']
                or monthly['source_account'] != period['source_account']):
            raise ValueError('snapshot continuity')
        stage = 'balance'
        balance = _balance(reader.read(ReadOperation.BALANCE, account))
        balance['amount'] = _cash(Decimal(balance['amount']))
        if balance['currency'] != period['currency'] or not end <= _date(balance['booked_on']) <= date.today():
            raise ValueError('balance')
        return {'status': 'ok', 'monthly': monthly, 'period': period, 'balance': balance}
    except BankReadError as error:
        if 'method' in missing:
            return {'status': 'needs_method', 'options': missing['method']}
        if 'medium' in missing:
            return {'status': 'needs_medium', 'options': missing['medium']}
        if error.code is BankErrorCode.AUTH_REJECTED:
            return _error('auth_rejected')
        if error.code in {BankErrorCode.SCA_REQUIRED, BankErrorCode.LOCAL_AUTH_ABORT,
                          BankErrorCode.GRAPHICAL_TAN, BankErrorCode.AUTH_SETUP_REQUIRED,
                          BankErrorCode.AUTH_SELECTION_INVALID, BankErrorCode.TAN_LIMIT}:
            return _error('authorization_required')
        return _diagnostic_error('bank_failure', stage, error.code)
    except (ValueError, TypeError, OverflowError):
        return _diagnostic_error('invalid_bank_result', stage)
    except Exception:
        return _diagnostic_error('bank_failure', stage)


def _real_vault():
    from finance_control.security.server_vault import APPROVED_DIRECTORY, ServerCredentialStore
    return ServerCredentialStore(APPROVED_DIRECTORY)


def main():
    logging.disable(2**63 - 1)
    result = _error('invalid_request')
    try:
        payload = sys.stdin.buffer.read(_MAX_INPUT + 1)
        if sys.platform != 'linux':
            result = _error('unsupported_platform')
        elif 0 < len(payload) <= _MAX_INPUT:
            request = json.loads(payload.decode('utf-8'))
            with open(os.devnull, 'w', encoding='utf-8') as sink:
                with redirect_stdout(sink), redirect_stderr(sink):
                    from .fints_readonly import create_reader
                    result = run_request(request, _real_vault, create_reader)
    except Exception:
        result = _error('invalid_request')
    encoded = json.dumps(result, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
    if len(encoded) > _MAX_OUTPUT:
        encoded = b'{"status":"error","code":"invalid_bank_result"}'
    sys.stdout.buffer.write(encoded + b'\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
