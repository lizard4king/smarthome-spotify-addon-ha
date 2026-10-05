"""Explicit voucher payment annotations for confirmed Bonsy invoices."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

from .core import money

_EXCLUDED = {'source_excluded_bonsy', 'duplicate_source_document', 'not_invoice_like'}


def voucher_total(store, document_id) -> Decimal:
    """Return the confirmed voucher amount associated with a document."""
    rows = store.db.execute(
        'SELECT v.amount FROM bonsy_voucher_payments v '
        'JOIN bonsy_receipts r ON r.entry_id=v.entry_id WHERE r.document_id=?',
        (document_id,),
    )
    return sum((money(row['amount']) for row in rows), Decimal('0.00'))


def payment(store, entry_id):
    """Return a voucher annotation for display, or None."""
    row = store.db.execute(
        'SELECT v.entry_id,r.document_id,v.amount,v.confirmed_at '
        'FROM bonsy_voucher_payments v JOIN bonsy_receipts r USING(entry_id) '
        'WHERE v.entry_id=?', (entry_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def _amount(value):
    if not isinstance(value, str):
        raise ValueError('invalid_bonsy_voucher_amount')  # noqa: TRY004 -- API validation contract.
    try:
        amount = money(value)
    except ValueError as error:
        raise ValueError('invalid_bonsy_voucher_amount') from error
    if amount < 0:
        raise ValueError('invalid_bonsy_voucher_amount')
    return amount


def _excluded(warnings):
    try:
        values = json.loads(warnings)
    except (TypeError, ValueError):
        return True
    return (not isinstance(values, list)
            or any(not isinstance(value, str) for value in values)
            or bool(_EXCLUDED.intersection(values)))


def set_payment(store, data):
    """Set or revoke a voucher annotation without touching ledger or cash data."""
    if (not isinstance(data, dict)
            or set(data) != {'entry_id', 'amount', 'document_revision', 'confirmed'}
            or not isinstance(data['entry_id'], str) or not data['entry_id'].strip()
            or type(data['document_revision']) is not int
            or data['confirmed'] is not True):
        raise ValueError('invalid_bonsy_voucher_confirmation')
    amount = _amount(data['amount'])
    store.db.execute('BEGIN IMMEDIATE')
    try:
        row = store.db.execute(
            'SELECT r.entry_id,r.document_id,r.total,r.currency AS receipt_currency,'
            'd.kind,d.status,d.currency AS document_currency,d.amount AS document_amount,'
            'd.warnings,d.revision '
            'FROM bonsy_receipts r JOIN classification_documents d ON d.id=r.document_id '
            'WHERE r.entry_id=?', (data['entry_id'],),
        ).fetchone()
        if row is None:
            raise ValueError('unknown_bonsy_receipt')
        if row['revision'] != data['document_revision']:
            raise ValueError('stale_document_revision')
        old = payment(store, data['entry_id'])
        if amount > 0:
            if (row['kind'] != 'invoice' or row['status'] != 'confirmed'
                    or row['receipt_currency'] != 'EUR' or row['document_currency'] != 'EUR'
                    or row['document_amount'] is None or _excluded(row['warnings'])):
                raise ValueError('confirmed_eur_bonsy_invoice_required')
            try:
                receipt_total = money(row['total'])
                document_total = money(row['document_amount'])
            except ValueError as error:
                raise ValueError('invalid_bonsy_receipt_total') from error
            if receipt_total <= 0 or receipt_total != document_total:
                raise ValueError('bonsy_document_total_mismatch')
            links = store.db.execute(
                'SELECT allocation_type,allocated_amount FROM classification_document_links '
                'WHERE document_id=?', (row['document_id'],),
            ).fetchall()
            if any(link['allocation_type'] in {'refund', 'evidence'} for link in links):
                raise ValueError('bonsy_voucher_incompatible_link')
            direct = sum((money(link['allocated_amount']) for link in links), Decimal('0.00'))
            cash = sum((money(link['allocated_amount']) for link in store.db.execute(
                'SELECT allocated_amount FROM bonsy_cash_allocations WHERE entry_id=?',
                (data['entry_id'],))), Decimal('0.00'))
            if amount + direct + cash > receipt_total:
                raise ValueError('bonsy_voucher_exceeds_remaining')
        changed = old is None if amount > 0 else old is not None
        if old is not None and amount > 0:
            changed = money(old['amount']) != amount
        if changed:
            if amount == 0:
                store.db.execute('DELETE FROM bonsy_voucher_payments WHERE entry_id=?',
                                 (data['entry_id'],))
            else:
                store.db.execute(
                    'INSERT INTO bonsy_voucher_payments(entry_id,amount,confirmed_at) '
                    'VALUES (?,?,?) ON CONFLICT(entry_id) DO UPDATE SET '
                    'amount=excluded.amount,confirmed_at=excluded.confirmed_at',
                    (data['entry_id'], format(amount, '.2f'), datetime.now(UTC).isoformat()),
                )
            updated = store.db.execute(
                'UPDATE classification_documents SET revision=revision+1 '
                'WHERE id=? AND revision=?', (row['document_id'], row['revision']),
            )
            if updated.rowcount != 1:
                raise ValueError('stale_document_revision')
            from .classification import _audit  # Lazy import keeps helpers cycle-free.
            _audit(store, 'bonsy_voucher_payment_changed',
                   {'entry_id': data['entry_id'], 'amount': format(amount, '.2f')},
                   document_id=row['document_id'], previous=old)
        store.db.commit()
    except Exception:
        store.db.rollback()
        raise
    return {
        'entry_id': data['entry_id'], 'document_id': row['document_id'],
        'amount': format(amount, '.2f'),
        'document_revision': row['revision'] + int(changed), 'changed': changed,
    }
