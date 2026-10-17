"""Bounded, explicit access to immutable booking revisions; no source manifests."""
import json

from .classification import _text

_CONTEXT = ('counterparty', 'description', 'category', 'source_category')
_DEPENDENCIES = {
    'transaction_context': _CONTEXT,
    'classification_overrides': ('category_id', 'confirmed', 'revision'),
    'classification_document_links': ('document_id', 'allocated_amount', 'allocation_type'),
    'booking_contract_links': ('rule_id', 'decision', 'revision', 'updated_at'),
    'transfer_correction_members': ('pair_id',),
    'transfer_single_legs': ('revision', 'reason', 'created_at'),
    'bonsy_cash_allocations': ('entry_id', 'allocated_amount', 'confirmed_at'),
    'rejected_document_links': ('document_id', 'revision', 'actor', 'changed_at'),
    'classification_model_reviews': ('category_id', 'confidence', 'rationale', 'model', 'status', 'reviewed_at'),
    'classification_model_rejections': ('category_id',),
    'payment_mail_events': ('id', 'provider', 'event_status', 'event_date', 'amount', 'currency', 'match_status'),
}
_TRANSACTION = ('date', 'amount', 'currency', 'category', 'import_id', 'revision')


def _request(data, *, identity=True):
    required = {'account_id', 'external_id'} if identity else {'account_id'}
    if not isinstance(data, dict) or not required <= set(data) or set(data) - required - {'limit', 'offset'}:
        raise ValueError('invalid_booking_history_request')
    key = tuple(_text(data[name], name, 240) for name in sorted(required))
    limit, offset = data.get('limit', 50), data.get('offset', 0)
    if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 100000:
        raise ValueError('invalid_booking_history_bounds')
    return key, limit, offset


def _fields(value, fields):
    return {key: value[key] for key in fields
            if key in value and isinstance(value[key], (str, int, float, bool, type(None)))} if isinstance(value, dict) else {}


def _previous(value):
    if not isinstance(value, dict):
        return {}
    dependencies = value.get('dependencies', {})
    context = _fields(value.get('context'), _CONTEXT)
    if not context and isinstance(dependencies, dict):
        contexts = dependencies.get('transaction_context', [])
        if isinstance(contexts, list) and contexts:
            context = _fields(contexts[0], _CONTEXT)
    return {'transaction': _fields(value.get('transaction'), _TRANSACTION),
            'context': context,
            'dependencies': {table: [_fields(row, fields) for row in rows[:100]]
                             for table, fields in _DEPENDENCIES.items()
                             if isinstance(dependencies, dict)
                             and isinstance((rows := dependencies.get(table)), list) and rows}}


def _parse(value):
    try:
        return json.loads(value) if value else {}
    except (ValueError, TypeError):
        return {}


def _archive_status(value):
    if value is None:
        return 'missing'
    try:
        return 'available' if isinstance(json.loads(value), dict) else 'invalid'
    except (ValueError, TypeError):
        return 'invalid'


def view(store, data):
    identity, limit, offset = _request(data)
    rows = store.db.execute(
        'SELECT t.*,i.imported_at,e.previous AS archived_previous '
        'FROM transactions t JOIN imports i ON i.id=t.import_id '
        'LEFT JOIN finanzguru_snapshot_events e ON e.id=t.voided_by_event '
        'WHERE t.account_id=? AND t.external_id=? ORDER BY t.revision LIMIT ? OFFSET ?',
        (*identity, limit + 1, offset)).fetchall()
    versions = []
    for row in rows[:limit]:
        archived = _previous(_parse(row['archived_previous']))
        versions.append({**_fields(dict(row), _TRANSACTION), 'source_category': row['category'],
                         'imported_at': row['imported_at'],
                         'status': row['void_reason'] or 'active', 'voided_at': row['voided_at'],
                         'voided_by_event': row['voided_by_event'], 'actor': row['voided_by'],
                         'archived_data_status': _archive_status(row['archived_previous']),
                         'context': archived.get('context', {}),
                         'dependencies': archived.get('dependencies', {})})
    events = store.db.execute(
        'SELECT id,occurred_at,action,previous,current FROM finanzguru_snapshot_events '
        'WHERE account_id=? AND external_id=? ORDER BY id LIMIT ? OFFSET ?',
        (*identity, limit + 1, offset)).fetchall()
    return {'account_id': identity[0], 'external_id': identity[1], 'versions': versions,
            'events': [{'id': row['id'], 'occurred_at': row['occurred_at'], 'action': row['action'],
                        'archived_data_status': _archive_status(row['previous']),
                        'previous': _previous(_parse(row['previous'])),
                        'current': _fields(_parse(row['current']), _TRANSACTION + _CONTEXT)}
                       for row in events[:limit]],
            'has_more': len(rows) > limit or len(events) > limit,
            'offset': offset, 'limit': limit}


def inactive(store, data):
    """Removed identities are discoverable only through this explicit account lookup."""
    identity, limit, offset = _request(data, identity=False)
    rows = store.db.execute(
        "SELECT t.external_id,t.date,t.amount,t.currency,t.revision,t.voided_at FROM transactions t "
        "WHERE t.account_id=? AND t.void_reason='voided' AND NOT EXISTS "
        '(SELECT 1 FROM transactions newer WHERE newer.account_id=t.account_id '
        'AND newer.external_id=t.external_id AND newer.revision>t.revision) '
        'ORDER BY t.voided_at DESC,t.external_id LIMIT ? OFFSET ?',
        (*identity, limit + 1, offset)).fetchall()
    return {'account_id': identity[0], 'bookings': [dict(row) for row in rows[:limit]],
            'has_more': len(rows) > limit, 'offset': offset, 'limit': limit}
