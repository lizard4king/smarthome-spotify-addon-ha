"""Strict, text-free balance evidence for an owner-bound failed preview."""

from calendar import monthrange
from datetime import date
import re
from typing import TypedDict, cast

from .core import money


class ReconciliationProof(TypedDict):
    """Confidential financial values, only for the protected owner's error job."""
    period_start: str
    period_end: str
    currency: str
    ledger_opening_date: str
    ledger_initial_balance: str
    ledger_opening_balance: str
    ledger_closing_balance: str
    bank_opening_balance: str
    bank_closing_balance: str
    ledger_booking_count: int
    bank_booking_count: int


_DATES = ('period_start', 'period_end', 'ledger_opening_date')
_BALANCES = ('ledger_initial_balance', 'ledger_opening_balance',
             'ledger_closing_balance', 'bank_opening_balance', 'bank_closing_balance')
_COUNTS = ('ledger_booking_count', 'bank_booking_count')
_FIELDS = frozenset((*_DATES, *_BALANCES, *_COUNTS, 'currency'))
_AMOUNT = re.compile(r'-?(?:0|[1-9][0-9]{0,11})\.[0-9]{2}\Z')


def validated_reconciliation(value) -> ReconciliationProof | None:
    """Validate confidential owner-job evidence, never a public bank diagnostic."""
    if (type(value) is not dict or any(type(key) is not str for key in value)
            or set(value) != _FIELDS):
        return None
    try:
        dates = {}
        for key in _DATES:
            text = value[key]
            if type(text) is not str or len(text) != 10:
                return None
            parsed = date.fromisoformat(text)
            if parsed.isoformat() != text:
                return None
            dates[key] = parsed
        start, end = dates['period_start'], dates['period_end']
        if (start.day != 1 or end != date(start.year, start.month,
                                          monthrange(start.year, start.month)[1])
                or dates['ledger_opening_date'] >= start):
            return None
        currency = value['currency']
        if type(currency) is not str or re.fullmatch(r'[A-Z]{3}', currency) is None:
            return None
        for key in _BALANCES:
            text = value[key]
            if (type(text) is not str or len(text) > 16
                    or _AMOUNT.fullmatch(text) is None or text == '-0.00'):
                return None
            money(text)
        if any(type(value[key]) is not int or not 0 <= value[key] <= 1_000_000
               for key in _COUNTS):
            return None
    except (ValueError, TypeError, ArithmeticError):
        return None
    return cast(ReconciliationProof, value.copy())


class LedgerControlBalanceMismatch(ValueError):
    """Static reason with confidential RAM evidence for the protected owner job."""

    def __init__(self, *, reconciliation=None):
        super().__init__('ledger_control_balance_mismatch')
        self.reconciliation = validated_reconciliation(reconciliation)
