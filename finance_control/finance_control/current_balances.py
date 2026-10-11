"""Latest recorded bank or reconciled statement balance per account."""

from datetime import date
from decimal import Decimal

from .core import money


def summarize(account_rows, balances):
    """Sum only recorded current balances, keeping unknown accounts explicit.

    ``balances`` maps account ids to the current_balance objects returned by
    :func:`latest_by_account`. Amounts are accumulated as Decimal and never
    converted between currencies.
    """
    totals = {}
    for account in account_rows:
        account_id = account['id']
        currency = account['currency']
        if type(currency) is not str or not currency:
            continue
        group = totals.setdefault(currency, {
            'currency': currency, 'amount': Decimal('0.00'),
            'known_count': 0, 'account_count': 0,
            'oldest_booked_on': None, 'newest_booked_on': None,
        })
        group['account_count'] += 1
        balance = balances.get(account_id)
        if not isinstance(balance, dict) or balance.get('currency') != currency:
            continue
        try:
            amount = money(balance['amount'])
            booked_on = balance['booked_on']
            if (type(booked_on) is not str
                    or date.fromisoformat(booked_on).isoformat() != booked_on):
                continue
        except (KeyError, TypeError, ValueError):
            continue
        group['amount'] += amount
        group['known_count'] += 1
        if group['oldest_booked_on'] is None or booked_on < group['oldest_booked_on']:
            group['oldest_booked_on'] = booked_on
        if group['newest_booked_on'] is None or booked_on > group['newest_booked_on']:
            group['newest_booked_on'] = booked_on
    return [{**group, 'amount': format(group['amount'], '.2f')}
            for _, group in sorted(totals.items())]


def _candidate(account_id, currency, amount, booked_on, retrieved_at, source):
    try:
        if (type(booked_on) is not str or date.fromisoformat(booked_on).isoformat() != booked_on
                or type(currency) is not str or not currency):
            return None
        amount = format(money(amount), '.2f')
    except (TypeError, ValueError):
        return None
    return (account_id, {'amount': amount, 'currency': currency,
                         'booked_on': booked_on, 'retrieved_at': retrieved_at,
                         'source': source})


def latest_by_account(store, account_rows):
    """Use recorded controls only; the selected reporting month is irrelevant."""
    wanted = {row['id']: row['currency'] for row in account_rows}
    if not wanted:
        return {}
    placeholders = ','.join('?' for _ in wanted)
    candidates = []

    # Explicit bank balances are accepted only while their original binding
    # still points at the same account.
    for row in store.db.execute(
            'SELECT b.account_id,b.amount,b.currency,b.booked_on,b.retrieved_at '
            'FROM bank_account_balances b JOIN bank_source_accounts s '
            'ON s.source_key=b.source_key AND s.account_id=b.account_id '
            f'WHERE b.account_id IN ({placeholders})', tuple(wanted)):
        candidates.append(_candidate(row['account_id'], row['currency'], row['amount'],
                                     row['booked_on'], row['retrieved_at'], 'bank'))
    for row in store.db.execute(
            'SELECT p.account_id,p.closing_balance,p.currency,p.as_of '
            'FROM ing_period_imports p JOIN bank_source_accounts s '
            'ON s.source_key=p.source_key AND s.account_id=p.account_id '
            f'WHERE p.account_id IN ({placeholders})', tuple(wanted)):
        candidates.append(_candidate(row['account_id'], row['currency'],
                                     row['closing_balance'], row['as_of'], None, 'statement'))
    for row in store.db.execute(
            'SELECT m.account_id,m.closing_balance,a.currency,m.period_end '
            'FROM bank_monthly_adoptions m JOIN bank_source_accounts s '
            'ON s.source_key=m.source_key AND s.account_id=m.account_id '
            'JOIN accounts a ON a.id=m.account_id '
            f'WHERE m.account_id IN ({placeholders})', tuple(wanted)):
        candidates.append(_candidate(row['account_id'], row['currency'],
                                     row['closing_balance'], row['period_end'], None, 'statement'))
    for row in store.db.execute(
            'SELECT i.account_id,i.closing_balance,a.currency,i.period_end '
            'FROM bank_statement_imports i JOIN bank_statement_account_bindings b '
            'ON b.account_id=i.account_id JOIN accounts a ON a.id=i.account_id '
            f'WHERE i.account_id IN ({placeholders})', tuple(wanted)):
        candidates.append(_candidate(row['account_id'], row['currency'],
                                     row['closing_balance'], row['period_end'], None, 'statement'))

    result = {}
    for candidate in candidates:
        if candidate is None:
            continue
        account_id, balance = candidate
        if balance['currency'] != wanted[account_id]:
            continue
        previous = result.get(account_id)
        if (previous is None or (balance['booked_on'], balance['source'] == 'bank')
                > (previous['booked_on'], previous['source'] == 'bank')):
            result[account_id] = balance
    return result
