"""Dated account balances for visible ledger bookings.

The result is conditional on the imported ledger being complete between each
displayed day and the recorded balance. A booking's position within its day is
not established, so every booking on that day receives the same dated value.
"""

from collections import defaultdict
from datetime import date
from decimal import Decimal

from .core import money
from .paypal_date_basis import transaction_date_accounts


def statement_anchors(store, account_id, currency):
    """Read qualified closing controls using the current account bindings."""
    queries = (
        'SELECT p.as_of,p.closing_balance,p.currency FROM ing_period_imports p '
        'JOIN bank_source_accounts s ON s.source_key=p.source_key AND s.account_id=p.account_id '
        'WHERE p.account_id=?',
        'SELECT m.period_end,m.closing_balance,a.currency FROM bank_monthly_adoptions m '
        'JOIN bank_source_accounts s ON s.source_key=m.source_key AND s.account_id=m.account_id '
        'JOIN accounts a ON a.id=m.account_id WHERE m.account_id=?',
        'SELECT i.period_end,i.closing_balance,a.currency FROM bank_statement_imports i '
        'JOIN bank_statement_account_bindings b ON b.account_id=i.account_id '
        'JOIN accounts a ON a.id=i.account_id WHERE i.account_id=?',
        "SELECT booked_on,amount,currency FROM confirmed_balance_references "
        "WHERE account_id=? AND basis='statement_day_end'",
    )
    anchors = []
    for query in queries:
        for row in store.db.execute(query, (account_id,)):
            try:
                day, amount, recorded_currency = row
                if (type(day) is not str or date.fromisoformat(day).isoformat() != day
                        or recorded_currency != currency):
                    continue
                anchors.append({'booked_on': day, 'amount': format(money(amount), '.2f'),
                                'currency': currency, 'source': 'statement'})
            except (TypeError, ValueError):
                continue
    return anchors


_statement_anchors = statement_anchors


class LedgerBalanceUnavailable(ValueError):
    """A dated ledger cannot be compared without inventing financial facts."""


def ledger_balance_at(store, account, as_of):
    """Sum the opening anchor and active movements in the account's currency.

    The opening value is the balance at its date; valid imported movements
    start after it. This computes the recorded ledger, not bank completeness.
    """
    try:
        cutoff = date.fromisoformat(as_of)
        opening = date.fromisoformat(account['opening_date'])
        if cutoff.isoformat() != as_of or opening.isoformat() != account['opening_date']:
            raise ValueError
        if cutoff < opening:
            raise LedgerBalanceUnavailable('control_predates_opening')
        if account['id'] in transaction_date_accounts(store, [account['id']]):
            raise LedgerBalanceUnavailable('paypal_transaction_date_not_booking_date')
        balance = money(account['opening'])
        for row in store.db.execute(
                'SELECT date,amount,currency FROM active_transactions '
                'WHERE account_id=? AND date<=?', (account['id'], as_of)):
            if row['currency'] != account['currency']:
                raise LedgerBalanceUnavailable('currency_conflict')
            day = date.fromisoformat(row['date'])
            if day.isoformat() != row['date'] or day <= opening:
                raise ValueError
            balance += money(row['amount'])
        return money(balance)
    except LedgerBalanceUnavailable:
        raise
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        raise LedgerBalanceUnavailable('invalid_ledger') from exc


def _statement_conflict(store, account_id, currency, day, chosen_amount):
    """Reject disagreeing qualified controls on the selected anchor date."""
    values = {chosen_amount}
    values.update(money(control['amount'])
                  for control in _statement_anchors(store, account_id, currency)
                  if control['booked_on'] == day)
    return len(values) > 1


def attach_day_balances(store, bookings, account_rows, current_balances):
    """Add evidence-bounded dated balances without changing ledger amounts."""
    if not bookings:
        return
    account_by_id = {row['id']: row for row in account_rows}
    days_by_account = defaultdict(set)
    for booking in bookings:
        days_by_account[booking['account_id']].add(booking['date'])

    # Without the actual booking date, even overlap with a reconstruction span
    # cannot be established. Keep recorded controls, but derive no dated path.
    uncertain_accounts = transaction_date_accounts(store, list(days_by_account))
    results = {}
    owner_day_results = {}
    for account_id, days in days_by_account.items():
        account = account_by_id[account_id]
        currency = account['currency']
        anchor = current_balances.get(account_id)
        source = anchor.get('source') if isinstance(anchor, dict) else None
        basis = {'bank': 'recorded_bank_ledger',
                 'statement': 'reconciled_statement',
                 'finanzguru_file': 'recorded_file_ledger',
                 'confirmed_reference': 'reconciled_statement'}.get(source)
        shared = {'currency': currency, 'basis': basis,
                  'anchor_booked_on': anchor.get('booked_on') if isinstance(anchor, dict) else None,
                  'anchor_source': source,
                  'warnings': ['ledger_completeness_unverified']}

        def unknown(reason):
            return {day: {**shared, 'date': day, 'amount': None,
                          'warnings': [*shared['warnings'], reason]} for day in days}

        if account_id in uncertain_accounts:
            results[account_id] = unknown('paypal_transaction_date_not_booking_date')
            continue
        if account.get('reason') == 'incomplete_source_coverage':
            results[account_id] = unknown('incomplete_source_coverage')
            continue
        if source == 'confirmed_reference':
            if anchor.get('status') == 'ambiguous' or anchor.get('amount') is None:
                results[account_id] = unknown('recorded_balance_ambiguous')
                continue
            if anchor.get('basis') != 'statement_day_end':
                if anchor.get('basis') != 'owner_confirmed_day':
                    results[account_id] = unknown('owner_reference_not_day_end')
                    continue
                if store.db.execute(
                        'SELECT 1 FROM active_transactions WHERE account_id=? '
                        'AND date<=? AND currency!=? LIMIT 1',
                        (account_id, anchor['reference_booked_on'], currency)).fetchone():
                    results[account_id] = unknown('ledger_currency_conflict')
                    continue
                # latest_by_account validates captured snapshot membership.
                # This logical path is explicitly distinct from a day end.
                owner_day_results[account_id] = unknown('owner_reference_not_day_end')
                shared.update(basis='owner_confirmed_snapshot', warnings=[
                    *shared['warnings'], 'owner_snapshot_sequence_assumed'])
            # The derived current amount is not the statement anchor itself.
            anchor = {**anchor, 'booked_on': anchor['reference_booked_on'],
                      'amount': anchor['reference_amount']}
            source = ('owner_snapshot' if account_id in owner_day_results else 'statement')
            shared.update(anchor_booked_on=anchor['booked_on'])
        if source == 'bank':
            # A current bank control can precede other bookings on its date.
            # It therefore cannot anchor any reconstruction of day-end values.
            controls = _statement_anchors(store, account_id, currency)
            if not controls:
                results[account_id] = unknown('bank_balance_not_day_end')
                continue
            latest_day = max(control['booked_on'] for control in controls)
            latest = [control for control in controls if control['booked_on'] == latest_day]
            if len({control['amount'] for control in latest}) != 1:
                results[account_id] = unknown('conflicting_statement_balances_on_same_date')
                continue
            anchor = latest[0]
            source = 'statement'
            shared.update(basis='reconciled_statement', anchor_booked_on=latest_day,
                          anchor_source=source)
        if not isinstance(anchor, dict) or anchor.get('status') == 'ambiguous' or anchor.get('amount') is None:
            results[account_id] = unknown('recorded_balance_ambiguous' if isinstance(anchor, dict) and anchor.get('status') == 'ambiguous'
                                          else 'recorded_balance_missing')
            continue
        try:
            anchor_day = anchor['booked_on']
            if (type(anchor_day) is not str or date.fromisoformat(anchor_day).isoformat() != anchor_day
                    or anchor['currency'] != currency):
                results[account_id] = unknown('invalid_balance_anchor')
                continue
            anchor_amount = money(anchor['amount'])
        except (KeyError, TypeError, ValueError):
            results[account_id] = unknown('invalid_balance_anchor')
            continue
        if source == 'statement' and _statement_conflict(store, account_id, currency, anchor_day, anchor_amount):
            results[account_id] = unknown('conflicting_statement_balances_on_same_date')
            continue

        first, last = min(min(days), anchor_day), max(max(days), anchor_day)
        movements = defaultdict(lambda: Decimal('0.00'))
        currency_conflicts = set()
        anchor_day_count = 0
        for row in store.db.execute(
                'SELECT date,amount,currency FROM active_transactions '
                'WHERE account_id=? AND date BETWEEN ? AND ?', (account_id, first, last)):
            if row['date'] == anchor_day:
                anchor_day_count += 1
            if row['currency'] != currency:
                currency_conflicts.add(row['date'])
            else:
                movements[row['date']] += money(row['amount'])

        # Without a complete accepted-snapshot trail, a file balance belongs
        # to one transaction and its position among same-day postings is unknown.
        if (source == 'finanzguru_file' and anchor_day_count != 1
                and anchor.get('day_end_proven') is not True):
            results[account_id] = unknown('file_balance_not_day_end')
            continue
        results[account_id] = {}
        for day in days:
            span = [movement_day for movement_day in movements
                    if min(day, anchor_day) < movement_day <= max(day, anchor_day)]
            conflict = any(conflict_day == day
                           or min(day, anchor_day) < conflict_day <= max(day, anchor_day)
                           for conflict_day in currency_conflicts)
            if conflict:
                results[account_id][day] = {**shared, 'date': day, 'amount': None,
                                            'warnings': [*shared['warnings'], 'ledger_currency_conflict']}
                continue
            delta = sum((movements[movement_day] for movement_day in span), Decimal('0.00'))
            amount = anchor_amount + delta if day >= anchor_day else anchor_amount - delta
            results[account_id][day] = {**shared, 'date': day,
                                        'amount': format(amount, '.2f'),
                                        'warnings': list(shared['warnings'])}

    for booking in bookings:
        account_id = booking['account_id']
        booking['account_day_balance'] = owner_day_results.get(
            account_id, results[account_id])[booking['date']]
    _attach_movement_balances(store, bookings, {
        account_id: results[account_id] for account_id in owner_day_results})


def _attach_movement_balances(store, bookings, movement_day_balances=None):
    """Calculate a logical per-movement path, never assert bank intraday order.

    Use the full ledger, irrespective of presentation filters. The recorded
    day end is retained; only the explicitly assumed same-day sequence changes
    intermediate values. Unknown or conflicting day controls stay unknown.
    """
    days = defaultdict(set)
    for booking in bookings:
        days[booking['account_id']].add(booking['date'])
    paths = {}
    for account_id, selected_days in days.items():
        rows_by_day = defaultdict(list)
        for row in store.db.execute(
                'SELECT date,external_id,amount,currency FROM active_transactions '
                'WHERE account_id=? AND date BETWEEN ? AND ? '
                'ORDER BY date,external_id',
                (account_id, min(selected_days), max(selected_days))):
            if row['date'] in selected_days:
                rows_by_day[row['date']].append(row)
        for day, rows in rows_by_day.items():
            total = sum((money(row['amount']) for row in rows), Decimal('0.00'))
            prefix = Decimal('0.00')
            for index, row in enumerate(rows, 1):
                prefix += money(row['amount'])
                paths[(account_id, row['external_id'])] = (prefix-total, index, len(rows))
    for booking in bookings:
        day_balance = (movement_day_balances or {}).get(
            booking['account_id'], {}).get(booking['date'], booking['account_day_balance'])
        offset, index, count = paths.get((booking['account_id'], booking['external_id']), (None, None, 0))
        warnings = list(day_balance['warnings'])
        if count > 1:
            warnings.append('same_day_order_assumed')
        value = None
        if (offset is not None and day_balance['amount'] is not None
                and day_balance['currency'] == booking['currency']):
            value = format(money(day_balance['amount']) + offset, '.2f')
        booking['account_movement_balance'] = {
            **day_balance, 'amount': value, 'warnings': warnings,
            'sequence_basis': 'external_id_order', 'sequence_index': index,
            'sequence_count': count,
        }
