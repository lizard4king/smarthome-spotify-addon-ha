"""Small, explicitly allowlisted data view for the local read-only API."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from . import budget, monthly_review, overview, plan_actual
from .core import Store, money


def _amount(value):
    return format(money(value), '.2f')


def _period_end(period):
    start, end, _ = monthly_review._period_bounds(period)
    return start, end


class FinanceReadApi:
    """Expose only reviewed aggregates and named ledger balances.

    Every request opens a fresh SQLite read-only connection. Methods return
    allowlisted JSON primitives; private domain objects never cross the boundary.
    """

    def __init__(self, database: str | Path, *, today: date | None = None,
                 stale_after_days: int = 7):
        self.database = Path(database).expanduser().resolve()
        if today is not None and not isinstance(today, date):
            raise ValueError('today must be a date')
        if type(stale_after_days) is not int or stale_after_days < 1:
            raise ValueError('stale_after_days must be positive')
        self._today_override = today
        self.stale_after_days = stale_after_days

    @property
    def today(self):
        """Use a fixed date in tests and the live local date in long-running servers."""
        return self._today_override or date.today()

    def accounts(self, as_of: str):
        cutoff = date.fromisoformat(as_of).isoformat()
        if cutoff > self.today.isoformat():
            raise ValueError('as_of must not be in the future')
        store = Store(self.database, readonly=True)
        try:
            account_rows = store.db.execute(
                "SELECT id,display_name,kind,currency,opening,opening_date FROM accounts "
                "WHERE NULLIF(TRIM(display_name),'') IS NOT NULL ORDER BY display_name,id"
            ).fetchall()
            if not account_rows:
                return {'as_of': cutoff, 'currency': 'EUR', 'status': 'unavailable',
                        'accounts': []}
            accounts = []
            for row in account_rows:
                if row['opening_date'] > cutoff:
                    balance = None
                    balance_status = 'unavailable'
                else:
                    transactions = store.db.execute(
                        'SELECT amount FROM transactions WHERE account_id=? AND date<=?',
                        (row['id'], cutoff))
                    balance = _amount(money(row['opening']) + sum(
                        (money(tx['amount']) for tx in transactions), Decimal('0.00')))
                    balance_status = 'booked'
                entry = {'display_name': row['display_name'].strip(),
                         'kind': row['kind'], 'currency': row['currency'],
                         'balance': balance, 'balance_as_of': cutoff,
                         'balance_status': balance_status}
                if row['kind'] == 'CREDIT_CARD':
                    entry['available_credit'] = None
                    entry['available_credit_status'] = 'unavailable'
                accounts.append(entry)
            return {'as_of': cutoff, 'currency': 'EUR',
                    'status': ('booked' if any(row['balance'] is not None
                                             for row in accounts) else 'unavailable'),
                    'accounts': accounts}
        finally:
            store.close()

    def monthly_status(self, period: str):
        _, end = _period_end(period)
        base = {'period': period, 'period_end': end.isoformat(), 'currency': 'EUR',
                'income': None, 'expenses': None, 'available_budget': None,
                'forecast': None, 'status': 'unavailable',
                'available_budget_status': 'unavailable',
                'forecast_status': 'unavailable'}
        if period > self.today.strftime('%Y-%m'):
            return base
        store = Store(self.database, readonly=True)
        try:
            if not store.db.execute('SELECT 1 FROM accounts LIMIT 1').fetchone():
                return base
            try:
                snapshot = monthly_review.preview(store, period)
            except ValueError:
                return base
            saved = store.db.execute(
                'SELECT source_digest,payload FROM monthly_reviews '
                'WHERE period=? ORDER BY revision DESC LIMIT 1', (period,)).fetchone()
            reviewed = saved is not None and monthly_review.source_matches(saved, snapshot)
            result = dict(base)
            result.update({'income': snapshot['liquidity']['income'],
                           'expenses': snapshot['liquidity']['expenses'],
                           'status': ('booked_partial' if period == self.today.strftime('%Y-%m')
                                      else 'reviewed' if reviewed else 'booked_unreviewed')})
            active = budget.load(store)
            if active['plan'] is not None and active['calculation_status'] == 'saved':
                try:
                    comparison = plan_actual.compare_actual(
                        store, {'revision': active['revision'], 'month': period})
                except ValueError:
                    pass
                else:
                    if (comparison['basis']['type'] == 'saved_budget'
                            and comparison['unclassified']['count'] == 0):
                        result['available_budget'] = _amount(
                            comparison['totals']['remaining_expense_budget'])
                        result['available_budget_status'] = 'calculated'
                # The existing overview only confirms an absolute projection
                # when the plan joins a completed month-end ledger balance.
                if period == active['calculation']['rows'][-1]['period']:
                    start_month = active['plan']['start_month']
                    start, _ = _period_end(start_month)
                    prior_end = date.fromordinal(start.toordinal() - 1)
                    try:
                        prior_status = store.status(prior_end.isoformat())
                        plan_view, _ = overview._plan_overview(store, prior_end, prior_status)
                    except ValueError:
                        pass
                    else:
                        if plan_view is not None and plan_view['connected']:
                            result['forecast'] = {
                                'period': plan_view['end_period'],
                                'expected_balance': _amount(plan_view['expected_end'])}
                            result['forecast_status'] = 'calculated'
            return result
        finally:
            store.close()

    def amazon_summary(self):
        store = Store(self.database, readonly=True)
        try:
            rows = store.db.execute(
                'SELECT d.amount,d.currency,EXISTS('
                'SELECT 1 FROM classification_document_links l '
                'WHERE l.document_id=d.id) AS matched '
                'FROM amazon_order_documents a '
                'JOIN classification_documents d ON d.id=a.document_id'
            ).fetchall()
            unmatched = [row for row in rows if not row['matched']]
            complete = bool(rows) and all(
                row['currency'] == 'EUR' and row['amount'] is not None
                for row in unmatched)
            aggregate = _amount(sum((money(row['amount']) for row in unmatched),
                                    money('0.00'))) if complete else None
            return {'status': 'available' if rows else 'unavailable',
                    'currency': 'EUR', 'order_count': len(rows),
                    'matched_count': len(rows) - len(unmatched),
                    'unmatched_count': len(unmatched),
                    'unmatched_amount': aggregate,
                    'unmatched_amount_status': 'calculated' if complete else 'unavailable'}
        finally:
            store.close()

    def source_status(self):
        store = Store(self.database, readonly=True)
        try:
            imported = store.db.execute('SELECT MAX(imported_at) FROM imports').fetchone()[0]
            finanzguru = store.db.execute(
                'SELECT MAX(i.imported_at) FROM finanzguru_snapshot_heads h '
                'JOIN imports i ON i.id=h.import_id').fetchone()[0]
            def item(name, timestamp):
                if timestamp is None:
                    return {'source': name, 'last_success_at': None,
                            'status': 'unavailable'}
                try:
                    age = (self.today - date.fromisoformat(timestamp[:10])).days
                except ValueError:
                    return {'source': name, 'last_success_at': None,
                            'status': 'unavailable'}
                return {'source': name, 'last_success_at': timestamp,
                        'status': ('unknown' if age < 0 else
                                   'stale' if age > self.stale_after_days else 'ok')}
            return {'as_of': self.today.isoformat(), 'sources': [
                item('ledger_import', imported), item('finanzguru', finanzguru),
                item('amazon', None), item('credit_card', None)]}
        finally:
            store.close()
