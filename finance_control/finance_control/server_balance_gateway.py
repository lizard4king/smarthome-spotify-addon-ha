"""Owner-bound one-shot bridge to the isolated read-only banking worker."""

from __future__ import annotations

import json
import queue
import re
import subprocess
import sys
import threading
from datetime import date
from decimal import Decimal, InvalidOperation

from .administration import AdministrationError, BANKS, _id, _revision
from .connectors.server_bank_rules import (
    BALANCE_DIAGNOSTIC_BANK_CODES, BALANCE_ISOLATED_BANK_CODES,
    valid_auth_selection, valid_product_id,
)


_FINGERPRINT = re.compile(r'[0-9a-f]{64}\Z')
_MASK = re.compile(r'••••(?:[A-Za-z0-9]{4})?\Z')
_AMOUNT = re.compile(r'-?[0-9]+(?:\.[0-9]+)?\Z')
_CURRENCY = re.compile(r'[A-Z]{3}\Z')
_MAX_OUTPUT = 16 * 1024
_TIMEOUT = 90
_BANK_READ_LOCK = threading.Lock()
_ERROR_CODES = {
    'invalid_request', 'vault_unavailable', 'authorization_required', 'auth_rejected',
    'bank_failure', 'invalid_bank_result', 'too_many_accounts',
    'duplicate_account', 'unsupported_platform',
}
_DIAGNOSTIC_STAGES = frozenset({'accounts', 'balance'})


def validated_diagnostic(value):
    """Accept only fixed stage/code tokens, never bank text or account data."""
    if (type(value) is not dict
            or set(value) not in ({'stage'}, {'stage', 'bank_error_code'})
            or type(value['stage']) is not str
            or value['stage'] not in _DIAGNOSTIC_STAGES):
        raise ValueError('diagnostic')
    if 'bank_error_code' in value and (
            type(value['bank_error_code']) is not str
            or value['bank_error_code'] not in BALANCE_DIAGNOSTIC_BANK_CODES):
        raise ValueError('diagnostic')
    return value.copy()


def _fail(code='bank_read_unavailable', status=503):
    raise AdministrationError(code, status)


def _parse_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _validated_output(payload):
    if type(payload) is not bytes or len(payload) > _MAX_OUTPUT:
        _fail()
    try:
        result = json.loads(payload.decode('utf-8'), object_pairs_hook=_parse_pairs,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, TypeError):
        _fail()
    if type(result) is not dict:
        _fail()
    if result.get('status') == 'error':
        if (set(result) not in ({'status', 'code'}, {'status', 'code', 'diagnostic'})
                or type(result['code']) is not str
                or result['code'] not in _ERROR_CODES):
            _fail()
        if 'diagnostic' in result:
            try:
                diagnostic = validated_diagnostic(result['diagnostic'])
            except ValueError:
                _fail()
            if (result['code'] not in {'bank_failure', 'invalid_bank_result', 'auth_rejected',
                                       'authorization_required', 'too_many_accounts', 'duplicate_account'}
                    or (result['code'] != 'bank_failure' and 'bank_error_code' in diagnostic)):
                _fail()
        return result
    if (result.get('status') != 'ok'
            or set(result) not in ({'status', 'accounts'}, {'status', 'accounts', 'account_errors'})):
        _fail()
    accounts = result['accounts']
    account_errors = result.get('account_errors', [])
    if (type(accounts) is not list or type(account_errors) is not list
            or len(accounts) + len(account_errors) > 20):
        _fail()
    seen = set()
    for account in accounts:
        if type(account) is not dict or set(account) != {
                'fingerprint', 'masked_account', 'amount', 'currency', 'booked_on'}:
            _fail()
        fingerprint = account['fingerprint']
        mask = account['masked_account']
        amount = account['amount']
        currency = account['currency']
        booked_on = account['booked_on']
        if (type(fingerprint) is not str or _FINGERPRINT.fullmatch(fingerprint) is None
                or fingerprint in seen or type(mask) is not str or _MASK.fullmatch(mask) is None
                or type(amount) is not str or len(amount) > 80
                or _AMOUNT.fullmatch(amount) is None
                or type(currency) is not str or _CURRENCY.fullmatch(currency) is None
                or type(booked_on) is not str):
            _fail()
        seen.add(fingerprint)
        try:
            if not Decimal(amount).is_finite() or date.fromisoformat(booked_on).isoformat() != booked_on:
                _fail()
        except (InvalidOperation, ValueError, OverflowError):
            _fail()
    for item in account_errors:
        if (type(item) is not dict
                or set(item) != {'fingerprint', 'masked_account', 'code', 'diagnostic'}
                or type(item['fingerprint']) is not str
                or _FINGERPRINT.fullmatch(item['fingerprint']) is None
                or item['fingerprint'] in seen
                or type(item['masked_account']) is not str
                or _MASK.fullmatch(item['masked_account']) is None
                or type(item['code']) is not str
                or item['code'] not in {'bank_failure', 'invalid_bank_result'}):
            _fail()
        try:
            diagnostic = validated_diagnostic(item['diagnostic'])
        except ValueError:
            _fail()
        if (diagnostic['stage'] != 'balance'
                or (item['code'] == 'bank_failure'
                    and diagnostic.get('bank_error_code') not in BALANCE_ISOLATED_BANK_CODES)
                or (item['code'] == 'invalid_bank_result' and 'bank_error_code' in diagnostic)):
            _fail()
        seen.add(item['fingerprint'])
    return result


def _run_subprocess(payload):
    """Read at most 16 KiB; terminate a hung or chatty child without captured stderr."""
    process = subprocess.Popen(
        [sys.executable, '-m', 'finance_control.connectors.server_balance_worker'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        shell=False)
    outcome = queue.Queue(maxsize=1)

    def exchange():
        try:
            process.stdin.write(payload)
            process.stdin.close()
            answer = process.stdout.read(_MAX_OUTPUT + 1)
            if len(answer) > _MAX_OUTPUT:
                outcome.put(('oversize', None))
                return
            outcome.put(('ok', (answer, process.wait())))
        except Exception:
            outcome.put(('failed', None))

    thread = threading.Thread(target=exchange, daemon=True)
    thread.start()
    try:
        thread.join(_TIMEOUT)
        if thread.is_alive():
            _fail('bank_read_timeout', 504)
        state, value = outcome.get_nowait()
        if state != 'ok':
            _fail()
        answer, exit_code = value
        if exit_code != 0:
            _fail()
        return answer
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


class ServerBalanceGateway:
    """Never passes credentials through the web process or subprocess stdin."""

    def __init__(self, administration, product_id, runner=None):
        self.administration = administration
        self.product_id = product_id
        self.runner = runner or _run_subprocess

    def _connection(self, actor, connection_id, revision):
        with self.administration._connection() as db:
            owner = self.administration._actor(db, actor)
            row = db.execute(
                "SELECT id,user_id,bank_id,revision FROM app_bank_connections "
                "WHERE id=? AND status!='REVOKED'", (connection_id,)).fetchone()
            if row is None:
                raise AdministrationError('unknown_connection', 404)
            if row['user_id'] != owner['id']:
                raise AdministrationError('forbidden', 403)
            if row['revision'] != revision:
                raise AdministrationError('stale_revision', 409)
            bank = next((entry for entry in BANKS if entry['id'] == row['bank_id']), None)
            if bank is None:
                raise AdministrationError('invalid_bank')
            return owner['id'], bank['id'], bank['bank_code']

    def read(self, actor, data):
        if sys.platform != 'linux':
            _fail('server_banking_unsupported')
        if type(data) is not dict or set(data) != {
                'id', 'revision', 'confirmed', 'tan_method', 'tan_medium'}:
            raise AdministrationError('invalid_action')
        if data['confirmed'] is not True:
            raise AdministrationError('confirmation_required')
        connection_id, revision = _id(data['id']), _revision(data['revision'])
        owner_id, bank_id, bank_code = self._connection(actor, connection_id, revision)
        if not valid_product_id(self.product_id):
            _fail('bank_product_unavailable')
        method, medium = data['tan_method'], data['tan_medium']
        if not valid_auth_selection(bank_id, method, medium):
            raise AdministrationError('invalid_bank_auth_selection')
        request = {'connection_id': connection_id, 'owner_user_id': owner_id,
                   'bank_id': bank_id, 'bank_code': bank_code,
                   'product_id': self.product_id, 'tan_method': method, 'tan_medium': medium}
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
            # Never return a bank result after the owner, status, or revision changed.
            current = self._connection(actor, connection_id, revision)
            if current != (owner_id, bank_id, bank_code):
                raise AdministrationError('stale_revision', 409)
            return _validated_output(response)
        finally:
            _BANK_READ_LOCK.release()
