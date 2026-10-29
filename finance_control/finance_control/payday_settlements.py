"""Explicit, version-bound booking assignments for manual payday commitments."""
import hashlib
import json
import re
from datetime import date
from decimal import Decimal

from .core import money


def flow_key(cycle, flow):
    value = [cycle['account_id'], cycle['last_salary_date'], cycle['next_salary_date'],
             *[flow[name] for name in ('label', 'direction', 'amount', 'due_date', 'evidence')]]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def normalize_refs(value):
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError('invalid payday settlements')
    result, seen = [], set()
    for ref in value:
        if not isinstance(ref, dict) or set(ref) != {'external_id', 'transaction_revision', 'amount'}:
            raise ValueError('invalid payday settlement')
        key, revision, amount = ref['external_id'], ref['transaction_revision'], ref['amount']
        if (type(key) is not str or not 1 <= len(key) <= 512
                or any(ord(char) < 32 for char in key)
                or type(revision) is not int or not 1 <= revision <= 2147483647
                or type(amount) is not str or len(amount) > 15 or not re.fullmatch(r'[0-9]+(?:\.[0-9]{1,2})?', amount)
                or not Decimal('0.00') < money(amount) <= Decimal('999999999999.99')
                or (key, revision) in seen):
            raise ValueError('invalid payday settlement')
        seen.add((key, revision))
        result.append({'external_id': key, 'transaction_revision': revision,
                       'amount': format(money(amount), '.2f')})
    return result


def _booking(store, account, ref):
    return store.db.execute(
        'SELECT * FROM active_transaction_versions WHERE account_id=? AND external_id=? AND revision=?',
        (account, ref['external_id'], ref['transaction_revision'])).fetchone()


def validate_plan_links(store, plan, *, require_available=False):
    allocated = {}
    for cycle in plan.get('payday_cycles', []):
        for flow in cycle['cashflows']:
            refs = normalize_refs(flow.get('settlements', []))
            if refs and flow.get('settlement_scope', flow_key(cycle, flow)) != flow_key(cycle, flow):
                if require_available:
                    raise ValueError('payday settlement target changed')
                continue  # Old evidence remains visible, but no longer claims a booking.
            if sum((money(r['amount']) for r in refs), Decimal('0.00')) > money(flow['amount']):
                raise ValueError('payday settlement exceeds commitment')
            for ref in refs:
                key = (cycle['account_id'], ref['external_id'], ref['transaction_revision'])
                allocated[key] = allocated.get(key, Decimal('0.00')) + money(ref['amount'])
                if store is None:
                    continue
                row = _booking(store, cycle['account_id'], ref)
                if row is None and not require_available:
                    continue  # Retain old evidence inertly so other flows remain editable.
                if (row is None or row['currency'] != 'EUR'
                        or row['date'] > date.today().isoformat()
                        or (money(row['amount']) > 0) != (flow['direction'] == 'inflow')
                        or money(row['amount']) == 0
                        or allocated[key] > abs(money(row['amount']))):
                    raise ValueError('payday settlement booking unavailable or overallocated')


def allocation_totals(plan):
    totals = {}
    for cycle in plan.get('payday_cycles', []):
        for flow in cycle['cashflows']:
            if flow.get('settlement_scope', flow_key(cycle, flow)) != flow_key(cycle, flow):
                continue
            try:
                refs = normalize_refs(flow.get('settlements', []))
            except ValueError:
                continue
            for ref in refs:
                key = (cycle['account_id'], ref['external_id'], ref['transaction_revision'])
                totals[key] = totals.get(key, Decimal('0.00')) + money(ref['amount'])
    return totals


def state(store, cycle, flow, as_of, *, plan=None, _allocations=None):
    """Subtract only explicitly allocated, still active bookings already in the balance."""
    settled, invalid = Decimal('0.00'), 0
    allocations = (_allocations if _allocations is not None else
                   allocation_totals(plan or {'payday_cycles': [cycle]}))
    try:
        refs = normalize_refs(flow.get('settlements', []))
    except ValueError:
        refs, invalid = [], 1
    if refs and flow.get('settlement_scope', flow_key(cycle, flow)) != flow_key(cycle, flow):
        invalid += len(refs)
        refs = []
    for ref in refs:
        row = _booking(store, cycle['account_id'], ref)
        if (row is None or row['currency'] != 'EUR' or row['date'] > as_of
                or (money(row['amount']) > 0) != (flow['direction'] == 'inflow')
                or allocations.get((cycle['account_id'], ref['external_id'], ref['transaction_revision']),
                                   money(ref['amount'])) > abs(money(row['amount']))):
            invalid += 1
        else:
            settled += money(ref['amount'])
    if settled > money(flow['amount']):
        settled, invalid = Decimal('0.00'), invalid + 1
    return {'settled_amount': format(settled, '.2f'),
            'remaining_amount': format(money(flow['amount']) - settled, '.2f'),
            'invalid_settlement_count': invalid}


def _locate(store, data):
    from . import budget
    revision, account, key = data['revision'], data['account_id'], data['flow_key']
    if (type(revision) is not int or revision < 1 or type(account) is not str
            or type(key) is not str or not re.fullmatch('[0-9a-f]{64}', key)):
        raise ValueError('invalid payday settlement target')
    snapshot = budget.load(store)
    if snapshot['revision'] != revision:
        raise ValueError('stale active budget revision')
    matches = [(cycle, flow) for cycle in snapshot['plan'].get('payday_cycles', [])
               if cycle['account_id'] == account for flow in cycle['cashflows']
               if flow_key(cycle, flow) == key]
    if len(matches) != 1:
        raise ValueError('unknown or ambiguous payday commitment')
    return snapshot, *matches[0]


def view(store, data):
    if not isinstance(data, dict) or set(data) != {'revision', 'account_id', 'flow_key'}:
        raise ValueError('invalid payday settlement request')
    snapshot, cycle, flow = _locate(store, data)
    candidates = []
    for row in store.db.execute(
            'SELECT t.external_id,t.revision,t.date,t.amount,c.counterparty,c.description '
            'FROM active_transaction_versions t LEFT JOIN transaction_context c '
            'ON c.account_id=t.account_id AND c.external_id=t.external_id '
            'WHERE t.account_id=? AND t.currency=? AND t.date>=? AND t.date<=? '
            'AND (CAST(t.amount AS REAL)>0)=? '
            'ORDER BY t.date DESC,t.external_id LIMIT 501',
            (cycle['account_id'], 'EUR', cycle['last_salary_date'],
             min(date.today().isoformat(), cycle['next_salary_date']), flow['direction'] == 'inflow')):
        if money(row['amount']) == 0 or (money(row['amount']) > 0) != (flow['direction'] == 'inflow'):
            continue
        candidates.append({'external_id': row['external_id'], 'transaction_revision': row['revision'],
                           'date': row['date'], 'amount': format(abs(money(row['amount'])), '.2f'),
                           'counterparty': row['counterparty'] or '', 'description': row['description'] or ''})
    return {'revision': snapshot['revision'], 'latest_revision': snapshot['latest_revision'],
            'account_id': cycle['account_id'], 'flow_key': data['flow_key'],
            'label': flow['label'], 'amount': flow['amount'],
            'settlements': flow.get('settlements', []),
            **state(store, cycle, flow, date.today().isoformat(), plan=snapshot['plan']),
            'candidates': candidates[:500], 'has_more': len(candidates) > 500}


def save(store, data):
    from . import budget
    if (not isinstance(data, dict)
            or set(data) != {'revision', 'latest_revision', 'account_id', 'flow_key', 'settlements', 'confirmed'}
            or data['confirmed'] is not True or type(data['latest_revision']) is not int):
        raise ValueError('payday settlement requires explicit confirmation')
    snapshot, cycle, flow = _locate(store, data)
    if snapshot['latest_revision'] != data['latest_revision']:
        raise ValueError('stale budget revision')
    refs = normalize_refs(data['settlements'])
    if refs:
        flow['settlements'] = refs
        flow['settlement_scope'] = flow_key(cycle, flow)
    else:
        flow.pop('settlements', None)
        flow.pop('settlement_scope', None)
    validate_plan_links(store, {'payday_cycles': [{**cycle, 'cashflows': [flow]}]},
                        require_available=True)
    validate_plan_links(store, snapshot['plan'])
    saved = budget.save(store, {'revision': snapshot['latest_revision'],
                               'base_revision': snapshot['revision'], 'activate': True,
                               'active_revision': snapshot['revision'], 'plan': snapshot['plan']})
    return {'revision': saved['revision'], 'flow_key': flow_key(cycle, flow),
            'settlements': refs}
