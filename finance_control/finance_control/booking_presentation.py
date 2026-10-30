"""Read-only, currency-separated booking filters and presentation totals."""
import re
from decimal import Decimal


def booking_filters(data):
    direction = data.get('direction')
    if 'direction' in data and (
            not isinstance(direction, str) or direction not in {'income', 'expense', 'transfer'}):
        raise ValueError('invalid_direction')
    bounds = []
    for name in ('amount_min', 'amount_max'):
        value = data.get(name)
        if name in data:
            if not isinstance(value, str) or not re.fullmatch(r'[0-9]+(?:\.[0-9]{1,2})?', value):
                raise ValueError(f'invalid_{name}')
            value = Decimal(value)
        bounds.append(value)
    minimum, maximum = bounds
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ValueError('invalid_amount_range')
    return direction, minimum, maximum


def booking_direction(row):
    return ('transfer' if row['is_transfer_row'] else
            'income' if Decimal(row['amount']) > 0 else 'expense')


def filter_bookings(rows, filters):
    direction, minimum, maximum = filters
    return [row for row in rows
            if (direction is None or booking_direction(row) == direction)
            and (minimum is None or abs(Decimal(row['amount'])) >= minimum)
            and (maximum is None or abs(Decimal(row['amount'])) <= maximum)]


def booking_summary(rows):
    """Sum all filtered rows before paging; never combine currency codes."""
    currencies = {}
    for row in rows:
        currency = row['currency']
        bucket = currencies.setdefault(currency, {
            'currency': currency, 'count': 0, 'income': Decimal(0),
            'outflow': Decimal(0), 'transfer': Decimal(0), 'net': Decimal(0),
        })
        amount = Decimal(row['amount'])
        direction = row.get('direction') if isinstance(row, dict) else None
        direction = direction or booking_direction(row)
        bucket['count'] += 1
        bucket['net'] += amount
        bucket['outflow' if direction == 'expense' else direction] += (
            -amount if direction == 'expense' else amount)
    return {'total': len(rows), 'currencies': [
        {key: format(value, '.2f') if isinstance(value, Decimal) else value
         for key, value in currencies[currency].items()}
        for currency in sorted(currencies)
    ]}
