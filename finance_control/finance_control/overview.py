"""Read-only cockpit overview derived from stored reviews and budget snapshots."""

import calendar
from datetime import date, datetime, timedelta

from . import approval_queue, budget
from .core import money


def _month_end(value):
    return value.day == calendar.monthrange(value.year, value.month)[1]


def _next_month(period):
    year, month = map(int, period.split('-'))
    return f'{year + (month == 12):04d}-{1 if month == 12 else month + 1:02d}'


def _last_complete_month(today=None):
    today = datetime.now().astimezone().date() if today is None else today
    return today.replace(day=1) - timedelta(days=1)


def _text(value):
    return format(money(value), '.2f')


def _task(kind, label, count, target, detail):
    return {'kind': kind, 'label': label, 'count': count, 'target': target, 'detail': detail}


def _classification_task(store, cutoff):
    row = store.db.execute(
        "SELECT COUNT(*) FROM transactions t "
        "LEFT JOIN classification_overrides o ON o.account_id=t.account_id AND o.external_id=t.external_id "
        "LEFT JOIN transfer_correction_members m ON m.account_id=t.account_id AND m.external_id=t.external_id "
        "WHERE t.date <= ? AND t.transfer_id='' AND m.pair_id IS NULL AND COALESCE(o.confirmed, 0)=0",
        (cutoff.isoformat(),)).fetchone()
    count = row[0]
    if not count:
        return None
    return _task('unconfirmed_categories', 'Buchungskategorien bestätigen', count,
                 'classification', f'{count} Buchungskategorien bis {cutoff.isoformat()} sind unbestätigt.')


def _document_task(store):
    count = len(approval_queue.document_link_items(store))
    if not count:
        return None
    return _task('unreviewed_documents', 'Belegzuordnungen prüfen', count, 'documents',
                 f'{count} Buchungen haben einen passenden Belegvorschlag.')


def _estimated_total(calculation, kind):
    """Read one persisted estimate without reconstructing missing legacy data."""
    totals = calculation.get('totals')
    estimated = totals.get('estimated') if isinstance(totals, dict) else None
    value = estimated.get(kind) if isinstance(estimated, dict) else None
    return _text(value) if value is not None else None


def _with_compatible_breakdown(store, plan, stored):
    """Derive estimate totals only when the stored core result matches exactly."""
    if _estimated_total(stored, 'income') is not None:
        return stored
    current = budget.calculate(store, {'plan': plan})
    row_keys = ('period', 'income', 'fixed', 'variable', 'cashflow', 'cumulative')
    total_keys = ('income', 'expenses', 'cashflow')
    stored_signature = (
        [{key: row.get(key) for key in row_keys} for row in stored.get('rows', [])],
        {key: stored.get('totals', {}).get(key) for key in total_keys},
    )
    current_signature = (
        [{key: row.get(key) for key in row_keys} for row in current.get('rows', [])],
        {key: current.get('totals', {}).get(key) for key in total_keys},
    )
    return current if stored_signature == current_signature else stored


def _plan_overview(store, cutoff, status):
    snapshot = budget.load(store)
    if snapshot['plan'] is None:
        return None, []
    plan = snapshot['plan']
    calculation = _with_compatible_breakdown(store, plan, snapshot['calculation'])
    rows = calculation['rows']
    unconfirmed = sum(not item['confirmed'] for item in plan['items'])
    end_period = rows[-1]['period']
    overview = {
        'revision': snapshot['revision'], 'title': plan['title'],
        'start_month': plan['start_month'], 'end_period': end_period,
        'horizon_months': len(rows), 'connected': False, 'connection_reason': None,
        'total_change': rows[-1]['cumulative'], 'expected_end': None,
        'minimum_balance': None, 'minimum_period': None,
        'first_negative_period': None, 'unconfirmed_count': unconfirmed,
        'unconfirmed_income': _estimated_total(calculation, 'income'),
        'unconfirmed_expenses': _estimated_total(calculation, 'expenses'),
    }
    tasks = []
    if unconfirmed:
        tasks.append(_task('unconfirmed_budget_items', 'Budgetpositionen bestätigen', unconfirmed,
                           'planning', f'{unconfirmed} Budgetpositionen sind unbestätigt.'))
    if status is None:
        overview['connection_reason'] = 'Kein angezeigter Kontostand verfügbar.'
    elif cutoff > _last_complete_month():
        overview['connection_reason'] = 'Der Monat des angezeigten Stands ist noch nicht vollständig.'
    elif not _month_end(cutoff):
        overview['connection_reason'] = 'Der angezeigte Stand liegt nicht auf einem Monatsende.'
    elif plan['start_month'] != _next_month(cutoff.strftime('%Y-%m')):
        overview['connection_reason'] = 'Der Budgetplan beginnt nicht im Folgemonat des angezeigten Stands.'
    else:
        opening = money(status['liquidity'])
        balances = [(row['period'], opening + money(row['cumulative'])) for row in rows]
        minimum_period, minimum = min(balances, key=lambda entry: entry[1])
        overview.update({
            'connected': True,
            'connection_reason': 'Der Plan schließt an den Monatsendstand an.',
            'expected_end': _text(balances[-1][1]),
            'minimum_balance': _text(minimum),
            'minimum_period': minimum_period,
            'first_negative_period': next((period for period, balance in balances if balance < 0), None),
        })
    if not overview['connected']:
        tasks.append(_task('budget_connection', 'Budgetplan anschließen', 1, 'planning',
                           overview['connection_reason']))
    return overview, tasks


def _primary_action(plan, tasks):
    approval_kinds = {
        'unconfirmed_categories', 'unreviewed_documents', 'unconfirmed_budget_items',
    }
    approvals = [task for task in tasks if task['kind'] in approval_kinds]
    if approvals:
        count = sum(task['count'] for task in approvals)
        areas = len(approvals)
        if count == 1:
            detail = '1 offener Punkt wartet auf Bestätigung.'
        else:
            detail = (f'Offene Punkte aus {areas} Bereichen warten auf Bestätigung.'
                      if areas > 1 else 'Offene Punkte aus einem Bereich warten auf Bestätigung.')
        return {
            'label': 'Offene Punkte freigeben',
            'detail': detail,
            'target': 'approvals',
        }

    if plan is None:
        return {
            'label': 'Planung anlegen',
            'detail': 'Es ist noch kein Budgetplan gespeichert.',
            'target': 'planning',
        }
    if not plan['connected']:
        return {
            'label': 'Budgetplan anschließen',
            'detail': plan['connection_reason'],
            'target': 'planning',
        }
    return None


def build(store, as_of, status):
    """Return stable, read-only overview data for one cockpit state."""
    cutoff = date.fromisoformat(as_of)
    plan, tasks = _plan_overview(store, cutoff, status)
    for task in (_classification_task(store, cutoff), _document_task(store)):
        if task is not None:
            tasks.append(task)
    return {'plan': plan, 'tasks': tasks, 'primary_action': _primary_action(plan, tasks)}
