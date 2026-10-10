"""Pure input rules shared by both sides of the server banking boundary."""

from __future__ import annotations

import re


_PRODUCT = re.compile(r'[A-Za-z0-9]{25}\Z')
_TAN_METHOD = re.compile(r'[0-9]{1,8}\Z')

# Wire tokens only: this module must stay independent of reader/gateway imports.
BALANCE_DIAGNOSTIC_BANK_CODES = frozenset({
    'ONLINE_LOGIN_REQUIRED', 'CREDENTIALS_REJECTED', 'AUTH_TEMPORARY',
    'UNSUPPORTED', 'CONNECTION', 'TIMEOUT', 'TLS', 'DIALOG_INIT',
    'NO_RESPONSE', 'BANK_REJECTED', 'UNKNOWN', 'DATA_FORMAT',
    'IDENTIFICATION_FORMAT', 'PRODUCT_FORMAT', 'STATEMENT_INCOMPLETE',
    'STATEMENT_FORMAT', 'STATEMENT_ID_MISSING',
})
BALANCE_ISOLATED_BANK_CODES = frozenset({'UNSUPPORTED', 'DATA_FORMAT'})


def valid_product_id(value):
    return type(value) is str and _PRODUCT.fullmatch(value) is not None


def valid_auth_selection(bank_id, method, medium):
    if method is not None and (type(method) is not str or _TAN_METHOD.fullmatch(method) is None):
        return False
    if medium is not None and (type(medium) is not str or not medium or len(medium) > 32
                               or any(not character.isprintable() for character in medium)):
        return False
    if bank_id == 'ING':
        return method in (None, '999') and medium is None
    return method != '999'
