"""Isolated Postbank C.12 reader; card number stays inside this process."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from decimal import Decimal

from .credit_card import CreditCardBalance, CreditCardBooking, CreditCardMetadata, CreditCardTransactions
from .fints_readonly import AccountRef, BankErrorCode, BankReadError, ReadOperation
from .server_balance_worker import _account_identity, _fingerprint
from .server_bank_rules import valid_auth_selection, valid_product_id
from .server_transactions_worker import _options

_ID = re.compile(r'[0-9a-f]{32}\Z')
_FP = re.compile(r'[0-9a-f]{64}\Z')
_MONEY = re.compile(r'-?[0-9]{1,18}\.[0-9]{2}\Z')
_CURRENCY = re.compile(r'[A-Z]{3}\Z')
_FIELDS = {'connection_id', 'owner_user_id', 'bank_id', 'bank_code', 'product_id',
           'card_id', 'tan_method', 'tan_medium', 'start', 'end'}
_MAX_INPUT = 16 * 1024
_MAX_OUTPUT = 8 * 1024 * 1024
_MAX_ROWS = 10_000


def _error(code):
    return {'status': 'error', 'code': code}


def _date(value):
    if type(value) is not str or len(value) != 10:
        raise ValueError('date')
    result = date.fromisoformat(value)
    if result.isoformat() != value:
        raise ValueError('date')
    return result


def _valid(request):
    if type(request) is not dict or set(request) != _FIELDS:
        return False
    if any(type(request[key]) is not str or _ID.fullmatch(request[key]) is None
           for key in ('connection_id', 'owner_user_id', 'card_id')):
        return False
    if (request['bank_id'] != 'POSTBANK' or request['bank_code'] != '50010060'
            or not valid_product_id(request['product_id'])
            or not valid_auth_selection('POSTBANK', request['tan_method'], request['tan_medium'])):
        return False
    try:
        start, end = _date(request['start']), _date(request['end'])
    except (ValueError, TypeError, OverflowError):
        return False
    return start <= end <= date.today() and (end - start).days <= 366


def _cash(value):
    if type(value) is not Decimal or not value.is_finite() or abs(value) >= Decimal('1e18'):
        raise ValueError('money')
    raw = format(value, '.2f')
    if Decimal(raw) != value or _MONEY.fullmatch(raw) is None:
        raise ValueError('money')
    return raw


def _optional_money(value):
    return None if value is None else _cash(value)


def _rate(value):
    if value is None:
        return None
    if type(value) is not Decimal or not value.is_finite() or not 0 < value < Decimal('1e18'):
        raise ValueError('rate')
    raw = format(value, 'f')
    if len(raw) > 80 or re.fullmatch(r'[0-9]{1,18}(?:\.[0-9]{1,18})?', raw) is None:
        raise ValueError('rate')
    return raw


def _date_out(value):
    if value is None:
        return None
    if type(value) is not date:
        raise ValueError('date')
    return value.isoformat()


def _text(value, pan, mask):
    if value is None:
        return None
    if (type(value) is not str or len(value) > 8192
            or any((ord(c) < 32 and c not in '\n\t') or ord(c) == 127
                   or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        raise ValueError('text')
    masked = value.replace(pan, mask)
    if len(masked) > 8192:
        raise ValueError('text')
    return masked


def _currency(value):
    if type(value) is not str or _CURRENCY.fullmatch(value) is None:
        raise ValueError('currency')
    return value


def _balance(value):
    if (type(value) is not CreditCardBalance or type(value.as_of) is not date
            or value.as_of > date.today()):
        raise ValueError('balance')
    return {'amount': _cash(value.amount), 'currency': _currency(value.currency),
            'as_of': _date_out(value.as_of),
            'available_amount': _optional_money(value.available_amount),
            'available_currency': None if value.available_currency is None else _currency(value.available_currency),
            'open_authorizations': _optional_money(value.open_authorizations),
            'credit_limit': _optional_money(value.credit_limit),
            'last_billing_date': _date_out(value.last_billing_date),
            'expected_billing_date': _date_out(value.expected_billing_date)}


def _booking(value, pan, mask):
    if type(value) is not CreditCardBooking or (value.billed is not None and type(value.billed) is not bool):
        raise ValueError('booking')
    if type(value.descriptions) is not tuple or len(value.descriptions) != 4:
        raise ValueError('descriptions')
    descriptions = []
    for pair in value.descriptions:
        if type(pair) is not tuple or len(pair) != 2:
            raise ValueError('description')
        descriptions.append([_text(pair[0], pan, mask), _text(pair[1], pan, mask)])
    return {'receipt_date': _date_out(value.receipt_date),
            'booking_date': _date_out(value.booking_date),
            'billing_date': _date_out(value.billing_date),
            'value_date': _date_out(value.value_date),
            'amount': _cash(value.amount), 'currency': _currency(value.currency),
            'original_amount': _optional_money(value.original_amount),
            'original_currency': None if value.original_currency is None else _currency(value.original_currency),
            'original_exchange_rate': _rate(value.original_exchange_rate),
            'billed': value.billed, 'descriptions': descriptions,
            'merchant_name': _text(value.merchant_name, pan, mask),
            'country_code': _text(value.country_code, pan, mask),
            'terminal_id': _text(value.terminal_id, pan, mask),
            'booking_reference': _text(value.booking_reference, pan, mask),
            'fee_code': _text(value.fee_code, pan, mask),
            'billing_label': _text(value.billing_label, pan, mask),
            'atm_fee_reference': _text(value.atm_fee_reference, pan, mask),
            'foreign_use_fee_reference': _text(value.foreign_use_fee_reference, pan, mask)}


def _metadata(secret, account):
    # The encrypted record, never an account list, supplies the card number.
    from fints.formals import BankIdentifier, KTI1
    card_number = getattr(secret, 'card_number', None)
    card_account = getattr(secret, 'card_account_number', None)
    if type(card_number) is not str or re.fullmatch(r'[0-9]{16}', card_number) is None:
        raise ValueError('card secret')
    if card_account is not None and (type(card_account) is not str or not 1 <= len(card_account) <= 30):
        raise ValueError('card account')
    native = None
    if account is not None:
        if type(account) is not AccountRef:
            raise ValueError('account')
        _account_identity(account)
        native = KTI1(iban=account.iban or None, bic=account.bic or None,
                      account_number=account.accountnumber or None,
                      subaccount_number=account.subaccount or None,
                      bank_identifier=BankIdentifier(country_identifier='280',
                                                     bank_code=account.blz))
    return CreditCardMetadata(card_number, native, card_account)


def run_request(request, vault_factory, card_factory, reader_factory):
    if not _valid(request):
        return _error('invalid_request')
    try:
        credentials = vault_factory().load(request['connection_id'], request['owner_user_id'])
        secret = card_factory().load(request['connection_id'], request['owner_user_id'], request['card_id'])
    except Exception:
        return _error('vault_unavailable')
    missing = {}
    bestsign = [False]
    try:
        def challenge(value):
            if not value.decoupled or not bestsign[0]:
                return None
            time.sleep(5)
            return True

        reader = reader_factory('POSTBANK', '50010060', request['product_id'], credentials,
                                challenge, tan_method=request['tan_method'])

        def method(options):
            safe = _options(options)
            if any(secret.card_number in item['key'] or secret.card_number in item['label']
                   for item in safe):
                raise ValueError('card number in options')
            if request['tan_method'] is None:
                missing['method'] = safe
                return None
            matches = [i for i, value in enumerate(safe) if value['key'] == request['tan_method']]
            return matches[0] if len(matches) == 1 else None

        def medium(options):
            safe = _options(options, medium=True)
            if any(secret.card_number in item['label'] for item in safe):
                raise ValueError('card number in options')
            if request['tan_medium'] is None:
                missing['medium'] = safe
                return None
            matches = [i for i, value in enumerate(safe) if value['label'] == request['tan_medium']]
            return matches[0] if len(matches) == 1 else None

        reader.configure_auth(method, medium, force_selection=True)
        client = getattr(reader, '_client', None)
        selected = client.get_current_tan_mechanism()
        mechanism = client.get_tan_mechanisms().get(selected)
        bestsign[0] = 'bestsign' in str(getattr(mechanism, 'name', '')).casefold()
        fingerprint = getattr(secret, 'account_fingerprint', None)
        account = None
        if fingerprint is not None:
            if type(fingerprint) is not str or _FP.fullmatch(fingerprint) is None:
                raise ValueError('fingerprint')
            accounts = reader.read(ReadOperation.ACCOUNTS)
            if type(accounts) not in (tuple, list) or len(accounts) > 20:
                raise ValueError('accounts')
            matched = [item for item in accounts
                       if _fingerprint(request, _account_identity(item)) == fingerprint]
            if len(matched) != 1:
                return _error('unknown_account')
            account = matched[0]
        metadata = _metadata(secret, account)
        start, end = _date(request['start']), _date(request['end'])
        transactions = reader.read(ReadOperation.CREDIT_CARD_TRANSACTIONS, metadata, start, end)
        if type(transactions) is not CreditCardTransactions or type(transactions.bookings) is not tuple or len(transactions.bookings) > _MAX_ROWS:
            raise ValueError('transactions')
        current = reader.read(ReadOperation.CREDIT_CARD_BALANCE, metadata)
        result = {'status': 'ok', 'card_masked': metadata.masked_number,
                  'balance': _balance(current),
                  'transactions': [_booking(item, metadata.card_number, metadata.masked_number)
                                   for item in transactions.bookings],
                  'last_billing_date': _date_out(transactions.last_billing_date),
                  'expected_billing_date': _date_out(transactions.expected_billing_date)}
        for item in result['transactions']:
            if not start <= _date(item['booking_date']) <= end:
                raise ValueError('booking period')
        return result
    except BankReadError as error:
        if 'method' in missing:
            return {'status': 'needs_method', 'options': missing['method']}
        if 'medium' in missing:
            return {'status': 'needs_medium', 'options': missing['medium']}
        if error.code is BankErrorCode.AUTH_REJECTED:
            return _error('auth_rejected')
        if error.code is BankErrorCode.UNSUPPORTED:
            return _error('card_operation_unsupported')
        if error.code is BankErrorCode.DATA_FORMAT:
            return _error('invalid_bank_result')
        if error.code in {BankErrorCode.SCA_REQUIRED, BankErrorCode.LOCAL_AUTH_ABORT,
                          BankErrorCode.GRAPHICAL_TAN, BankErrorCode.AUTH_SETUP_REQUIRED,
                          BankErrorCode.AUTH_SELECTION_INVALID, BankErrorCode.TAN_LIMIT}:
            return _error('authorization_required')
        return _error('bank_failure')
    except (ValueError, TypeError, OverflowError):
        return _error('invalid_bank_result')
    except Exception:
        return _error('bank_failure')


def _real_vault():
    from finance_control.security.server_vault import APPROVED_DIRECTORY, ServerCredentialStore
    return ServerCredentialStore(APPROVED_DIRECTORY)


def _real_cards():
    from finance_control.security.server_card_store import ServerCardStore
    from finance_control.security.server_vault import APPROVED_DIRECTORY
    return ServerCardStore(APPROVED_DIRECTORY)


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
                    result = run_request(request, _real_vault, _real_cards, create_reader)
    except Exception:
        result = _error('invalid_request')
    try:
        encoded = json.dumps(result, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
    except Exception:
        encoded = b'{"status":"error","code":"invalid_bank_result"}'
    if len(encoded) > _MAX_OUTPUT:
        encoded = b'{"status":"error","code":"invalid_bank_result"}'
    sys.stdout.buffer.write(encoded + b'\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
