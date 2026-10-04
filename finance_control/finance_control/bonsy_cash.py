"""Assess whether unlinked Bonsy receipts likely represent cash payments."""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_DOWN, Decimal

from .cash_components import cash_principal
from .classification import (
    _cash_withdrawal_evidence,
    _document_link_rejected,
    document_match_suggestions,
)
from .core import money

_EXCLUDED_BONSY_WARNING = 'source_excluded_bonsy'


def _source_excluded(warnings):
    try:
        values = json.loads(warnings or '[]')
    except (TypeError, ValueError):
        return False
    return isinstance(values, list) and _EXCLUDED_BONSY_WARNING in values


def _withdrawals(store):
    rows = store.db.execute(
        """SELECT t.account_id,t.external_id,t.date,t.amount,a.owner,
                  t.category AS source_category,o.category_id AS category,
                  c.counterparty,c.description,
                  COALESCE(NULLIF(a.display_name,''),a.id) AS account_label
           FROM transactions t
           JOIN accounts a ON a.id=t.account_id
           JOIN classification_overrides o
             ON o.account_id=t.account_id AND o.external_id=t.external_id
            AND o.confirmed=1 AND o.category_id='AUSGABEN_BARGELD'
           LEFT JOIN transaction_context c
             ON c.account_id=t.account_id AND c.external_id=t.external_id
           LEFT JOIN transfer_correction_members tm
             ON tm.account_id=t.account_id AND tm.external_id=t.external_id
           WHERE CAST(t.amount AS REAL)<0 AND t.currency='EUR'
             AND (t.transfer_id IS NULL OR t.transfer_id='') AND tm.pair_id IS NULL
           ORDER BY t.date,t.account_id,t.external_id""").fetchall()
    result = []
    for row in rows:
        amount = cash_principal(row)
        allocated = sum((money(link[0]) for link in store.db.execute(
            'SELECT allocated_amount FROM bonsy_cash_allocations '
            'WHERE account_id=? AND external_id=?',
            (row['account_id'], row['external_id']))), Decimal(0))
        result.append({
            'account_id': row['account_id'], 'external_id': row['external_id'],
            'owner': row['owner'],
            'date': row['date'], 'account_label': row['account_label'],
            'amount': format(amount, '.2f'),
            'allocated': format(allocated, '.2f'),
            'remaining': format(max(Decimal(0), amount - allocated), '.2f'),
        })
    return result


def _has_broad_exact_payment_candidate(store, document_id, document_date, amount):
    """Catch processor-labelled payments that lack the receipt vendor text."""
    issued = date.fromisoformat(document_date)
    date_to = (issued + timedelta(days=45)).isoformat()
    rows = store.db.execute(
        """SELECT t.account_id,t.external_id,t.amount
           FROM transactions t
           LEFT JOIN transfer_correction_members tm
             ON tm.account_id=t.account_id AND tm.external_id=t.external_id
           WHERE CAST(t.amount AS REAL)<0 AND t.currency='EUR'
             AND t.amount=?
             AND t.date>=? AND t.date<=?
             AND (t.transfer_id IS NULL OR t.transfer_id='') AND tm.pair_id IS NULL""",
        (format(-amount, '.2f'), document_date, date_to),
    ).fetchall()
    for row in rows:
        if (_document_link_rejected(
                store, row['account_id'], row['external_id'], document_id)
                or _cash_withdrawal_evidence(
                    store, row['account_id'], row['external_id'])):
            continue
        allocated = sum((money(link[0]) for link in store.db.execute(
            'SELECT allocated_amount FROM classification_document_links '
            'WHERE account_id=? AND external_id=?',
            (row['account_id'], row['external_id']))), Decimal(0))
        if abs(money(row['amount'])) - allocated == amount:
            return True
    return False


def _has_payment_candidate(store, document_id, document_date, amount):
    if _has_broad_exact_payment_candidate(
            store, document_id, document_date, amount):
        return True
    page = 0
    while True:
        matches = document_match_suggestions(
            store, {'document_id': document_id, 'max_days': 45, 'page': page})
        if (any(item['allocation_type'] == 'payment' for item in matches['suggestions'])
                or any(group['allocation_type'] == 'payment'
                       for group in matches['suggestion_groups'])):
            return True
        page += 1
        if page >= matches['pages']:
            return False


def _period(data, *, require_confirmation=False):
    fields = {'date_from', 'date_to'}
    if require_confirmation:
        fields.add('confirmed')
    if not isinstance(data, dict) or set(data) != fields:
        raise ValueError('invalid_bonsy_cash_period')
    if require_confirmation and data['confirmed'] is not True:
        raise ValueError('bonsy_cash_confirmation_required')
    values = []
    for key in ('date_from', 'date_to'):
        value = data[key]
        if not isinstance(value, str):
            raise ValueError('invalid_bonsy_cash_period')  # noqa: TRY004 -- API validation contract.
        try:
            parsed = date.fromisoformat(value)
        except ValueError as error:
            raise ValueError('invalid_bonsy_cash_period') from error
        if parsed.isoformat() != value:
            raise ValueError('invalid_bonsy_cash_period')
        values.append(parsed)
    if values[0] > values[1]:
        raise ValueError('invalid_bonsy_cash_period')
    return values[0], values[1]


def _cents(value):
    return int((money(value) * 100).to_integral_exact())


def _proportional_cents(total, capacities):
    """Split cents by available cash, conserving every cent and respecting caps."""
    amount = min(max(_cents(total), 0), sum(max(value, 0) for value in capacities.values()))
    active = {key: max(value, 0) for key, value in capacities.items() if value > 0}
    result = {key: 0 for key in capacities}
    while amount and active:
        weight_total = sum(active.values())
        exact = {key: Decimal(amount * capacity) / weight_total
                 for key, capacity in active.items()}
        shares = {key: int(value.to_integral_value(rounding=ROUND_DOWN))
                  for key, value in exact.items()}
        remainder = amount - sum(shares.values())
        order = sorted(active, key=lambda key: (-(exact[key] - shares[key]), str(key)))
        for key in order[:remainder]:
            shares[key] += 1
        capped = [key for key, share in shares.items() if share > active[key]]
        if capped:
            for key in capped:
                result[key] += active[key]
                amount -= active[key]
                active.pop(key)
            continue
        for key, share in shares.items():
            result[key] += share
        amount = 0
    return {key: Decimal(value) / 100 for key, value in result.items() if value}


def _receipt_links(store, document_id):
    return list(store.db.execute(
        "SELECT allocation_type FROM classification_document_links "
        "WHERE document_id=? AND allocation_type IN ('payment','refund','evidence')",
        (document_id,),
    ))


def _cash_allocations(store, entry_id):
    return {
        (row['account_id'], row['external_id']): money(row['allocated_amount'])
        for row in store.db.execute(
            'SELECT account_id,external_id,allocated_amount '
            'FROM bonsy_cash_allocations WHERE entry_id=?', (entry_id,))
    }


def _build_allocation_preview(store, date_from, date_to, *, today=None):
    today = today or datetime.now().astimezone().date()
    withdrawals = _withdrawals(store)
    cash_left = {
        (row['account_id'], row['external_id']): money(row['remaining'])
        for row in withdrawals
    }
    withdrawal_by_key = {
        (row['account_id'], row['external_id']): row for row in withdrawals
    }
    people = {row['id'] for row in store.db.execute('SELECT id FROM persons')}
    receipts = store.db.execute(
        """SELECT r.entry_id,r.document_id,substr(r.occurred_at,1,10) AS date,
                  r.vendor,r.total,d.warnings
           FROM bonsy_receipts r
           JOIN classification_documents d ON d.id=r.document_id
           WHERE d.kind='invoice' AND d.status='confirmed'
             AND r.currency='EUR' AND CAST(r.total AS REAL)>0
             AND substr(r.occurred_at,1,10) BETWEEN ? AND ?
           ORDER BY r.occurred_at,r.entry_id""",
        (date_from.isoformat(), date_to.isoformat()),
    ).fetchall()
    receipts = [row for row in receipts if not _source_excluded(row['warnings'])]
    planned = []
    for receipt in receipts:
        amount = money(receipt['total'])
        existing = _cash_allocations(store, receipt['entry_id'])
        covered = sum(existing.values(), Decimal('0.00'))
        remaining = max(amount - covered, Decimal('0.00'))
        item = {
            'entry_id': receipt['entry_id'], 'document_id': receipt['document_id'],
            'date': receipt['date'], 'vendor': receipt['vendor'],
            'amount': format(amount, '.2f'), 'covered': format(covered, '.2f'),
            'remaining': format(remaining, '.2f'), 'reason': None, 'allocations': [],
        }
        if remaining <= 0:
            item['reason'] = 'already_covered'
            planned.append(item)
            continue
        links = _receipt_links(store, receipt['document_id'])
        if links:
            item['reason'] = ('evidence_link_requires_manual_review'
                              if any(link['allocation_type'] == 'evidence' for link in links)
                              else 'direct_payment_or_refund_requires_review')
            planned.append(item)
            continue
        if _has_payment_candidate(
                store, receipt['document_id'], receipt['date'], remaining):
            item['reason'] = 'direct_payment_candidate_requires_review'
            planned.append(item)
            continue
        try:
            age = (today - date.fromisoformat(receipt['date'])).days
        except ValueError:
            age = -1
        if age <= 5:
            item['reason'] = 'pending_bank_posting'
            planned.append(item)
            continue

        receipt_date = date.fromisoformat(receipt['date'])
        earliest = receipt_date - timedelta(days=45)
        candidates = [
            withdrawal_by_key[(row['account_id'], row['external_id'])]
            for row in withdrawals
            if row['owner'] in people
            and earliest <= date.fromisoformat(row['date']) <= receipt_date
            and row['date'][:7] == receipt['date'][:7]
            and cash_left[(row['account_id'], row['external_id'])] > 0
        ]
        if not candidates:
            item['reason'] = 'no_eligible_cash_withdrawals'
            planned.append(item)
            continue
        person_capacity = {}
        withdrawal_capacity = {}
        for row in candidates:
            key = (row['account_id'], row['external_id'])
            capacity = _cents(cash_left[key])
            person_capacity[row['owner']] = person_capacity.get(row['owner'], 0) + capacity
            withdrawal_capacity[key] = capacity
        person_allocations = _proportional_cents(remaining, person_capacity)
        for owner, owner_amount in person_allocations.items():
            owner_withdrawals = {
                (row['account_id'], row['external_id']): withdrawal_capacity[
                    (row['account_id'], row['external_id'])]
                for row in candidates if row['owner'] == owner
            }
            for key, allocated in _proportional_cents(
                    owner_amount, owner_withdrawals).items():
                item['allocations'].append({
                    'account_id': key[0], 'external_id': key[1],
                    'date': withdrawal_by_key[key]['date'], 'owner': owner,
                    'account_label': withdrawal_by_key[key]['account_label'],
                    'amount': format(allocated, '.2f'),
                })
                cash_left[key] -= allocated
        allocated = sum((money(part['amount']) for part in item['allocations']), Decimal(0))
        item['reason'] = ('ready' if allocated == remaining else 'partial_cash_available')
        item['proposed'] = format(allocated, '.2f')
        item['unmatched'] = format(remaining - allocated, '.2f')
        planned.append(item)
    return {
        'date_from': date_from.isoformat(), 'date_to': date_to.isoformat(),
        'receipts': planned, 'withdrawals': withdrawals,
        'totals': {
            'receipts': len(receipts),
            'ready': sum(item['reason'] == 'ready' for item in planned),
            'partial': sum(item['reason'] == 'partial_cash_available' for item in planned),
            'blocked': sum(item['reason'] not in {'ready', 'partial_cash_available',
                                                  'already_covered'} for item in planned),
            'proposed': format(sum((money(item.get('proposed', '0.00'))
                                   for item in planned), Decimal(0)), '.2f'),
            'unmatched': format(sum((money(item.get('unmatched', '0.00'))
                                     for item in planned), Decimal(0)), '.2f'),
        },
    }


def preview(store, data, *, _today=None):
    """Preview explicit historical allocations without changing ledger data."""
    date_from, date_to = _period(data)
    return _build_allocation_preview(store, date_from, date_to, today=_today)


def apply(store, data, *, _today=None):
    """Atomically apply a user-confirmed historical cash allocation preview."""
    date_from, date_to = _period(data, require_confirmation=True)
    store.db.execute('BEGIN IMMEDIATE')
    try:
        result = _build_allocation_preview(store, date_from, date_to, today=_today)
        timestamp = datetime.now(UTC).isoformat()
        changes = 0
        total = Decimal('0.00')
        for receipt in result['receipts']:
            if receipt['reason'] not in {'ready', 'partial_cash_available'}:
                continue
            for allocation in receipt['allocations']:
                key = (receipt['entry_id'], allocation['account_id'], allocation['external_id'])
                previous = store.db.execute(
                    'SELECT allocated_amount FROM bonsy_cash_allocations '
                    'WHERE entry_id=? AND account_id=? AND external_id=?', key).fetchone()
                amount = money(allocation['amount'])
                if previous is not None:
                    amount += money(previous['allocated_amount'])
                    store.db.execute(
                        'UPDATE bonsy_cash_allocations SET allocated_amount=?,confirmed_at=? '
                        'WHERE entry_id=? AND account_id=? AND external_id=?',
                        (format(amount, '.2f'), timestamp, *key))
                else:
                    store.db.execute(
                        'INSERT INTO bonsy_cash_allocations '
                        '(entry_id,account_id,external_id,allocated_amount,confirmed_at) '
                        'VALUES (?,?,?,?,?)', (*key, format(amount, '.2f'), timestamp))
                changes += 1
                total += money(allocation['amount'])
        store.db.commit()
    except Exception:
        store.db.rollback()
        raise
    result['applied_allocations'] = changes
    result['applied_amount'] = format(total, '.2f')
    return result


def _missing_payment_assessment(document_date, amount, *, today=None):
    """Classify a receipt only after all stronger payment evidence is absent."""
    today = today or datetime.now().astimezone().date()
    try:
        age_days = (today - date.fromisoformat(document_date)).days
    except (TypeError, ValueError):
        return 'no_direct_payment_found_requires_review', None
    if age_days < 0:
        return 'no_direct_payment_found_requires_review', None
    if age_days <= 5:
        return 'pending_bank_posting', {
            'method': 'bank_debit_or_card',
            'confidence': 'pending',
            'confirmed': False,
            'basis': 'receipt_younger_than_or_equal_5_days',
        }
    if amount < Decimal('50.00'):
        return 'likely_cash_no_direct_payment_found', {
            'method': 'cash',
            'confidence': 'very_high',
            'confirmed': False,
            'basis': 'no_direct_payment_candidate',
        }
    return 'no_direct_payment_found_requires_review', None


def overview(store, _data=None, *, _today=None):
    """List receipt payment status without creating links or allocations."""
    withdrawals = _withdrawals(store)
    available = {
        (row['account_id'], row['external_id']): money(row['remaining'])
        for row in withdrawals
    }
    receipts = store.db.execute(
        """SELECT r.entry_id,r.document_id,
                  substr(r.occurred_at,1,10) AS date,r.vendor,r.total,d.warnings
           FROM bonsy_receipts r
           JOIN classification_documents d ON d.id=r.document_id
           WHERE d.kind='invoice' AND d.status='confirmed'
             AND CAST(r.total AS REAL)>0
           ORDER BY r.occurred_at,r.entry_id""").fetchall()
    receipts = [row for row in receipts if not _source_excluded(row['warnings'])]
    unresolved, confirmed = [], []
    for row in receipts:
        amount = money(row['total'])
        links = store.db.execute(
            """SELECT l.account_id,l.external_id,l.allocated_amount,l.allocation_type,t.date,
                      COALESCE(NULLIF(a.display_name,''),a.id) AS account_label
               FROM classification_document_links l
               JOIN transactions t USING(account_id,external_id)
               JOIN accounts a ON a.id=l.account_id
               WHERE l.document_id=? AND l.allocation_type IN ('payment','refund','evidence')
               ORDER BY t.date,l.account_id,l.external_id""",
            (row['document_id'],),
        ).fetchall()
        direct_links = [link for link in links if link['allocation_type'] in {'payment', 'refund'}]
        evidence_links = [link for link in links if link['allocation_type'] == 'evidence']
        direct_allocated = sum((money(link['allocated_amount']) for link in direct_links), Decimal(0))
        cash_allocated = sum((money(link[0]) for link in store.db.execute(
            'SELECT allocated_amount FROM bonsy_cash_allocations WHERE entry_id=?',
            (row['entry_id'],))), Decimal(0))
        allocated = direct_allocated + cash_allocated
        source_items = [{
            'account_id': link['account_id'], 'external_id': link['external_id'],
            'date': link['date'], 'account_label': link['account_label'],
            'amount': format(money(link['allocated_amount']), '.2f'),
            'allocation_type': link['allocation_type'],
        } for link in links]
        if allocated >= amount:
            confirmed.append({'entry_id': row['entry_id'], 'date': row['date'],
                              'vendor': row['vendor'], 'amount': format(amount, '.2f'),
                              'reason': ('direct_payment_linked' if direct_allocated >= amount
                                         else 'legacy_cash_allocation'),
                              'sources': source_items,
                              'direct_allocated': format(direct_allocated, '.2f')})
            continue
        assessment = None
        if direct_links:
            reason = 'partial_direct_transaction_link'
        elif cash_allocated > 0:
            reason = 'partial_legacy_cash_allocation'
        elif evidence_links:
            reason = 'evidence_link_requires_manual_review'
        elif _has_payment_candidate(
                store, row['document_id'], row['date'], amount):
            reason = 'direct_payment_review'
        else:
            reason, assessment = _missing_payment_assessment(
                row['date'], amount, today=_today)
        item = {
            'entry_id': row['entry_id'], 'document_id': row['document_id'],
            'date': row['date'], 'vendor': row['vendor'],
            'amount': format(amount, '.2f'),
            'sources': source_items,
            'covered': format(allocated, '.2f'),
            'remaining': format(amount - allocated, '.2f'),
            'reason': reason,
        }
        if assessment is not None:
            item['payment_assessment'] = assessment
        unresolved.append(item)
    remaining = []
    for row in withdrawals:
        value = available[(row['account_id'], row['external_id'])]
        if value > 0:
            remaining.append({**row, 'projected_remaining': format(value, '.2f')})
    return {
        'proposals': [], 'unresolved': unresolved, 'confirmed': confirmed,
        'withdrawals': withdrawals, 'remaining_withdrawals': remaining,
        'counts': {
            'proposals': 0, 'unresolved': len(unresolved),
            'confirmed': len(confirmed), 'withdrawals': len(withdrawals),
            'likely_cash': sum(
                item['reason'] == 'likely_cash_no_direct_payment_found'
                for item in unresolved),
            'pending_posting': sum(
                item['reason'] == 'pending_bank_posting'
                for item in unresolved),
            'payment_review': sum(
                item['reason'] not in {
                    'likely_cash_no_direct_payment_found',
                    'pending_bank_posting',
                }
                for item in unresolved),
        },
        'projected_cash_remaining': format(sum(
            (money(row['projected_remaining']) for row in remaining), Decimal(0)), '.2f'),
    }
