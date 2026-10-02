"""Assess whether unlinked Bonsy receipts likely represent cash payments."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from .classification import (
    _cash_withdrawal_evidence,
    _document_link_rejected,
    document_match_suggestions,
)
from .core import money


def _withdrawals(store):
    rows = store.db.execute(
        """SELECT t.account_id,t.external_id,t.date,t.amount,
                  COALESCE(NULLIF(a.display_name,''),a.id) AS account_label
           FROM transactions t
           JOIN accounts a ON a.id=t.account_id
           JOIN classification_overrides o
             ON o.account_id=t.account_id AND o.external_id=t.external_id
            AND o.confirmed=1 AND o.category_id='AUSGABEN_BARGELD'
           LEFT JOIN transfer_correction_members tm
             ON tm.account_id=t.account_id AND tm.external_id=t.external_id
           WHERE CAST(t.amount AS REAL)<0 AND t.currency='EUR'
             AND (t.transfer_id IS NULL OR t.transfer_id='') AND tm.pair_id IS NULL
           ORDER BY t.date,t.account_id,t.external_id""").fetchall()
    result = []
    for row in rows:
        amount = abs(money(row['amount']))
        allocated = sum((money(link[0]) for link in store.db.execute(
            'SELECT allocated_amount FROM bonsy_cash_allocations '
            'WHERE account_id=? AND external_id=?',
            (row['account_id'], row['external_id']))), Decimal(0))
        result.append({
            'account_id': row['account_id'], 'external_id': row['external_id'],
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
    matches = document_match_suggestions(
        store, {'document_id': document_id, 'max_days': 45, 'page': 0})
    return (
        any(item['allocation_type'] == 'payment' for item in matches['suggestions'])
        or any(group['allocation_type'] == 'payment'
               for group in matches['suggestion_groups'])
    )


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
                  substr(r.occurred_at,1,10) AS date,r.vendor,r.total
           FROM bonsy_receipts r
           WHERE CAST(r.total AS REAL)>0
           ORDER BY r.occurred_at,r.entry_id""").fetchall()
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
