"""Local, resumable persistence of Amazon Data Portability evidence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal

from .classification import register_document
from .providers import ProviderBatch, SourceDocument, SourceLineItem, SourceProvenance


class AmazonPortabilityImportError(ValueError):
    """Sanitized import failure; never contains source content."""


@dataclass(frozen=True)
class ImportResult:
    orders: int = 0
    refunds: int = 0
    line_items: int = 0
    duplicates: int = 0


def _decimal(value: Decimal | None) -> str | None:
    return None if value is None else format(value.normalize(), 'f')


def _fingerprint(*fields: object) -> str:
    payload = json.dumps(fields, ensure_ascii=False, separators=(',', ':'), sort_keys=True)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _document_fingerprint(row: SourceDocument) -> str:
    return _fingerprint(row.external_id, row.title, row.issued_on.isoformat() if row.issued_on else None,
                        row.external_account_id, _decimal(row.amount), row.currency, row.related_external_id)


def _item_fingerprint(row: SourceLineItem) -> str:
    return _fingerprint(row.external_id, row.document_external_id, row.description,
                        _decimal(row.quantity), row.product_reference, _decimal(row.amount), row.currency)


def _check_batch(batch: ProviderBatch) -> None:
    if (not isinstance(batch, ProviderBatch)
            or not isinstance(batch.provenance, SourceProvenance)
            or batch.provenance.provider_id != 'amazon-portability'
            or not isinstance(batch.documents, tuple)
            or not isinstance(batch.line_items, tuple)):
        raise AmazonPortabilityImportError('invalid_amazon_batch')
    if batch.accounts or batch.balances or batch.transactions or batch.positions:
        raise AmazonPortabilityImportError('unsupported_amazon_content')
    ids: dict[str, str] = {}
    for row in batch.documents:
        if not isinstance(row, SourceDocument) or not isinstance(row.external_id, str):
            raise AmazonPortabilityImportError('invalid_amazon_document')
        if row.related_external_id is None:
            if not row.external_id.startswith('amazon:order:') or not row.external_id.removeprefix('amazon:order:'):
                raise AmazonPortabilityImportError('invalid_amazon_order')
            if row.amount is None or row.amount < 0:
                raise AmazonPortabilityImportError('invalid_amazon_order')
        else:
            if not row.external_id.startswith('amazon:refund:') or not row.related_external_id.startswith('amazon:order:'):
                raise AmazonPortabilityImportError('invalid_amazon_refund')
            if row.amount is None or row.amount >= 0:
                raise AmazonPortabilityImportError('invalid_amazon_refund')
        if row.currency != 'EUR':
            raise AmazonPortabilityImportError('unsupported_amazon_currency')
        fingerprint = _document_fingerprint(row)
        if row.external_id in ids and ids[row.external_id] != fingerprint:
            raise AmazonPortabilityImportError('amazon_identity_conflict')
        ids[row.external_id] = fingerprint
    order_ids = {row.external_id for row in batch.documents if row.related_external_id is None}
    if any(row.related_external_id is not None and row.related_external_id not in order_ids
           for row in batch.documents):
        raise AmazonPortabilityImportError('amazon_refund_order_missing')
    item_ids: dict[tuple[str, str], str] = {}
    for row in batch.line_items:
        if not isinstance(row, SourceLineItem) or row.document_external_id not in order_ids:
            raise AmazonPortabilityImportError('invalid_amazon_line_item')
        if row.currency is not None and row.currency != 'EUR':
            raise AmazonPortabilityImportError('unsupported_amazon_currency')
        key = (row.document_external_id, row.external_id)
        fingerprint = _item_fingerprint(row)
        if key in item_ids and item_ids[key] != fingerprint:
            raise AmazonPortabilityImportError('amazon_identity_conflict')
        item_ids[key] = fingerprint


def _stored_document(store, external_id: str, fingerprint: str):
    existing = store.db.execute('SELECT * FROM amazon_portability_documents WHERE external_id=?',
                                (external_id,)).fetchone()
    if existing is not None and existing['content_sha256'] != fingerprint:
        raise AmazonPortabilityImportError('amazon_identity_conflict')
    return existing


def _insert_document(store, row: SourceDocument, document_id: int, batch: ProviderBatch,
                     fingerprint: str) -> None:
    with store.db:
        store.db.execute(
            'INSERT INTO amazon_portability_documents '
            '(external_id,content_sha256,document_id,related_external_id,amount,currency,retrieved_at,source,source_sha256) '
            'VALUES (?,?,?,?,?,?,?,?,?)',
            (row.external_id, fingerprint, document_id, row.related_external_id,
             _decimal(row.amount), row.currency, batch.provenance.retrieved_at.isoformat(),
             batch.provenance.source, batch.provenance.sha256))


def _import_order(store, row: SourceDocument, batch: ProviderBatch) -> bool:
    fingerprint = _document_fingerprint(row)
    if _stored_document(store, row.external_id, fingerprint) is not None:
        return False
    order_number = row.external_id.removeprefix('amazon:order:')
    mapping = store.db.execute('SELECT document_id FROM amazon_order_documents WHERE order_number=?',
                               (order_number,)).fetchone()
    if mapping is None:
        # Recovery if the public registration committed before this mapping was persisted.
        document = store.db.execute('SELECT id FROM classification_documents WHERE source_reference=?',
                                    (row.external_id,)).fetchone()
        if document is None:
            document_id = register_document(store, {
                'kind': 'invoice', 'vendor': 'Amazon.de', 'title': row.title,
                'document_date': row.issued_on.isoformat() if row.issued_on else None,
                'amount': _decimal(row.amount), 'currency': 'EUR',
                'source_reference': row.external_id,
                'warnings': ['amazon_data_portability_not_tax_invoice'],
                'status': 'unreviewed',
            })['document']['id']
        else:
            document_id = document['id']
    else:
        document_id = mapping['document_id']
    # A registered candidate remains resumable through its stable source_reference
    # if the following mapping transaction is interrupted.
    with store.db:
        store.db.execute('INSERT OR IGNORE INTO amazon_order_documents(order_number,document_id) VALUES (?,?)',
                         (order_number, document_id))
        check = store.db.execute('SELECT document_id FROM amazon_order_documents WHERE order_number=?',
                                 (order_number,)).fetchone()
        if check['document_id'] != document_id:
            raise AmazonPortabilityImportError('amazon_order_mapping_conflict')
        store.db.execute(
            'INSERT INTO amazon_portability_documents '
            '(external_id,content_sha256,document_id,related_external_id,amount,currency,retrieved_at,source,source_sha256) '
            'VALUES (?,?,?,?,?,?,?,?,?)',
            (row.external_id, fingerprint, document_id, None, _decimal(row.amount), row.currency,
             batch.provenance.retrieved_at.isoformat(), batch.provenance.source, batch.provenance.sha256))
    return True


def _import_refund(store, row: SourceDocument, batch: ProviderBatch) -> bool:
    fingerprint = _document_fingerprint(row)
    if _stored_document(store, row.external_id, fingerprint) is not None:
        return False
    order = store.db.execute('SELECT document_id FROM amazon_portability_documents WHERE external_id=? '
                             'AND related_external_id IS NULL', (row.related_external_id,)).fetchone()
    if order is None:
        raise AmazonPortabilityImportError('amazon_refund_order_missing')
    _insert_document(store, row, order['document_id'], batch, fingerprint)
    return True


def _import_item(store, row: SourceLineItem) -> bool:
    fingerprint = _item_fingerprint(row)
    existing = store.db.execute('SELECT content_sha256 FROM amazon_portability_line_items '
                                'WHERE document_external_id=? AND external_id=?',
                                (row.document_external_id, row.external_id)).fetchone()
    if existing is not None:
        if existing['content_sha256'] != fingerprint:
            raise AmazonPortabilityImportError('amazon_identity_conflict')
        return False
    if store.db.execute('SELECT 1 FROM amazon_portability_documents WHERE external_id=?',
                        (row.document_external_id,)).fetchone() is None:
        raise AmazonPortabilityImportError('amazon_item_document_missing')
    with store.db:
        store.db.execute('INSERT INTO amazon_portability_line_items '
                         '(document_external_id,external_id,content_sha256,description,quantity,product_reference,amount,currency) '
                         'VALUES (?,?,?,?,?,?,?,?)',
                         (row.document_external_id, row.external_id, fingerprint, row.description,
                          _decimal(row.quantity), row.product_reference, _decimal(row.amount), row.currency))
    return True


def import_amazon_portability(store, batch: ProviderBatch) -> ImportResult:
    """Import each source identity once; conflicts stop processing without overwrites."""
    _check_batch(batch)
    orders = refunds = items = duplicates = 0
    for row in sorted((r for r in batch.documents if r.related_external_id is None),
                      key=lambda r: r.external_id):
        if _import_order(store, row, batch):
            orders += 1
        else:
            duplicates += 1
    for row in sorted((r for r in batch.documents if r.related_external_id is not None),
                      key=lambda r: r.external_id):
        if _import_refund(store, row, batch):
            refunds += 1
        else:
            duplicates += 1
    for row in sorted(batch.line_items, key=lambda r: (r.document_external_id, r.external_id)):
        if _import_item(store, row):
            items += 1
        else:
            duplicates += 1
    return ImportResult(orders, refunds, items, duplicates)
