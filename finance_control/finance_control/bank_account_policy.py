"""Bank-specific account kinds accepted by direct statement imports."""

from __future__ import annotations


_BANK_ACCOUNT_KINDS = {
    'ING': frozenset({'CHECKING', 'SAVINGS'}),
    'POSTBANK': frozenset({'CHECKING', 'SAVINGS'}),
    'NASPA': frozenset({'CHECKING'}),
}


def supported_bank_account_kinds(bank_id):
    """Return the directly importable account kinds for a known bank."""
    if type(bank_id) is not str:
        return frozenset()
    return _BANK_ACCOUNT_KINDS.get(bank_id, frozenset())


def bank_account_kind_supported(bank_id, kind, currency):
    """Direct bank imports currently support EUR checking and savings accounts."""
    return (type(currency) is str and currency == 'EUR'
            and type(kind) is str and kind in supported_bank_account_kinds(bank_id))
