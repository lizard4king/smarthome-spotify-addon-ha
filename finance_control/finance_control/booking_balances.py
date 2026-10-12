"""Dated account balances for visible ledger bookings.

The result is conditional on the imported ledger being complete between each
displayed day and the recorded balance. A booking's position within its day is
not established, so every booking on that day receives the same dated value.
"""

from collections import defaultdict
from datetime import date
from decimal import Decimal

from .core import money


def _statement_anchors(store, account_id, currency):
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

    results = {}
    for account_id, days in days_by_account.items():
        account = account_by_id[account_id]
        currency = account['currency']
        anchor = current_balances.get(account_id)
        source = anchor.get('source') if isinstance(anchor, dict) else None
        basis = {'bank': 'recorded_bank_ledger',
                 'statement': 'reconciled_statement',
                 'finanzguru_file': 'recorded_file_ledger'}.get(source)
        shared = {'currency': currency, 'basis': basis,
                  'anchor_booked_on': anchor.get('booked_on') if isinstance(anchor, dict) else None,
                  'anchor_source': source,
                  'warnings': ['ledger_completeness_unverified']}

        def unknown(reason):
            return {day: {**shared, 'date': day, 'amount': None,
                          'warnings': [*shared['warnings'], reason]} for day in days}

        if account.get('reason') == 'incomplete_source_coverage':
            results[account_id] = unknown('incomplete_source_coverage')
            continue
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
                'SELECT date,amount,currency FROM transactions '
                'WHERE account_id=? AND date BETWEEN ? AND ?', (account_id, first, last)):
            if row['date'] == anchor_day:
                anchor_day_count += 1
            if row['currency'] != currency:
                currency_conflicts.add(row['date'])
            else:
                movements[row['date']] += money(row['amount'])

        # A file balance belongs to one transaction. More transactions on that
        # date make its position relative to the other postings unknowable.
        if source == 'finanzguru_file' and anchor_day_count != 1:
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
        booking['account_day_balance'] = results[booking['account_id']][booking['date']]
