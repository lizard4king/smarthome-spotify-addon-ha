"""Owner-bound bridge for bounded Giro account and period reads."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from .administration import AdministrationError, _id, _revision
from .connectors.fints_readonly import Balance
from .connectors.ing_period_snapshot import PeriodRow, PeriodSnapshot, validate_period
from .connectors.mt940_statements import MonthlySnapshot, StatementRow
from .connectors.server_bank_rules import valid_auth_selection, valid_product_id
from .server_balance_gateway import ServerBalanceGateway, _BANK_READ_LOCK
from .history_read import validated_history


_FP = re.compile(r'[0-9a-f]{64}\Z')
_MASK = re.compile(r'••••(?:[A-Za-z0-9]{4})?\Z')
_MONEY = re.compile(r'-?[0-9]{1,18}\.[0-9]{2}\Z')
_CURRENCY = re.compile(r'[A-Z]{3}\Z')
_SOURCE = re.compile(r'[^\x00-\x20\x7f]{1,120}\Z')
_MAX_OUTPUT = 8 * 1024 * 1024
_MAX_ROWS = 10_000
_MAX_TEXT = 8192
_TIMEOUT = 300
_ERROR_CODES = frozenset({'invalid_request', 'vault_unavailable', 'authorization_required',
                          'auth_rejected',
                          'bank_failure', 'invalid_bank_result', 'unknown_account',
                          'unsupported_platform'})
_DIAGNOSTIC_STAGES = frozenset({'accounts', 'control_month', 'period', 'balance'})
_DIAGNOSTIC_BANK_CODES = frozenset({
    'ONLINE_LOGIN_REQUIRED', 'CREDENTIALS_REJECTED', 'AUTH_TEMPORARY',
    'UNSUPPORTED', 'CONNECTION', 'TIMEOUT', 'TLS', 'DIALOG_INIT',
    'NO_RESPONSE', 'BANK_REJECTED', 'UNKNOWN', 'DATA_FORMAT',
    'IDENTIFICATION_FORMAT', 'PRODUCT_FORMAT', 'STATEMENT_INCOMPLETE',
    'STATEMENT_FORMAT', 'STATEMENT_ID_MISSING',
})
_BANK_CODES = {'POSTBANK': '50010060', 'ING': '50010517',
               'NASPA': '51050015'}


def _fail(code='bank_read_unavailable', status=503):
    raise AdministrationError(code, status)


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


def _date(value):
    if type(value) is not str or len(value) != 10:
        raise ValueError('date')
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError('date')
    return parsed


def _money(value):
    if type(value) is not str or _MONEY.fullmatch(value) is None:
        raise ValueError('money')
    amount = Decimal(value)
    if not amount.is_finite() or abs(amount) >= Decimal('1e18'):
        raise ValueError('money')
    return amount


def _text(value):
    if (type(value) is not str or len(value) > _MAX_TEXT
            or any((ord(c) < 32 and c not in '\n\t') or ord(c) == 127
                   or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        raise ValueError('text')
    return value


def _source(value):
    if type(value) is not str or _SOURCE.fullmatch(value) is None:
        raise ValueError('source')
    return value


def _currency(value):
    if type(value) is not str or _CURRENCY.fullmatch(value) is None:
        raise ValueError('currency')
    return value


def validated_diagnostic(value):
    """Accept only the fixed, non-sensitive worker diagnostic vocabulary."""
    if (type(value) is not dict
            or set(value) not in ({'stage'}, {'stage', 'bank_error_code'})
            or type(value['stage']) is not str
            or value['stage'] not in _DIAGNOSTIC_STAGES):
        raise ValueError('diagnostic')
    if 'bank_error_code' in value and (
            type(value['bank_error_code']) is not str
            or value['bank_error_code'] not in _DIAGNOSTIC_BANK_CODES):
        raise ValueError('diagnostic')
    return value.copy()


def _monthly(value, start, bank_id):
    if type(value) is not dict or set(value) != {
            'source_profile', 'source_account', 'period_start', 'period_end',
            'opening_date', 'closing_date', 'opening_balance', 'closing_balance',
            'currency', 'rows'} or value['source_profile'] != bank_id:
        raise ValueError('monthly')
    end = start.replace(day=28) + timedelta(days=4)
    end = end.replace(day=1) - timedelta(days=1)
    if type(value['rows']) is not list or len(value['rows']) > _MAX_ROWS:
        raise ValueError('rows')
    currency = _currency(value['currency'])
    rows = []
    for item in value['rows']:
        if type(item) is not dict or set(item) != {'booked_on', 'amount', 'currency'}:
            raise ValueError('row')
        booked = _date(item['booked_on'])
        if not start <= booked <= end or item['currency'] != currency:
            raise ValueError('row')
        rows.append(StatementRow(booked, _money(item['amount']), currency))
    snapshot = MonthlySnapshot(bank_id, _source(value['source_account']),
                               _date(value['period_start']), _date(value['period_end']),
                               _date(value['opening_date']), _date(value['closing_date']),
                               _money(value['opening_balance']), _money(value['closing_balance']),
                               currency, tuple(rows))
    if (snapshot.period_start != start or snapshot.period_end != end
            or snapshot.opening_date > start or snapshot.closing_date != end
            or snapshot.opening_balance + sum((r.amount for r in rows), Decimal(0))
            != snapshot.closing_balance):
        raise ValueError('monthly')
    return snapshot


def _period(value, start, end, bank_id):
    if type(value) is not dict or set(value) != {
            'source_profile', 'source_account', 'month_start', 'as_of',
            'opening_date', 'closing_date', 'opening_balance', 'closing_balance',
            'currency', 'rows'} or value['source_profile'] != bank_id:
        raise ValueError('period')
    if type(value['rows']) is not list or len(value['rows']) > _MAX_ROWS:
        raise ValueError('rows')
    rows = []
    for item in value['rows']:
        if type(item) is not dict or set(item) != {
                'booked_on', 'value_on', 'amount', 'currency', 'counterparty',
                'description', 'booking_text'}:
            raise ValueError('row')
        rows.append(PeriodRow(_date(item['booked_on']), _date(item['value_on']),
                              _money(item['amount']), _currency(item['currency']),
                              _text(item['counterparty']), _text(item['description']),
                              _text(item['booking_text'])))
    snapshot = PeriodSnapshot(bank_id, _source(value['source_account']),
                              _date(value['month_start']), _date(value['as_of']),
                              _date(value['opening_date']), _date(value['closing_date']),
                              _money(value['opening_balance']), _money(value['closing_balance']),
                              _currency(value['currency']), tuple(rows))
    validate_period(snapshot)
    if snapshot.month_start != start or not start <= snapshot.as_of <= end:
        raise ValueError('period')
    return snapshot


def _validated_output(payload, request):
    if type(payload) is not bytes or len(payload) > _MAX_OUTPUT:
        _fail()
    try:
        result = json.loads(payload.decode('utf-8'), object_pairs_hook=_pairs,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if type(result) is not dict:
            raise ValueError('result')
        if result.get('status') == 'error':
            if (set(result) not in ({'status', 'code'}, {'status', 'code', 'diagnostic'})
                    or type(result['code']) is not str
                    or result['code'] not in _ERROR_CODES):
                raise ValueError('error')
            if 'diagnostic' in result:
                if result['code'] not in ('bank_failure', 'invalid_bank_result'):
                    raise ValueError('diagnostic')
                diagnostic = validated_diagnostic(result['diagnostic'])
                if result['code'] == 'invalid_bank_result' and 'bank_error_code' in diagnostic:
                    raise ValueError('diagnostic')
            return result
        if result.get('status') in ('needs_method', 'needs_medium'):
            if set(result) != {'status', 'options'} or type(result['options']) is not list or not 1 <= len(result['options']) <= 20:
                raise ValueError('options')
            method = result['status'] == 'needs_method'
            if (method and request['tan_method'] is not None) or (not method and request['tan_medium'] is not None):
                raise ValueError('options')
            seen = set()
            for item in result['options']:
                if type(item) is not dict or set(item) != ({'key', 'label'} if method else {'label'}):
                    raise ValueError('option')
                label = item['label']
                if type(label) is not str or not 1 <= len(label) <= 80 or not label.isprintable():
                    raise ValueError('option')
                key = item['key'] if method else label
                if key in seen:
                    raise ValueError('duplicate option')
                seen.add(key)
                if method and (type(key) is not str or re.fullmatch(r'[0-9]{1,8}', key) is None):
                    raise ValueError('key')
            return result
        if result.get('status') != 'ok':
            raise ValueError('status')
        if request['action'] == 'history':
            if set(result) != {'status', 'history'} or request['bank_id'] != 'ING':
                raise ValueError('history')
            return {'status': 'ok', 'history': validated_history(
                result['history'], _date(request['month_start']), _date(request['as_of']))}
        if request['action'] == 'accounts':
            if set(result) != {'status', 'accounts'} or type(result['accounts']) is not list or len(result['accounts']) > 20:
                raise ValueError('accounts')
            seen = set()
            for item in result['accounts']:
                if (type(item) is not dict or set(item) != {'fingerprint', 'masked_account'}
                        or type(item['fingerprint']) is not str or _FP.fullmatch(item['fingerprint']) is None
                        or item['fingerprint'] in seen or type(item['masked_account']) is not str
                        or _MASK.fullmatch(item['masked_account']) is None):
                    raise ValueError('account')
                seen.add(item['fingerprint'])
            return result
        if set(result) != {'status', 'monthly', 'period', 'balance'}:
            raise ValueError('result')
        start, end = _date(request['month_start']), _date(request['as_of'])
        previous_end = start - timedelta(days=1)
        previous = date(previous_end.year, previous_end.month, 1)
        bank_id = request['bank_id']
        monthly = _monthly(result['monthly'], previous, bank_id)
        period = _period(result['period'], start, end, bank_id)
        if (monthly.source_account != period.source_account
                or monthly.currency != period.currency
                or monthly.closing_balance != period.opening_balance):
            raise ValueError('continuity')
        balance = result['balance']
        if type(balance) is not dict or set(balance) != {'amount', 'currency', 'booked_on'}:
            raise ValueError('balance')
        booked = _date(balance['booked_on'])
        if not period.as_of <= booked <= date.today() or balance['currency'] != period.currency:
            raise ValueError('balance')
        return {'status': 'ok', 'monthly': monthly, 'period': period,
                'balance': Balance(_money(balance['amount']), period.currency, booked)}
    except (ValueError, TypeError, KeyError, InvalidOperation, OverflowError):
        _fail()


def _run_subprocess(payload):
    process = subprocess.Popen(
        [sys.executable, '-m', 'finance_control.connectors.server_transactions_worker'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, shell=False)
    answer = [None]

    def exchange():
        try:
            process.stdin.write(payload)
            process.stdin.close()
            output = process.stdout.read(_MAX_OUTPUT + 1)
            answer[0] = (output, process.wait())
        except Exception:
            answer[0] = None

    thread = threading.Thread(target=exchange, daemon=True)
    thread.start()
    try:
        thread.join(_TIMEOUT)
        if thread.is_alive():
            _fail('bank_read_timeout', 504)
        if answer[0] is None or answer[0][1] != 0 or len(answer[0][0]) > _MAX_OUTPUT:
            _fail()
        return answer[0][0]
    finally:
        try:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass
        thread.join(5)
        for pipe in (process.stdin, process.stdout):
            try:
                pipe.close()
            except (OSError, ValueError):
                pass


class ServerTransactionsGateway:
    """Only validated private dataclasses cross into the server service."""

    def __init__(self, administration, product_id, runner=None):
        self.administration = administration
        self.product_id = product_id
        self.runner = runner or _run_subprocess
        self._balance = ServerBalanceGateway(administration, product_id)

    def _connection(self, actor, connection_id, revision):
        owner_id, bank_id, bank_code = self._balance._connection(
            actor, _id(connection_id), _revision(revision))
        if _BANK_CODES.get(bank_id) != bank_code:
            raise AdministrationError('invalid_bank')
        return owner_id, bank_id, bank_code

    def check(self, actor, connection_id, revision):
        owner_id, _, _ = self._connection(actor, connection_id, revision)
        return owner_id

    def profile(self, actor, connection_id, revision):
        _, bank_id, _ = self._connection(actor, connection_id, revision)
        return bank_id

    def read(self, actor, data):
        if sys.platform != 'linux':
            _fail('server_banking_unsupported')
        if type(data) is not dict or set(data) != {
                'id', 'revision', 'confirmed', 'tan_method', 'tan_medium',
                'action', 'account_fingerprint', 'month_start', 'as_of'}:
            raise AdministrationError('invalid_action')
        if data['confirmed'] is not True:
            raise AdministrationError('confirmation_required')
        connection_id, revision = _id(data['id']), _revision(data['revision'])
        owner_id, bank_id, bank_code = self._connection(actor, connection_id, revision)
        if not valid_product_id(self.product_id):
            _fail('bank_product_unavailable')
        if not valid_auth_selection(bank_id, data['tan_method'], data['tan_medium']):
            raise AdministrationError('invalid_bank_auth_selection')
        request = {'connection_id': connection_id, 'owner_user_id': owner_id,
                   'bank_id': bank_id, 'bank_code': bank_code, 'product_id': self.product_id,
                   'tan_method': data['tan_method'], 'tan_medium': data['tan_medium'],
                   'action': data['action'], 'account_fingerprint': data['account_fingerprint'],
                   'month_start': data['month_start'], 'as_of': data['as_of']}
        from .connectors.server_transactions_worker import _valid
        if not _valid(request):
            raise AdministrationError('invalid_action')
        payload = json.dumps(request, separators=(',', ':')).encode('utf-8')
        if not _BANK_READ_LOCK.acquire(blocking=False):
            raise AdministrationError('bank_read_busy', 409)
        try:
            try:
                response = self.runner(payload)
            except AdministrationError:
                raise
            except Exception:
                _fail()
            if self._connection(actor, connection_id, revision) != (owner_id, bank_id, bank_code):
                raise AdministrationError('stale_revision', 409)
            return _validated_output(response, request)
        finally:
            _BANK_READ_LOCK.release()
