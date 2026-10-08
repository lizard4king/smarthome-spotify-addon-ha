"""One-shot, read-only FinTS worker with a bounded secret-free JSON channel."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from decimal import Decimal

from .fints_readonly import AccountRef, Balance, BankErrorCode, BankReadError, ReadOperation
from .server_bank_rules import valid_auth_selection, valid_product_id


_FIELDS = {'connection_id', 'owner_user_id', 'bank_id', 'bank_code', 'product_id',
           'tan_method', 'tan_medium'}
_ID = re.compile(r'[0-9a-f]{32}\Z')
_BANK_CODE = re.compile(r'[0-9]{8}\Z')
_CURRENCY = re.compile(r'[A-Z]{3}\Z')
_BANKS = {'ING', 'POSTBANK', 'NASPA'}
_MAX_INPUT = 16 * 1024
_MAX_ACCOUNTS = 20


def _error(code):
    return {'status': 'error', 'code': code}


def _valid(request):
    if type(request) is not dict or set(request) != _FIELDS:
        return False
    if any(type(request[name]) is not str or _ID.fullmatch(request[name]) is None
           for name in ('connection_id', 'owner_user_id')):
        return False
    if (type(request['bank_id']) is not str or request['bank_id'] not in _BANKS
            or type(request['bank_code']) is not str
            or _BANK_CODE.fullmatch(request['bank_code']) is None
            or not valid_product_id(request['product_id'])):
        return False
    return valid_auth_selection(request['bank_id'], request['tan_method'], request['tan_medium'])


def _account_identity(account):
    if type(account) is not AccountRef:
        raise ValueError('invalid account')
    values = (account.iban, account.bic, account.accountnumber,
              account.subaccount, account.blz)
    if (any(type(value) is not str or len(value) > 100
            or any(not character.isprintable() for character in value) for value in values)
            or not (account.iban or account.accountnumber)):
        raise ValueError('invalid account')
    return values


def _fingerprint(request, values):
    payload = json.dumps([request['bank_id'], request['owner_user_id'],
                          request['connection_id'], *values], separators=(',', ':'),
                         ensure_ascii=False).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


def _masked(values):
    reference = ''.join(char for char in (values[0] or values[2]) if char.isalnum())
    return '••••' + reference[-4:] if len(reference) >= 4 else '••••'


def _balance(balance):
    if (type(balance) is not Balance or type(balance.amount) is not Decimal
            or not balance.amount.is_finite()
            or type(balance.currency) is not str or _CURRENCY.fullmatch(balance.currency) is None
            or type(balance.booked_on) is not date):
        raise ValueError('invalid balance')
    # Avoid scientific notation and exceptionally large bank values on the IPC channel.
    amount = format(balance.amount, 'f')
    if len(amount) > 80:
        raise ValueError('invalid balance')
    return {'amount': amount, 'currency': balance.currency,
            'booked_on': balance.booked_on.isoformat()}


def _choose_exact(options, label):
    if label is None:
        return None
    matches = [index for index, option in enumerate(options)
               if type(getattr(option, 'label', None)) is str and option.label == label]
    return matches[0] if len(matches) == 1 else None


def run_request(request, vault_factory, reader_factory):
    """Return only fixed errors or validated account/balance DTOs; no persistence."""
    if not _valid(request):
        return _error('invalid_request')
    try:
        vault = vault_factory()
        credentials = vault.load(request['connection_id'], request['owner_user_id'])
    except Exception:
        return _error('vault_unavailable')
    try:
        def reject_challenge(_challenge):
            raise BankReadError(code=BankErrorCode.SCA_REQUIRED)

        reader = reader_factory(request['bank_id'], request['bank_code'],
                                request['product_id'], credentials, reject_challenge,
                                tan_method=request['tan_method'])
        reader.configure_auth(
            lambda options: None,  # Explicit method key is passed to create_reader.
            lambda options: _choose_exact(options, request['tan_medium']))
        accounts = reader.read(ReadOperation.ACCOUNTS)
        if type(accounts) not in (tuple, list):
            return _error('invalid_bank_result')
        if len(accounts) > _MAX_ACCOUNTS:
            return _error('too_many_accounts')
        result, seen = [], set()
        for account in accounts:
            values = _account_identity(account)
            fingerprint = _fingerprint(request, values)
            if fingerprint in seen:
                return _error('duplicate_account')
            seen.add(fingerprint)
            balance = _balance(reader.read(ReadOperation.BALANCE, account))
            result.append({'fingerprint': fingerprint, 'masked_account': _masked(values),
                           **balance})
        return {'status': 'ok', 'accounts': result}
    except BankReadError as error:
        if error.code in {BankErrorCode.SCA_REQUIRED, BankErrorCode.LOCAL_AUTH_ABORT,
                          BankErrorCode.GRAPHICAL_TAN, BankErrorCode.AUTH_SETUP_REQUIRED,
                          BankErrorCode.AUTH_SELECTION_INVALID, BankErrorCode.TAN_LIMIT}:
            return _error('authorization_required')
        return _error('bank_failure')
    except (ValueError, TypeError, UnicodeError, OverflowError):
        return _error('invalid_bank_result')
    except Exception:
        return _error('bank_failure')


def _real_vault():
    from finance_control.security.server_vault import APPROVED_DIRECTORY, ServerCredentialStore

    return ServerCredentialStore(APPROVED_DIRECTORY)


def main():
    """One JSON request on stdin, one bounded JSON response on stdout."""
    logging.disable(2**63 - 1)  # This process performs banking only and then exits.
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
    encoded = json.dumps(result, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    if len(encoded) > _MAX_INPUT:
        encoded = b'{"status":"error","code":"invalid_bank_result"}'
    sys.stdout.buffer.write(encoded + b'\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
