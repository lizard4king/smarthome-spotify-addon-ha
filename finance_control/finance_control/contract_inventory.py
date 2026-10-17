"""Read-only inventory of confirmed contract sources and explicit decisions.

Missing terms remain missing: historical payments never create contract terms or
payment rules here. An open end date is allowed and is not a missing field.
"""
import json

from . import contracts, recurring

_PAGE_SIZE = 25
_BOOKING_LIMIT = 200
_LABELS = {
    'contract_terms': 'Vertragsangaben', 'valid_from': 'Vertragsbeginn',
    'phases': 'Vertragsphasen mit Betrag und Rhythmus',
    'payment_rule': 'Aktive bestätigte Zahlungsregel',
    'account_id': 'Zahlungskonto', 'currency': 'Zahlungswährung',
    'day_of_month': 'Bestätigter Zahlungstag',
}


def _bookings(store, document_id):
    where = (' FROM classification_document_links l '
             'JOIN transactions t USING(account_id,external_id) '
             "WHERE l.document_id=? AND l.allocation_type IN ('payment','evidence') "
             'AND NOT EXISTS (SELECT 1 FROM classification_audit a '
             "WHERE a.action='document_link_rejected' AND a.account_id=l.account_id "
             'AND a.external_id=l.external_id AND a.document_id=l.document_id)')
    total = store.db.execute('SELECT COUNT(*)' + where, (document_id,)).fetchone()[0]
    rows = store.db.execute(
        'SELECT l.account_id,l.external_id,t.date,t.amount,t.currency,'
        'l.allocated_amount,l.allocation_type,'
        "COALESCE((SELECT c.counterparty FROM transaction_context c WHERE c.account_id=l.account_id "
        "AND c.external_id=l.external_id),'') AS counterparty,"
        "COALESCE((SELECT c.description FROM transaction_context c WHERE c.account_id=l.account_id "
        "AND c.external_id=l.external_id),'') AS description" + where
        + ' ORDER BY t.date DESC,l.account_id,l.external_id LIMIT ?',
        (document_id, _BOOKING_LIMIT))
    return [dict(row) for row in rows], total


def view(store, data):
    """Return one bounded page, without forecasts, inference, or database writes."""
    if (not isinstance(data, dict) or set(data) - {'page'}
            or type(data.get('page', 0)) is not int
            or not 0 <= data.get('page', 0) <= 1_000_000):
        raise ValueError('invalid_contract_inventory_request')
    page = data.get('page', 0)
    # Reuse the contract eligibility policy, including proven-source exclusions.
    clauses = ["kind='contract'", "status='confirmed'"]
    params = []
    excluded = sorted(contracts._EXCLUDED_DOCUMENT_WARNINGS)
    if excluded:
        marks = ','.join('?' for _ in excluded)
        clauses.append('NOT EXISTS (SELECT 1 FROM json_each(classification_documents.warnings) w '
                       f'WHERE w.value IN ({marks}))')
        params.extend(excluded)
    where = ' WHERE ' + ' AND '.join(clauses)
    total = store.db.execute('SELECT COUNT(*) FROM classification_documents' + where, params).fetchone()[0]
    documents = store.db.execute('SELECT * FROM classification_documents' + where
                                 + ' ORDER BY id LIMIT ? OFFSET ?',
                                 [*params, _PAGE_SIZE, page * _PAGE_SIZE]).fetchall()
    result = []
    for document in documents:
        contract = contracts.load(store, {'document_id': document['id']})
        rules = [recurring._row(row) for row in store.db.execute(
            'SELECT * FROM recurring_rules WHERE document_id=? ORDER BY id', (document['id'],))]
        rule = rules[0] if len(rules) == 1 else None
        active = rule if rule and rule['active'] else None
        missing = []
        if not contract['configured']:
            missing.append('contract_terms')
        if contract['valid_from'] is None:
            missing.append('valid_from')
        if not contract['phases']:
            missing.append('phases')
        if active is None:
            missing.append('payment_rule')
        missing.extend(key for key in ('account_id', 'currency', 'day_of_month')
                       if active is None or active.get(key) is None)
        bookings, booking_total = _bookings(store, document['id'])
        result.append({
            'document_id': document['id'], 'document_revision': document['revision'],
            'title': document['title'], 'provider': document['vendor'],
            'source_reference': document['source_reference'], 'status': document['status'],
            'warnings': json.loads(document['warnings']), 'contract': contract,
            'rule': rule, 'rule_conflict': len(rules) > 1,
            'bookings': bookings, 'booking_total': booking_total,
            'bookings_truncated': booking_total > _BOOKING_LIMIT,
            'missing_fields': missing,
            'missing_field_labels': {key: _LABELS[key] for key in missing},
        })
    return {'contracts': result, 'page': page, 'page_size': _PAGE_SIZE,
            'total': total, 'pages': (total + _PAGE_SIZE - 1) // _PAGE_SIZE}
