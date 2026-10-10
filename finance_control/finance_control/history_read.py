"""Confidential, transient owner-only history summaries; never import authority."""

from calendar import monthrange
from datetime import date
from decimal import Decimal, localcontext
import re

from .core import money

MAX_ROWS = 10_000
MAX_VISIBLE_ROWS = 50
MAX_TEXT = 2048
_FIELDS = {'masked_account', 'month_start', 'as_of', 'count', 'totals', 'rows'}
_ROW_FIELDS = {'booked_on', 'value_on', 'amount', 'currency', 'counterparty', 'purpose', 'booking_text'}


def iso_date(value):
    if type(value) is not str or len(value) != 10:
        raise ValueError('history date')
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError('history date')
    return parsed


def history_period(start, end, today=None):
    today = today or date.today()
    return (start.day == 1 and end == date(start.year, start.month, monthrange(start.year, start.month)[1])
            and end < today.replace(day=1) and 0 <= (today - start).days <= 366)


def cash(value):
    amount = money(value)
    return format(amount if amount else Decimal(0), '.2f')


def _amount(value):
    if (type(value) is not str or len(value) > 16 or value == '-0.00'
            or re.fullmatch(r'-?(?:0|[1-9][0-9]{0,11})\.[0-9]{2}', value) is None):
        raise ValueError('history amount')
    return money(value)


def _currency(value):
    if type(value) is not str or re.fullmatch(r'[A-Z]{3}', value) is None:
        raise ValueError('history currency')
    return value


def text(value):
    if (type(value) is not str or len(value) > MAX_TEXT
            or any((ord(c) < 32 and c not in '\n\t') or ord(c) == 127
                   or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        raise ValueError('history text')
    return value


def _shape(value, fields):
    if type(value) is not dict or any(type(k) is not str for k in value) or set(value) != fields:
        raise ValueError('history shape')


def validated_history(value, start, end):
    """Revalidate the exact owner-only DTO; a result never asserts completeness."""
    _shape(value, _FIELDS)
    if (not history_period(start, end) or iso_date(value['month_start']) != start
            or iso_date(value['as_of']) != end or type(value['masked_account']) is not str
            or re.fullmatch(r'••••(?:[A-Za-z0-9]{4})?', value['masked_account']) is None
            or type(value['count']) is not int or not 0 <= value['count'] <= MAX_ROWS
            or type(value['rows']) is not list or len(value['rows']) != min(value['count'], MAX_VISIBLE_ROWS)
            or type(value['totals']) is not list or len(value['totals']) > 32):
        raise ValueError('history')
    totals = {}
    clean_totals = []
    with localcontext() as context:
        context.prec = 40
        for item in value['totals']:
            _shape(item, {'currency', 'credits', 'debits', 'net'})
            currency = _currency(item['currency'])
            credits, debits, net = (_amount(item[k]) for k in ('credits', 'debits', 'net'))
            if currency in totals or credits < 0 or debits < 0 or credits - debits != net:
                raise ValueError('history totals')
            totals[currency] = (credits, debits)
            clean_totals.append(item.copy())
        rows = []
        visible_totals = {}
        for row in value['rows']:
            _shape(row, _ROW_FIELDS)
            if not start <= iso_date(row['booked_on']) <= end:
                raise ValueError('history row date')
            if row['value_on'] is not None:
                iso_date(row['value_on'])
            amount, currency = _amount(row['amount']), _currency(row['currency'])
            if currency not in totals:
                raise ValueError('history row currency')
            for key in ('counterparty', 'purpose', 'booking_text'):
                text(row[key])
            pair = visible_totals.setdefault(currency, [Decimal(0), Decimal(0)])
            pair[0 if amount >= 0 else 1] += abs(amount)
            rows.append(row.copy())
        if value['count'] == 0 and totals:
            raise ValueError('empty history totals')
        if value['count'] and not totals:
            raise ValueError('missing history totals')
        for currency, pair in visible_totals.items():
            if any(pair[i] > totals[currency][i] for i in (0, 1)):
                raise ValueError('history visible totals')
        if value['count'] <= MAX_VISIBLE_ROWS and (
                set(visible_totals) != set(totals)
                or any(tuple(pair) != totals[c] for c, pair in visible_totals.items())):
            raise ValueError('history complete totals')
    return dict(value, totals=clean_totals, rows=rows)


def summarize_history(bookings, masked_account, start, end):
    """Aggregate every booked row with Decimal; expose at most fifty text rows."""
    from .connectors.fints_readonly import Booking

    if type(bookings) not in (list, tuple) or len(bookings) > MAX_ROWS:
        raise ValueError('history rows')
    totals = {}
    rows = []
    with localcontext() as context:
        context.prec = 40
        for booking in bookings:
            if (type(booking) is not Booking or type(booking.entry_on) is not date
                    or not start <= booking.entry_on <= end or type(booking.amount) is not Decimal
                    or (booking.value_on is not None and type(booking.value_on) is not date)):
                raise ValueError('history booking')
            amount, currency = money(booking.amount), _currency(booking.currency)
            pair = totals.setdefault(currency, [Decimal(0), Decimal(0)])
            if len(totals) > 32:
                raise ValueError('history currencies')
            pair[0 if amount >= 0 else 1] += abs(amount)
            # Validate even the rows outside the visible excerpt.
            for item in (booking.counterparty, booking.purpose, booking.booking_text):
                text(item)
            if len(rows) < MAX_VISIBLE_ROWS:
                rows.append({'booked_on': booking.entry_on.isoformat(),
                             'value_on': booking.value_on.isoformat() if booking.value_on else None,
                             'amount': cash(amount), 'currency': currency,
                             'counterparty': booking.counterparty, 'purpose': booking.purpose,
                             'booking_text': booking.booking_text})
        result = {'masked_account': masked_account, 'month_start': start.isoformat(),
                  'as_of': end.isoformat(), 'count': len(bookings), 'rows': rows,
                  'totals': [{'currency': c, 'credits': cash(p[0]), 'debits': cash(p[1]),
                              'net': cash(p[0] - p[1])} for c, p in sorted(totals.items())]}
    return validated_history(result, start, end)
