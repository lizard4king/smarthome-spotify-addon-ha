"""Explicit balance anchors and conservative derivation from booked movements."""
from datetime import UTC, date, datetime

from .core import money


def _date(value):
    try:
        if type(value) is not str or date.fromisoformat(value).isoformat() != value:
            raise ValueError
        return value
    except (TypeError, ValueError) as exc:
        raise ValueError('invalid_balance_date') from exc


def _text(value, name, limit=500):
    if (type(value) is not str or not value.strip() or value != value.strip()
            or len(value) > limit or any(ord(c) < 32 for c in value)):
        raise ValueError('invalid_' + name)
    return value


def _ledger(store, account_id, through):
    return [tuple(row) for row in store.db.execute(
        'SELECT external_id,date,amount,currency FROM active_transactions '
        'WHERE account_id=? AND date<=? ORDER BY external_id', (account_id, through))]


def save(store, *, account_id, amount, currency, booked_on, source_reference,
         basis, confirmed, actor='local-owner'):
    """Append an owner-confirmed anchor and its then-visible ledger provenance."""
    _text(account_id, 'account_id', 120)
    _text(source_reference, 'source_reference')
    _text(actor, 'actor', 120)
    _date(booked_on)
    if booked_on > date.today().isoformat():
        raise ValueError('future_balance_date')
    if confirmed is not True:
        raise ValueError('confirmation_required')
    if basis not in ('statement_day_end', 'owner_confirmed_day'):
        raise ValueError('invalid_balance_basis')
    value = format(money(amount), '.2f')
    with store.db:
        account = store.db.execute('SELECT currency FROM accounts WHERE id=?', (account_id,)).fetchone()
        if account is None:
            raise ValueError('unknown_account')
        if type(currency) is not str or currency != account['currency']:
            raise ValueError('invalid_currency')
        ledger = _ledger(store, account_id, booked_on)
        cursor = store.db.execute(
            'INSERT INTO confirmed_balance_references '
            '(account_id,amount,currency,booked_on,source_reference,basis,actor,confirmed_at) '
            'VALUES (?,?,?,?,?,?,?,?)',
            (account_id, value, currency, booked_on, source_reference, basis, actor,
             datetime.now(UTC).isoformat()))
        reference_id = cursor.lastrowid
        store.db.executemany(
            'INSERT INTO confirmed_balance_reference_ledger '
            '(reference_id,external_id,date,amount,currency) VALUES (?,?,?,?,?)',
            [(reference_id, *row) for row in ledger])
    return dict(store.db.execute('SELECT * FROM confirmed_balance_references WHERE id=?',
                                 (reference_id,)).fetchone())


def latest(store, account_rows, as_of=None):
    """Derive only after an anchor; uncertain snapshot membership stays unknown."""
    until = _date(as_of) if as_of is not None else date.today().isoformat()
    result = {}
    for account in account_rows:
        ref = store.db.execute(
            'SELECT * FROM confirmed_balance_references WHERE account_id=? AND booked_on<=? '
            'ORDER BY booked_on DESC,id DESC LIMIT 1', (account['id'], until)).fetchone()
        if ref is None:
            continue
        anchor = ref['booked_on']
        balance = {'amount': None, 'currency': ref['currency'], 'booked_on': anchor,
                   'source': 'confirmed_reference', 'reference_booked_on': anchor,
                   'reference_amount': ref['amount'],
                   'control_reference': f"confirmed_reference:{ref['id']}",
                   'warning': 'ledger_completeness_unverified', 'basis': ref['basis'],
                   'source_reference': ref['source_reference'], 'confirmed_at': ref['confirmed_at']}
        ambiguous = account['currency'] != ref['currency']
        if ref['basis'] == 'owner_confirmed_day':
            captured = [tuple(row) for row in store.db.execute(
                'SELECT external_id,date,amount,currency FROM confirmed_balance_reference_ledger '
                'WHERE reference_id=? ORDER BY external_id', (ref['id'],))]
            ambiguous |= captured != _ledger(store, account['id'], anchor)
        movements = store.db.execute(
            'SELECT date,amount,currency FROM active_transactions '
            'WHERE account_id=? AND date>? AND date<=? ORDER BY date,external_id',
            (account['id'], anchor, until)).fetchall()
        try:
            total = money(ref['amount'])
            for row in movements:
                if row['currency'] != ref['currency']:
                    ambiguous = True
                total += money(row['amount'])
                balance['booked_on'] = row['date']
            if not ambiguous:
                balance['amount'] = format(total, '.2f')
        except (ValueError, ArithmeticError):
            ambiguous = True
        if ambiguous:
            balance['status'] = 'ambiguous'
        result[account['id']] = balance
    return result
