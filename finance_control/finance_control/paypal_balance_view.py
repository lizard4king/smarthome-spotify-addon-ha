"""Validate and minimize Enable Banking PayPal balance responses."""

import json
import re
from datetime import date

from .core import money


_TYPES = frozenset({'CLBD', 'ITBD', 'CLAV', 'ITAV', 'XPCD'})
_MAX_BALANCES = 100
_MAX_RESPONSE_BYTES = 128_000


def sanitize_balances(response, currency, retrieved_at):
    """Keep supported provider balances separate, without inventing dates or totals.

    XPCD is the provider's instant balance, never a booked closing balance.
    Account currency XXX permits explicit balance currencies without conversion.
    """
    if type(response) is not dict:
        raise ValueError('invalid balance response')
    try:
        if len(json.dumps(response, ensure_ascii=False, allow_nan=False).encode('utf-8')) > _MAX_RESPONSE_BYTES:
            raise ValueError('balance response too large')
    except (TypeError, OverflowError) as exc:
        raise ValueError('invalid balance response') from exc
    entries = response.get('balances')
    if type(entries) is not list or len(entries) > _MAX_BALANCES:
        raise ValueError('invalid balances')
    result = []
    seen = set()
    for entry in entries:
        if type(entry) is not dict or type(entry.get('balance_type')) is not str:
            raise ValueError('invalid balance')
        balance_type = entry['balance_type']
        if balance_type not in _TYPES:
            continue
        amount = entry.get('balance_amount')
        label = entry.get('name')
        reference_date = entry.get('reference_date')
        if (type(amount) is not dict or type(amount.get('amount')) is not str
                or type(amount.get('currency')) is not str
                or re.fullmatch(r'[A-Z]{3}', amount['currency']) is None
                or amount['currency'] == 'XXX'
                or (currency != 'XXX' and amount['currency'] != currency)
                or type(label) is not str
                or not label.strip() or len(label) > 160
                or any(ord(char) < 32 for char in label)):
            raise ValueError('invalid balance')
        if (reference_date is not None
                and (type(reference_date) is not str
                     or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', reference_date))):
            raise ValueError('invalid balance date')
        if reference_date is not None:
            try:
                if date.fromisoformat(reference_date).isoformat() != reference_date:
                    raise ValueError
            except ValueError as exc:
                raise ValueError('invalid balance date') from exc
        key = (amount['currency'], balance_type, reference_date)
        if key in seen:
            raise ValueError('duplicate balance currency, type and date')
        seen.add(key)
        # money rejects floats, nonfinite, excessive precision and magnitude.
        result.append({'amount': format(money(amount['amount']), '.2f'),
                       'currency': amount['currency'], 'balance_type': balance_type,
                       'label': label.strip(), 'reference_date': reference_date,
                       'retrieved_at': retrieved_at})
    return result
