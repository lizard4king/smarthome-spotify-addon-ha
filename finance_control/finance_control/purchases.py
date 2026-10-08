"""Read-only, evidence-based purchase and receipt overview."""

from __future__ import annotations

from datetime import date


_PAGE_SIZE = 40
_EXCLUDED_WARNINGS = ('not_invoice_like', 'source_excluded_bonsy',
                      'duplicate_source_document')


def _date(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f'invalid_{field}')
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError(f'invalid_{field}') from None
    if parsed.isoformat() != value:
        raise ValueError(f'invalid_{field}')
    return value


def _inputs(data: object) -> tuple[str, str | None, str | None, str, int, str]:
    if not isinstance(data, dict) or set(data) - {
            'query', 'date_from', 'date_to', 'kind', 'page', 'order'}:
        raise ValueError('invalid_purchase_request')
    query = data.get('query', '')
    if not isinstance(query, str) or len(query) > 200:
        raise ValueError('invalid_purchase_query')
    query = query.strip()
    start = _date(data.get('date_from'), 'purchase_date_from')
    end = _date(data.get('date_to'), 'purchase_date_to')
    if start and end and start > end:
        raise ValueError('invalid_purchase_date_range')
    kind = data.get('kind', 'all')
    if not isinstance(kind, str) or kind not in ('all', 'receipt', 'invoice'):
        raise ValueError('invalid_purchase_kind')
    page = data.get('page', 0)
    if type(page) is not int or page < 0 or page > 100_000:
        raise ValueError('invalid_purchase_page')
    order = data.get('order', 'newest')
    if not isinstance(order, str) or order not in ('newest', 'oldest'):
        raise ValueError('invalid_purchase_order')
    return query, start, end, kind, page, order


def _ids_clause(rows: list[dict]) -> tuple[str, list[int]]:
    ids = [row['id'] for row in rows]
    return ','.join('?' for _ in ids), ids


def list_purchases(store, data: dict) -> dict:
    """List local purchase evidence; never infer expenses or mutate the store.

    The page index is zero based. Date filters exclude undated documents, while
    an unfiltered view keeps them after dated documents in either order.
    """
    query, start, end, kind, page, order = _inputs(data)
    clauses = ["d.kind='invoice'", "NOT EXISTS (SELECT 1 FROM json_each(d.warnings) w "
               "WHERE w.value IN (?,?,?))"]
    params: list[object] = list(_EXCLUDED_WARNINGS)
    if start is not None:
        clauses.append('d.document_date>=?')
        params.append(start)
    if end is not None:
        clauses.append('d.document_date<=?')
        params.append(end)
    if kind == 'receipt':
        clauses.append('EXISTS (SELECT 1 FROM bonsy_receipts br WHERE br.document_id=d.id)')
    elif kind == 'invoice':
        clauses.append('NOT EXISTS (SELECT 1 FROM bonsy_receipts br WHERE br.document_id=d.id)')
    if query:
        # Escape LIKE metacharacters so a search is literal, including '%' and '_'.
        escaped = query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        pattern = '%' + escaped + '%'
        clauses.append("(d.vendor LIKE ? ESCAPE '\\' COLLATE NOCASE "
                       "OR d.title LIKE ? ESCAPE '\\' COLLATE NOCASE "
                       "OR EXISTS (SELECT 1 FROM bonsy_receipts br "
                       "JOIN bonsy_products bp ON bp.entry_id=br.entry_id "
                       "WHERE br.document_id=d.id AND bp.product_name LIKE ? ESCAPE '\\' COLLATE NOCASE) "
                       "OR EXISTS (SELECT 1 FROM amazon_portability_documents ap "
                       "JOIN amazon_portability_line_items ai ON ai.document_external_id=ap.external_id "
                       "WHERE ap.document_id=d.id AND ap.related_external_id IS NULL "
                       "AND ai.description LIKE ? ESCAPE '\\' COLLATE NOCASE))")
        params.extend((pattern,) * 4)
    where = ' WHERE ' + ' AND '.join(clauses)
    total = store.db.execute('SELECT COUNT(*) FROM classification_documents d' + where,
                             params).fetchone()[0]
    direction = 'DESC' if order == 'newest' else 'ASC'
    records = store.db.execute(
        'SELECT d.id,d.document_date,d.vendor,d.title,d.amount,d.currency,d.status, '
        'CASE WHEN EXISTS (SELECT 1 FROM bonsy_receipts br WHERE br.document_id=d.id) '
        "THEN 'receipt' ELSE 'invoice' END AS purchase_kind, "
        'CASE WHEN EXISTS (SELECT 1 FROM bonsy_receipts br WHERE br.document_id=d.id) '
        "THEN 'bonsy' WHEN EXISTS (SELECT 1 FROM amazon_portability_documents ap "
        "WHERE ap.document_id=d.id AND ap.related_external_id IS NULL) THEN 'amazon' "
        "ELSE 'document' END AS purchase_source "
        'FROM classification_documents d' + where +
        f' ORDER BY d.document_date IS NULL, d.document_date {direction}, d.id {direction}'
        ' LIMIT ? OFFSET ?', params + [_PAGE_SIZE, page * _PAGE_SIZE]).fetchall()
    rows = [{
        'id': record['id'], 'date': record['document_date'],
        'vendor': record['vendor'], 'title': record['title'],
        'amount': record['amount'], 'currency': record['currency'],
        'status': record['status'], 'kind': record['purchase_kind'],
        'source': record['purchase_source'], 'links': [], 'items': [],
        'items_note': None,
    } for record in records]
    if rows:
        placeholders, ids = _ids_clause(rows)
        by_id = {row['id']: row for row in rows}
        for link in store.db.execute(
                'SELECT document_id,account_id,external_id,allocated_amount,allocation_type '
                f'FROM classification_document_links WHERE document_id IN ({placeholders}) '
                'ORDER BY document_id,account_id,external_id', ids):
            by_id[link['document_id']]['links'].append({
                key: link[key] for key in
                ('account_id', 'external_id', 'allocated_amount', 'allocation_type')})
        for item in store.db.execute(
                'SELECT br.document_id,bp.product_name,bp.quantity,bp.unit,bp.paid_price,br.currency '
                'FROM bonsy_receipts br JOIN bonsy_products bp ON bp.entry_id=br.entry_id '
                f'WHERE br.document_id IN ({placeholders}) '
                'ORDER BY br.document_id,bp.line_number', ids):
            by_id[item['document_id']]['items'].append({
                'description': item['product_name'], 'quantity': item['quantity'],
                'unit': item['unit'], 'amount': item['paid_price'], 'currency': item['currency']})
        for item in store.db.execute(
                'SELECT ap.document_id,ai.description,ai.quantity,ai.amount,ai.currency '
                'FROM amazon_portability_documents ap '
                'JOIN amazon_portability_line_items ai ON ai.document_external_id=ap.external_id '
                f'WHERE ap.document_id IN ({placeholders}) AND ap.related_external_id IS NULL '
                'ORDER BY ap.document_id,ap.external_id,ai.external_id', ids):
            by_id[item['document_id']]['items'].append({
                'description': item['description'], 'quantity': item['quantity'],
                'unit': None, 'amount': item['amount'], 'currency': item['currency']})
        for row in rows:
            if not row['items']:
                row['items_note'] = 'Keine Artikelpositionen erfasst.'
    return {'rows': rows, 'total': total, 'page': page,
            'pages': (total + _PAGE_SIZE - 1) // _PAGE_SIZE}
