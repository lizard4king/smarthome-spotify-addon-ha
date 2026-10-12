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


def latest_by_account(store, account_rows, *, as_of=None):
    """Use recorded controls only, optionally bounded by an inclusive ISO date.

    Without ``as_of`` the latest recorded control is returned regardless of the
    reporting month. The bound applies before selecting the latest candidate,
    so an older eligible statement remains available behind a future balance.
    """
    if as_of is not None:
        try:
            if type(as_of) is not str or date.fromisoformat(as_of).isoformat() != as_of:
                raise ValueError('invalid_balance_as_of')
        except (TypeError, ValueError) as exc:
            raise ValueError('invalid_balance_as_of') from exc
    wanted = {row['id']: row['currency'] for row in account_rows}
    if not wanted:
        return {}
    placeholders = ','.join('?' for _ in wanted)
    parameters = (*wanted, as_of) if as_of is not None else tuple(wanted)
    candidates = []

    # Explicit bank balances are accepted only while their original binding
    # still points at the same account.
    for row in store.db.execute(
            'SELECT b.account_id,b.amount,b.currency,b.booked_on,b.retrieved_at '
            'FROM bank_account_balances b JOIN bank_source_accounts s '
            'ON s.source_key=b.source_key AND s.account_id=b.account_id '
            f'WHERE b.account_id IN ({placeholders})'
            + (' AND b.booked_on<=?' if as_of is not None else ''), parameters):
        candidates.append(_candidate(row['account_id'], row['currency'], row['amount'],
                                     row['booked_on'], row['retrieved_at'], 'bank'))
    for row in store.db.execute(
            'SELECT p.account_id,p.closing_balance,p.currency,p.as_of '
            'FROM ing_period_imports p JOIN bank_source_accounts s '
            'ON s.source_key=p.source_key AND s.account_id=p.account_id '
            f'WHERE p.account_id IN ({placeholders})'
            + (' AND p.as_of<=?' if as_of is not None else ''), parameters):
        candidates.append(_candidate(row['account_id'], row['currency'],
                                     row['closing_balance'], row['as_of'], None, 'statement'))
    for row in store.db.execute(
            'SELECT m.account_id,m.closing_balance,a.currency,m.period_end '
            'FROM bank_monthly_adoptions m JOIN bank_source_accounts s '
            'ON s.source_key=m.source_key AND s.account_id=m.account_id '
            'JOIN accounts a ON a.id=m.account_id '
            f'WHERE m.account_id IN ({placeholders})'
            + (' AND m.period_end<=?' if as_of is not None else ''), parameters):
        candidates.append(_candidate(row['account_id'], row['currency'],
                                     row['closing_balance'], row['period_end'], None, 'statement'))
    for row in store.db.execute(
            'SELECT i.account_id,i.closing_balance,a.currency,i.period_end '
            'FROM bank_statement_imports i JOIN bank_statement_account_bindings b '
            'ON b.account_id=i.account_id JOIN accounts a ON a.id=i.account_id '
            f'WHERE i.account_id IN ({placeholders})'
            + (' AND i.period_end<=?' if as_of is not None else ''), parameters):
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

    # File evidence is a fallback only. Any qualified bank or bound statement
    # candidate above retains the existing source/date priority.
    file_only = set(wanted) - set(result)
    if file_only:
        ids = sorted(file_only)
        placeholders = ','.join('?' for _ in ids)
        file_parameters = (*ids, as_of) if as_of is not None else tuple(ids)
        file_rows = store.db.execute(
            'SELECT m.account_id,m.balance_amount,m.currency,m.booked_on '
            'FROM finanzguru_row_metadata m JOIN transactions t '
            'ON t.account_id=m.account_id AND t.external_id=m.external_id '
            'AND t.date=m.booked_on AND t.currency=m.currency '
            'WHERE m.balance_amount IS NOT NULL '
            f'AND m.account_id IN ({placeholders})'
            + (' AND m.booked_on<=?' if as_of is not None else ''), file_parameters)
        by_account = {}
        for row in file_rows:
            if row['currency'] != wanted[row['account_id']]:
                continue
            candidate = _candidate(row['account_id'], row['currency'], row['balance_amount'],
                                   row['booked_on'], None, 'finanzguru_file')
            if candidate is not None:
                by_account.setdefault(candidate[0], []).append(candidate[1])
        for account_id, values in by_account.items():
            latest_date = max(value['booked_on'] for value in values)
            latest = [value for value in values if value['booked_on'] == latest_date]
            amounts = {value['amount'] for value in latest}
            if len(amounts) > 1:
                result[account_id] = {
                    'amount': None, 'currency': wanted[account_id], 'booked_on': latest_date,
                    'retrieved_at': None, 'source': 'finanzguru_file', 'status': 'ambiguous',
                    'warning': 'conflicting_finanzguru_balances_on_same_date',
                }
            else:
                result[account_id] = latest[0]
    return result
