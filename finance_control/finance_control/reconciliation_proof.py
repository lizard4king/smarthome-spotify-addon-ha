"""Strict, text-free balance evidence for an owner-bound failed preview."""

from calendar import monthrange
from datetime import date, datetime
from decimal import Decimal
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


_COMPARISON_FIELDS = frozenset({
    'version', 'account_id', 'requested_as_of', 'comparison_date', 'currency',
    'control_amount', 'ledger_amount', 'difference', 'status', 'source',
    'control_reference', 'retrieved_at', 'comparison_basis', 'reason', 'warnings',
})
_COMPARISON_REASONS = {'no_control', 'conflicting_controls', 'currency_conflict',
                       'control_predates_opening', 'invalid_ledger', 'invalid_control',
                       'paypal_transaction_date_not_booking_date'}
_COMPARISON_WARNINGS = {'ledger_completeness_unverified', 'older_control',
                        'bank_balance_not_day_end', 'file_balance_not_day_end'}


def validated_balance_comparison(value):
    """Validate the daily comparison without weakening the monthly contract.

    Missing evidence is not zero. Equal numbers do not establish day-end
    timing or completeness. Only static reason codes and source references
    accompany the protected financial amounts.
    """
    if type(value) is not dict or set(value) != _COMPARISON_FIELDS:
        return None
    try:
        if type(value['version']) is not int or value['version'] != 1:
            return None
        if (type(value['account_id']) is not str
                or not 1 <= len(value['account_id']) <= 240
                or not value['account_id'].strip()
                or any(ord(char) < 32 for char in value['account_id'])
                or type(value['currency']) is not str
                or re.fullmatch('[A-Z]{3}', value['currency']) is None):
            return None
        for key in ('requested_as_of', 'comparison_date'):
            text = value[key]
            if key == 'comparison_date' and text is None:
                continue
            if type(text) is not str or date.fromisoformat(text).isoformat() != text:
                return None
        day = value['comparison_date']
        if day is not None and day > value['requested_as_of']:
            return None
        warnings = value['warnings']
        if (type(warnings) is not list or any(type(item) is not str for item in warnings)
                or warnings != sorted(set(warnings))
                or not set(warnings) <= _COMPARISON_WARNINGS
                or 'ledger_completeness_unverified' not in warnings
                or ('older_control' in warnings) != (day is not None and day < value['requested_as_of'])):
            return None
        source = value['source']
        if source not in {None, 'bank', 'statement', 'finanzguru_file'}:
            return None
        reference = value['control_reference']
        if source is None:
            if any(value[key] is not None for key in ('comparison_date', 'control_reference',
                                                       'retrieved_at', 'comparison_basis', 'control_amount')):
                return None
        elif (day is None or type(reference) is not str
              or not 1 <= len(reference) <= 256
              or any(ord(char) < 32 for char in reference)):
            return None
        timestamp = value['retrieved_at']
        if timestamp is not None:
            if (type(timestamp) is not str or len(timestamp) > 64
                    or datetime.fromisoformat(timestamp).utcoffset() is None):
                return None
        basis = value['comparison_basis']
        if basis not in {None, 'day_end', 'intraday'}:
            return None
        if basis == 'intraday':
            warning = {'bank': 'bank_balance_not_day_end',
                       'finanzguru_file': 'file_balance_not_day_end'}.get(source)
            if warning is None or warning not in warnings:
                return None
        elif set(warnings) & {'bank_balance_not_day_end', 'file_balance_not_day_end'}:
            return None
        for key in ('control_amount', 'ledger_amount', 'difference'):
            text = value[key]
            if text is None:
                continue
            pattern = r'-?(?:0|[1-9][0-9]{0,12})\.[0-9]{2}' if key == 'difference' else _AMOUNT
            if (type(text) is not str or re.fullmatch(pattern, text) is None or text == '-0.00'):
                return None
            if key != 'difference':
                money(text)
        status = value['status']
        if status == 'missing':
            if (value['reason'] not in _COMPARISON_REASONS
                    or value['difference'] is not None or value['ledger_amount'] is not None):
                return None
        elif status in {'matched', 'different'}:
            if (value['reason'] is not None or source is None or basis is None
                    or any(value[key] is None for key in ('control_amount', 'ledger_amount', 'difference'))):
                return None
            difference = Decimal(value['control_amount']) - Decimal(value['ledger_amount'])
            if Decimal(value['difference']) != difference or (status == 'matched') != (difference == 0):
                return None
        else:
            return None
    except (TypeError, ValueError, ArithmeticError):
        return None
    return {**value, 'warnings': list(warnings)}
