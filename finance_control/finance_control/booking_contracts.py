"""Explicit booking membership in planning rules; the actual ledger stays immutable."""
import calendar

from . import contracts, recurring
from .classification import _audit, _now, _text, _document_link_rejected
from .core import money
from .transfer_corrections import sql_transfer_predicate

_IDENTITY = {'account_id', 'external_id'}
_DOCUMENT_FIELDS = ('id', 'kind', 'vendor', 'title', 'status')


def _identity(data):
    return tuple(_text(data[key], key, 240) for key in ('account_id', 'external_id'))


def _booking(store, identity):
    row = store.db.execute(
        "SELECT t.*,COALESCE(c.counterparty,'') AS counterparty,COALESCE(c.description,'') AS description,"
        f'COALESCE({sql_transfer_predicate(context_alias="c")},0) AS is_transfer '
        'FROM transactions t LEFT JOIN transaction_context c USING(account_id,external_id) '
        'WHERE t.account_id=? AND t.external_id=?', identity).fetchone()
    if row is None:
        raise ValueError('unknown_booking')
    return dict(row)


def _links(store):
    return {(r['account_id'], r['external_id']): dict(r)
            for r in store.db.execute('SELECT * FROM booking_contract_links')}


def attach_context(store, bookings, as_of):
    """Attach public, path-free context in bounded batches, never one view per row."""
    if not bookings:
        return
    identities = {(b['account_id'], b['external_id']) for b in bookings}
    links = _links(store)
    rules = {r['id']: recurring._row(r) for r in store.db.execute('SELECT * FROM recurring_rules')}
    documents = {r['id']: {k: r[k] for k in _DOCUMENT_FIELDS}
                 for r in store.db.execute('SELECT id,kind,vendor,title,status FROM classification_documents')}
    linked_documents = {}
    for row in store.db.execute('SELECT account_id,external_id,document_id FROM classification_document_links'):
        key = (row['account_id'], row['external_id'])
        if key in identities and not _document_link_rejected(store, *key, row['document_id']):
            linked_documents.setdefault(key, set()).add(row['document_id'])
    matches, ambiguous, document_schedules = {}, set(), {}
    # Dashboard rows share a month. Single-booking views use their own month.
    months = {b['date'][:7] for b in bookings if b['date'] <= as_of}
    for month in sorted(months):
        last = f'{month}-{calendar.monthrange(int(month[:4]), int(month[5:]))[1]:02d}'
        cutoff = min(last, as_of)
        result = recurring.view(store, {'as_of': cutoff, 'until': cutoff, 'account_id': ''})
        document_schedules.update({r['document_id']: r for r in result['schedules'] if r['document_id']})
        for event in result['events']:
            for booking in event['matched_bookings']:
                key = (booking['account_id'], booking['external_id'])
                if key in identities:
                    matches.setdefault(key, []).append(event)
        # Ambiguous matching is returned with identities for this read-only context.
        for key in result.get('ambiguous_bookings', []):
            ambiguous.add((key['account_id'], key['external_id']))
    for booking in bookings:
        key = (booking['account_id'], booking['external_id'])
        link = links.get(key)
        docs = linked_documents.get(key, set()).copy()
        rule = rules.get(link['rule_id']) if link else None
        if rule and rule['document_id']:
            docs.add(rule['document_id'])
        events = matches.get(key, [])
        decision = link['decision'] if link else 'none'
        if not link:
            if key in ambiguous or len(events) > 1:
                decision = 'ambiguous'
            elif events:
                decision = 'matched'
                rule = rules.get(events[0]['rule_id'])
                if events[0]['document_id']:
                    docs.add(events[0]['document_id'])
            confirmed_docs = [d for d in docs if documents.get(d, {}).get('kind') == 'contract'
                              and documents[d]['status'] == 'confirmed']
            if decision == 'none' and confirmed_docs:
                candidates = [r for r in rules.values() if r['document_id'] in confirmed_docs
                              and r['account_id'] == booking['account_id'] and r['currency'] == booking['currency']]
                if len(confirmed_docs) == 1 and len(candidates) <= 1:
                    decision, rule = 'matched', next(iter(candidates), None)
                    if rule is None:
                        schedule = document_schedules.get(confirmed_docs[0])
                        if schedule and not schedule.get('requires_contract_setup'):
                            rule = {**schedule, 'account_id': booking['account_id'], 'currency': booking['currency'],
                                    'day_of_month': schedule['day_of_month'] or int(booking['date'][-2:]),
                                    'interval_months': schedule['interval_months'] or (schedule['phases'][0]['interval_months'] if schedule['phases'] else 1)}
                else:
                    decision = 'ambiguous'
        if rule and rule['document_id'] in document_schedules:
            schedule = document_schedules[rule['document_id']]
            rule = {**rule, **{k: schedule[k] for k in ('phases', 'document_revision', 'valid_from', 'valid_to')}}
        booking['contract_context'] = {'decision': decision, 'revision': link['revision'] if link else 0,
            'rule': rule, 'documents': [documents[d] for d in sorted(docs) if d in documents]}


def view(store, data):
    if not isinstance(data, dict) or set(data) != _IDENTITY:
        raise ValueError('invalid_booking_contract_view')
    booking = _booking(store, _identity(data))
    public = {k: booking[k] for k in ('account_id', 'external_id', 'date', 'amount', 'currency', 'counterparty', 'description')}
    public['amount'] = format(money(public['amount']), '.2f')
    attach_context(store, [public], booking['date'])
    context = public.pop('contract_context')
    suggestions = [recurring._row(r) for r in store.db.execute('SELECT * FROM recurring_rules WHERE active=1 ORDER BY id')]
    suggestions = [r for r in suggestions if r['account_id'] == booking['account_id'] and r['currency'] == booking['currency']]
    if context['rule'] and context['rule']['document_id']:
        suggestions = [r for r in suggestions if r['id'] != context['rule']['id']]
        suggestions.insert(0, context['rule'])
    return {'booking': public, **context, 'suggested_rules': suggestions}


def _change(store, data, *, reject):
    expected = _IDENTITY | {'revision', 'confirmed'} | (set() if reject else {'rule'})
    if not isinstance(data, dict) or set(data) != expected or data['confirmed'] is not True:
        raise ValueError('explicit_booking_contract_confirmation_required')
    identity = _identity(data)
    revision = contracts._revision(data['revision'])
    store.db.execute('BEGIN IMMEDIATE')
    try:
        booking = _booking(store, identity)
        if booking['is_transfer'] or not money(booking['amount']):
            raise ValueError('booking_not_suitable_for_contract')
        previous = store.db.execute('SELECT * FROM booking_contract_links WHERE account_id=? AND external_id=?', identity).fetchone()
        if revision != (previous['revision'] if previous else 0):
            raise ValueError('stale_booking_contract_revision')
        rule_id = previous['rule_id'] if previous else None
        if not reject:
            rule = data['rule']
            if not isinstance(rule, dict) or rule.get('account_id') != booking['account_id'] or rule.get('currency') != booking['currency']:
                raise ValueError('booking_contract_account_currency_mismatch')
            if rule.get('id') is not None:
                existing = store.db.execute('SELECT * FROM recurring_rules WHERE id=?', (rule['id'],)).fetchone()
                if existing:
                    selected = recurring._row(existing)
                    if (selected['account_id'] != booking['account_id']
                            or selected['currency'] != booking['currency']):
                        raise ValueError('booking_contract_account_currency_mismatch')
            authoritative = set()
            for doc in store.db.execute(
                    "SELECT d.* FROM classification_documents d JOIN classification_document_links l ON l.document_id=d.id "
                    "WHERE l.account_id=? AND l.external_id=? AND d.kind='contract' AND d.status='confirmed'", identity):
                if not _document_link_rejected(store, *identity, doc['id']) and contracts._load(store, doc)['configured']:
                    authoritative.add(doc['id'])
            if previous and previous['rule_id']:
                old = store.db.execute('SELECT * FROM recurring_rules WHERE id=?', (previous['rule_id'],)).fetchone()
                if old and old['document_id']:
                    authoritative.add(old['document_id'])
            if authoritative and (len(authoritative) != 1 or rule.get('document_id') not in authoritative):
                raise ValueError('booking_contract_document_authoritative')
            rule_id = recurring._save_within_transaction(store, rule)['id']
        decision = 'rejected' if reject else 'confirmed'
        store.db.execute('INSERT INTO booking_contract_links(account_id,external_id,rule_id,decision,revision,updated_at) '
                         'VALUES (?,?,?,?,?,?) ON CONFLICT(account_id,external_id) DO UPDATE SET '
                         'rule_id=excluded.rule_id,decision=excluded.decision,revision=excluded.revision,updated_at=excluded.updated_at',
                         (*identity, rule_id, decision, revision+1, _now()))
        _audit(store, 'booking_contract_'+decision, {'account_id': identity[0], 'external_id': identity[1],
               'rule_id': rule_id, 'decision': decision, 'revision': revision+1}, previous=dict(previous) if previous else None)
        store.db.commit()
    except Exception:
        store.db.rollback()
        raise
    return view(store, dict(zip(('account_id', 'external_id'), identity)))


def save(store, data):
    return _change(store, data, reject=False)


def reject(store, data):
    return _change(store, data, reject=True)
