"""Read-only cockpit overview derived from stored reviews and budget snapshots."""

import calendar
from datetime import date, datetime, timedelta
from decimal import Decimal

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
        "SELECT COUNT(*) FROM active_transactions t "
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


def _year_actuals(store, cutoff, year):
    """Sum non-transfer cash flows in the current calendar year through cutoff.

    Store.status validates lifetime opening balances, so it cannot supply this
    independent year window. Transfer semantics still use the shared resolver.
    """
    start = date(year, 1, 1)
    end = min(cutoff, date(year, 12, 31))
    income = expenses = money(0)
    if end >= start:
        from .transfer_corrections import effective_transfer_id
        rows = store.db.execute(
            'SELECT t.*,m.pair_id AS correction_pair_id,c.description FROM active_transactions t '
            'LEFT JOIN transfer_correction_members m '
            'ON m.account_id=t.account_id AND m.external_id=t.external_id '
            'LEFT JOIN transaction_context c '
            'ON c.account_id=t.account_id AND c.external_id=t.external_id '
            'WHERE t.date >= ? AND t.date <= ?', (start.isoformat(), end.isoformat()))
        for row in rows:
            if effective_transfer_id(store, row):
                continue
            amount = money(row['amount'])
            income += max(amount, money(0))
            expenses += max(-amount, money(0))
    return {'year': year, 'income': _text(income), 'expenses': _text(expenses),
            'balance': _text(income - expenses)}


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


def _reserve_overview(calculation, plan, cutoff, connected, opening, unavailable_reason=None):
    """Attach only the existing, connected bank basis to whole-plan reserves."""
    from .reserves import liquidity_at

    source = calculation['reserves']
    threshold = source['safety_threshold']
    initial_amounts = [pot['opening_amount'] for pot in source['pots']]
    initial_bound = (None if any(amount is None for amount in initial_amounts)
                     else format(sum((Decimal(amount) for amount in initial_amounts), Decimal(0)), '.2f'))

    def liquidity(row, balance):
        result = liquidity_at(row, format(balance, '.2f') if connected else None, threshold)
        if not connected:
            result['unavailable_reason'] = unavailable_reason or 'plan_not_connected'
        return result

    initial = liquidity({'bound_total': initial_bound}, opening)
    cumulative = {row['period']: Decimal(row['cumulative']) for row in calculation['rows']}
    rows = [{**row, 'liquidity': liquidity(
        row, opening + cumulative[row['period']] if connected else None)}
        for row in source['rows']]
    points = [(plan['start_month'] + '-01', initial)] + [
        (row['period'], row['liquidity']) for row in rows]
    complete = all(info['free_available'] is not None for _, info in points)
    minimum = min(points, key=lambda point: Decimal(point[1]['free_available'])) if complete else None
    maximum = (max(points, key=lambda point: Decimal(point[1]['safety_shortfall']))
               if complete and threshold is not None else None)
    result = {**source, 'available': complete,
              'unavailable_reason': None if complete else initial['unavailable_reason'],
              'rows': rows,
              'selected_row': next((row for row in rows
                                    if row['period'] == cutoff.strftime('%Y-%m')), None),
              'initial_liquidity': initial,
              'minimum_free_available': minimum[1]['free_available'] if minimum else None,
              'minimum_period': minimum[0] if minimum else None,
              'maximum_safety_shortfall': maximum[1]['safety_shortfall'] if maximum else None,
              'maximum_shortfall_period': maximum[0] if maximum else None}
    tasks = []
    if initial_bound is None:
        tasks.append(_task('reserve_opening_unknown', 'Anfangsbestand der Töpfe prüfen', 1,
                           'planning', 'Mindestens ein Topf hat keinen bekannten Anfangsbestand; '
                           'frei verfügbare Mittel und Reservewarnung sind nicht berechenbar.'))
    if maximum and Decimal(maximum[1]['safety_shortfall']) > 0:
        tasks.append(_task('reserve_shortfall', 'Sicherheitsreserve prüfen', 1, 'planning',
                           f"Die frei verfügbaren Mittel unterschreiten die Sicherheitsreserve "
                           f"um bis zu {maximum[1]['safety_shortfall']} EUR ({maximum[0]})."))
    return result, tasks


def _plan_overview(store, cutoff, status, reporting_history=None):
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
    elif status.get('liquidity') is None:
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
    if 'reserves' in calculation:
        from .plan_actual import _account_balances
        from .reporting_history import scope_for_month

        scope = scope_for_month(store, plan['start_month'], reporting_history)
        account_ids = plan.get('liquidity_accounts', [])
        coverage = scope.get('available_from_by_account', {})
        reserve_reason = None
        balances = None
        if scope['mode'] == 'individual':
            reserve_reason = 'whole_plan_only'
        elif not overview['connected']:
            reserve_reason = 'plan_not_connected'
        elif not account_ids:
            reserve_reason = 'no_liquidity_accounts'
        elif any(account_id in coverage and plan['start_month'] + '-01' < coverage[account_id]
                 for account_id in account_ids):
            reserve_reason = 'incomplete_source_coverage'
        else:
            balances = _account_balances(store, cutoff.isoformat(), account_ids)
            if balances is None:
                reserve_reason = 'missing_bank_balance'
        overview['reserves'], reserve_tasks = _reserve_overview(
            calculation, plan, cutoff, reserve_reason is None,
            sum(balances.values(), money(0)) if balances is not None else None,
            reserve_reason)
        overview['reserves']['bank_basis'] = {
            'type': 'ledger_projection', 'date': cutoff.isoformat(), 'account_ids': account_ids}
        tasks.extend(reserve_tasks)
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
    reserve_task = next((task for task in tasks
                         if task['kind'] in {'reserve_shortfall', 'reserve_opening_unknown'}), None)
    if reserve_task is not None:
        return {key: reserve_task[key] for key in ('label', 'detail', 'target')}
    return None


def build(store, as_of, status, *, _today=None, reporting_history=None):
    """Return stable, read-only overview data for one cockpit state."""
    cutoff = date.fromisoformat(as_of)
    today = datetime.now().astimezone().date() if _today is None else _today
    plan, tasks = _plan_overview(store, cutoff, status, reporting_history)
    for task in (_classification_task(store, cutoff), _document_task(store)):
        if task is not None:
            tasks.append(task)
    return {'plan': plan, 'tasks': tasks, 'primary_action': _primary_action(plan, tasks),
            'actuals': _year_actuals(store, cutoff, today.year)}
