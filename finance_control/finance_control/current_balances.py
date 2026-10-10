"""Latest recorded bank or reconciled statement balance per account."""

from datetime import date

from .core import money


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
