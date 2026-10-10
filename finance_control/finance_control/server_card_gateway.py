"""Owner-bound, one-shot Postbank Mastercard read boundary."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
from datetime import date
from decimal import Decimal

from .administration import AdministrationError, _id, _revision
from .connectors.server_bank_rules import valid_auth_selection, valid_product_id
from .server_balance_gateway import ServerBalanceGateway, _BANK_READ_LOCK

_ID = re.compile(r'[0-9a-f]{32}\Z')
_MONEY = re.compile(r'-?[0-9]{1,18}\.[0-9]{2}\Z')
_RATE = re.compile(r'[0-9]{1,18}(?:\.[0-9]{1,18})?\Z')
_CURRENCY = re.compile(r'[A-Z]{3}\Z')
_ERRORS = frozenset({'invalid_request', 'vault_unavailable', 'authorization_required',
                     'auth_rejected', 'bank_failure', 'invalid_bank_result',
                     'unknown_account', 'unsupported_platform', 'card_operation_unsupported'})
_MAX_OUTPUT = 8 * 1024 * 1024
_MAX_ROWS = 10_000
_TIMEOUT = 300
_BOOKING = {'receipt_date', 'booking_date', 'billing_date', 'value_date', 'amount',
            'currency', 'original_amount', 'original_currency', 'original_exchange_rate',
            'billed', 'descriptions', 'merchant_name', 'country_code', 'terminal_id',
            'booking_reference', 'fee_code', 'billing_label', 'atm_fee_reference',
            'foreign_use_fee_reference'}
_BALANCE = {'amount', 'currency', 'as_of', 'available_amount', 'available_currency',
            'open_authorizations', 'credit_limit', 'last_billing_date',
            'expected_billing_date'}


def _fail(code='bank_read_unavailable', status=503):
    raise AdministrationError(code, status)


def _pairs(items):
    value = {}
    for key, item in items:
        if key in value:
            raise ValueError('duplicate')
        value[key] = item
    return value


def _date(value, optional=False):
    if optional and value is None:
        return None
    if type(value) is not str or len(value) != 10 or date.fromisoformat(value).isoformat() != value:
        raise ValueError('date')
    return value


def _money(value, optional=False):
    if optional and value is None:
        return None
    if type(value) is not str or _MONEY.fullmatch(value) is None or not Decimal(value).is_finite():
        raise ValueError('money')
    return value


def _currency(value, optional=False):
    if optional and value is None:
        return None
    if type(value) is not str or _CURRENCY.fullmatch(value) is None:
        raise ValueError('currency')
    return value


def _text(value, optional=True):
    if optional and value is None:
        return None
    if (type(value) is not str or len(value) > 8192
            or any((ord(c) < 32 and c not in '\t\n') or ord(c) == 127
                   or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        raise ValueError('text')
    return value


def _balance(value):
    if type(value) is not dict or set(value) != _BALANCE:
        raise ValueError('balance')
    for key in ('amount',):
        _money(value[key])
    for key in ('available_amount', 'open_authorizations', 'credit_limit'):
        _money(value[key], True)
    _currency(value['currency'])
    _currency(value['available_currency'], True)
    if (value['available_amount'] is None) != (value['available_currency'] is None):
        raise ValueError('available')
    if date.fromisoformat(_date(value['as_of'])) > date.today():
        raise ValueError('future balance')
    for key in ('last_billing_date', 'expected_billing_date'):
        _date(value[key], True)
    return value


def _booking(value, start, end):
    if type(value) is not dict or set(value) != _BOOKING:
        raise ValueError('booking')
    if not start <= date.fromisoformat(_date(value['booking_date'])) <= end:
        raise ValueError('period')
    _date(value['receipt_date'])
    for key in ('billing_date', 'value_date'):
        _date(value[key], True)
    _money(value['amount'])
    _currency(value['currency'])
    _money(value['original_amount'], True)
    _currency(value['original_currency'], True)
    rate = value['original_exchange_rate']
    if rate is not None and (type(rate) is not str or _RATE.fullmatch(rate) is None
                             or Decimal(rate) <= 0):
        raise ValueError('rate')
    if (value['original_amount'] is None) != (value['original_currency'] is None):
        raise ValueError('original')
    if value['billed'] is not None and type(value['billed']) is not bool:
        raise ValueError('billed')
    descriptions = value['descriptions']
    if type(descriptions) is not list or len(descriptions) != 4:
        raise ValueError('descriptions')
    for pair in descriptions:
        if type(pair) is not list or len(pair) != 2:
            raise ValueError('description')
        for part in pair:
            _text(part)
    for key in _BOOKING - {'receipt_date', 'booking_date', 'billing_date', 'value_date',
                            'amount', 'currency', 'original_amount', 'original_currency',
                            'original_exchange_rate', 'billed', 'descriptions'}:
        _text(value[key])
    return value


def _validated_output(payload, request):
    if type(payload) is not bytes or len(payload) > _MAX_OUTPUT:
        _fail()
    try:
        result = json.loads(payload.decode('utf-8'), object_pairs_hook=_pairs,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if type(result) is not dict:
            raise ValueError('result')
        if result.get('status') == 'error':
            if set(result) != {'status', 'code'} or result['code'] not in _ERRORS:
                raise ValueError('error')
            return result
        if result.get('status') in ('needs_method', 'needs_medium'):
            method = result['status'] == 'needs_method'
            if (set(result) != {'status', 'options'} or type(result['options']) is not list
                    or not 1 <= len(result['options']) <= 20
                    or request['tan_method' if method else 'tan_medium'] is not None):
                raise ValueError('options')
            seen = set()
            for item in result['options']:
                if type(item) is not dict or set(item) != ({'key', 'label'} if method else {'label'}):
                    raise ValueError('option')
                _text(item['label'], False)
                if not 1 <= len(item['label']) <= 80 or not item['label'].isprintable():
                    raise ValueError('label')
                key = item['key'] if method else item['label']
                if key in seen or (method and (type(key) is not str or
                                             re.fullmatch(r'[0-9]{1,8}', key) is None)):
                    raise ValueError('key')
                seen.add(key)
            return result
        if set(result) != {'status', 'card_masked', 'balance', 'transactions',
                           'last_billing_date', 'expected_billing_date'} or result['status'] != 'ok':
            raise ValueError('result')
        if (type(result['card_masked']) is not str or
                re.fullmatch(r'•••• [0-9]{4}', result['card_masked']) is None):
            raise ValueError('mask')
        _balance(result['balance'])
        _date(result['last_billing_date'], True)
        _date(result['expected_billing_date'], True)
        rows = result['transactions']
        if type(rows) is not list or len(rows) > _MAX_ROWS:
            raise ValueError('rows')
        start, end = date.fromisoformat(request['start']), date.fromisoformat(request['end'])
        for row in rows:
            _booking(row, start, end)
        return result
    except (ValueError, TypeError, KeyError, OverflowError):
        _fail()


def _run_subprocess(payload):
    process = subprocess.Popen(
        [sys.executable, '-m', 'finance_control.connectors.server_card_worker'],
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


class ServerCardGateway:
    def __init__(self, administration, product_id, runner=None):
        self.product_id = product_id
        self.runner = runner or _run_subprocess
        self._balance_gateway = ServerBalanceGateway(administration, product_id)

    def read(self, actor, data):
        if sys.platform != 'linux':
            _fail('server_banking_unsupported')
        if type(data) is not dict or set(data) != {
                'id', 'revision', 'confirmed', 'card_id', 'tan_method', 'tan_medium',
                'start', 'end'}:
            raise AdministrationError('invalid_action')
        if data['confirmed'] is not True:
            raise AdministrationError('confirmation_required')
        connection_id, revision = _id(data['id']), _revision(data['revision'])
        owner_id, bank_id, bank_code = self._balance_gateway._connection(actor, connection_id, revision)
        if bank_id != 'POSTBANK' or bank_code != '50010060':
            raise AdministrationError('invalid_bank')
        if not valid_product_id(self.product_id):
            _fail('bank_product_unavailable')
        if not valid_auth_selection(bank_id, data['tan_method'], data['tan_medium']):
            raise AdministrationError('invalid_bank_auth_selection')
        request = {'connection_id': connection_id, 'owner_user_id': owner_id,
                   'bank_id': bank_id, 'bank_code': bank_code,
                   'product_id': self.product_id, 'card_id': data['card_id'],
                   'tan_method': data['tan_method'], 'tan_medium': data['tan_medium'],
                   'start': data['start'], 'end': data['end']}
        from .connectors.server_card_worker import _valid
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
            if self._balance_gateway._connection(actor, connection_id, revision) != (owner_id, bank_id, bank_code):
                raise AdministrationError('stale_revision', 409)
            return _validated_output(response, request)
        finally:
            _BANK_READ_LOCK.release()
