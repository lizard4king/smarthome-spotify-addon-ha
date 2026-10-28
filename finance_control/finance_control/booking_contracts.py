"""Explicit booking membership in planning rules; the actual ledger stays immutable."""
import calendar
import hashlib
import json
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from statistics import median

from . import contracts, recurring
from .classification import _audit, _now, _text, _document_link_rejected, _cash_withdrawal_evidence
from .core import money
from .transfer_corrections import sql_transfer_predicate
from .recurring_detection import _REVERSAL, _SETTLEMENT, _party_key

_IDENTITY = {'account_id', 'external_id'}
_DOCUMENT_FIELDS = ('id', 'kind', 'vendor', 'title', 'status')


def _local_party_key():
    """Share only pure string normalization within one dialog or preview."""
    normalized = {}

    def party_key(value):
        if not isinstance(value, str):
            return _party_key(value)
        if value not in normalized:
            normalized[value] = _party_key(value)
        return normalized[value]

    return party_key


def _estimate_source(store, identity, *, party_key=None):
    if party_key is None:
        party_key = _party_key
    row = _booking(store, identity)
    account = store.db.execute('SELECT kind,currency FROM accounts WHERE id=?', (row['account_id'],)).fetchone()
    text = ' '.join(str(row.get(k) or '') for k in ('counterparty', 'description', 'category'))
    if (row['is_transfer'] or not money(row['amount']) or row['date'] > date.today().isoformat() or account['kind'] == 'CASH'
            or account['currency'] != row['currency'] or contracts._EXCLUDED.search(text)
            or _REVERSAL.search(text) or _SETTLEMENT.search(text)
            or _cash_withdrawal_evidence(store, *identity)):
        raise ValueError('booking_not_suitable_for_estimate')
    for document in store.db.execute("SELECT d.* FROM classification_documents d JOIN classification_document_links l "
            "ON l.document_id=d.id WHERE l.account_id=? AND l.external_id=? AND d.kind='contract' AND d.status='confirmed'", identity):
        if not _document_link_rejected(store, *identity, document['id']) and contracts._load(store, document)['configured']:
            raise ValueError('contract_phase_amount_authoritative')
    # Do not retain an original payment cancelled by a nearby explicit reversal.
    for other in store.db.execute('SELECT t.*,c.counterparty,c.description FROM active_transactions t '
            'LEFT JOIN transaction_context c USING(account_id,external_id) '
            'WHERE t.account_id=? AND t.currency=? AND ABS(julianday(t.date)-julianday(?))<=7',
            (row['account_id'], row['currency'], row['date'])):
        if (party_key(other['counterparty'] or '') == party_key(row['counterparty'] or '')
                and money(other['amount']) == -money(row['amount'])
                and _REVERSAL.search(' '.join(str(other[k] or '') for k in ('counterparty', 'description', 'category')))):
            raise ValueError('booking_not_suitable_for_estimate')
    link = store.db.execute('SELECT rule_id,decision,revision FROM booking_contract_links '
                            'WHERE account_id=? AND external_id=?', identity).fetchone()
    public = {k: row[k] for k in ('account_id', 'external_id', 'date', 'amount', 'currency', 'counterparty', 'description')}
    public['amount'] = format(money(row['amount']), '.2f')
    public['membership'] = dict(link) if link else None
    public['fingerprint'] = hashlib.sha256(json.dumps(dict(row), sort_keys=True).encode()).hexdigest()
    return public


def _request_cache(store, cache):
    """Invalidate internal read caches on another store, edit or calendar day."""
    if cache is None:
        return None
    if not isinstance(cache, dict):
        raise ValueError('invalid_internal_preview_cache')
    stamp = (store.db.total_changes, store.db.execute('PRAGMA data_version').fetchone()[0],
             date.today().isoformat())
    if cache.get('_store') is not store or cache.get('_stamp') != stamp:
        cache.clear()
        cache.update(_store=store, _stamp=stamp)
    return cache


def preview(store, data, *, _context_cache=None):
    """Read-only, explicit amount-per-payment derivation; no cadence inference."""
    if not isinstance(data, dict) or set(data) != {'account_id', 'external_id', 'method', 'bookings'}:
        raise ValueError('invalid_amount_estimate')
    cache = _request_cache(store, _context_cache)
    party_key = _local_party_key()
    anchor = _estimate_source(store, _identity(data), party_key=party_key)
    if data['method'] not in {'median', 'mean', 'latest'} or not isinstance(data['bookings'], list) or not 1 <= len(data['bookings']) <= 100:
        raise ValueError('invalid_amount_estimate')
    sources = {}
    for key in data['bookings']:
        if not isinstance(key, dict) or set(key) != _IDENTITY:
            raise ValueError('invalid_amount_estimate')
        identity = _identity(key)
        source = _estimate_source(store, identity, party_key=party_key)
        if (source['account_id'] != anchor['account_id'] or source['currency'] != anchor['currency']
                or (money(source['amount']) > 0) != (money(anchor['amount']) > 0)):
            raise ValueError('estimate_account_currency_direction_mismatch')
        sources[identity] = source
    rows = sorted(sources.values(), key=lambda r: (r['date'], r['external_id']))
    # The dialog anchor receives membership on save even when its amount was
    # deliberately excluded from the calculation. Protect both sets of identities.
    protected = dict(sources)
    protected[(anchor['account_id'], anchor['external_id'])] = anchor
    protected_rows = list(protected.values())
    from .paypal_credit_forecast import forecast_schedules
    if cache is None:
        credit_schedules = forecast_schedules(store)
    else:
        if 'credit_schedules' not in cache:
            cache['credit_schedules'] = forecast_schedules(store)
        credit_schedules = cache['credit_schedules']
    for schedule in credit_schedules:
        if any(schedule['account_id'] == row['account_id'] and schedule['currency'] == row['currency']
                and contracts._reference_present(schedule['reference'], row['counterparty']+' '+row['description'])
                for row in protected_rows):
            raise ValueError('estimate_exact_payment_authoritative')
    _reject_authoritative_context(store, protected_rows, cache=cache)
    amounts = [money(r['amount']) for r in rows]
    if data['method'] == 'latest':
        latest = {money(r['amount']) for r in rows if r['date'] == rows[-1]['date']}
        if len(latest) != 1:
            raise ValueError('ambiguous_latest_estimate')
        amount = next(iter(latest))
    else:
        amount = median(amounts) if data['method'] == 'median' else sum(amounts, Decimal(0)) / len(amounts)
    amount = amount.quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
    if not amount:
        raise ValueError('zero_recurring_amount')
    return {'method': data['method'], 'amount': format(amount, '.2f'), 'currency': anchor['currency'],
            'rounding': 'ROUND_HALF_UP', 'amount_estimated': True, 'bookings': rows}


def _reject_authoritative_context(store, sources, *, cache=None):
    """Reuse existing matchers; a missing explicit link is not a new contract."""
    contextual = [dict(source) for source in sources]
    views = None if cache is None else cache.setdefault('recurring_views', {})
    attach_context(store, contextual, date.today().isoformat(), _view_cache=views)
    for source in contextual:
        context = source['contract_context']
        if (context.get('rule') or {}).get('document_id') is not None:
            raise ValueError('contract_phase_amount_authoritative')
    # A confirmed phase can also exist before an account/day setting is saved.
    # Use the contract matcher's provider, amount and period checks, requiring
    # its identifying reference here rather than blocking unrelated same-vendor rows.
    keys = {(r['account_id'], r['external_id']) for r in sources}
    if cache is None:
        # Preserve the original early rejection for one-off previews/decisions.
        protected = any(key in keys for key in _phase_source_keys(store))
    else:
        if 'phase_source_keys' not in cache:
            cache['phase_source_keys'] = set(_phase_source_keys(store))
        protected = bool(keys & cache['phase_source_keys'])
    if protected:
        raise ValueError('contract_phase_amount_authoritative')


def _phase_source_keys(store):
    for document, contract in contracts._active_contracts(store):
        suggestions, _ = contracts._matches(store, document)
        for candidate in suggestions:
            if contracts._reference_present(contract['reference'],
                    (candidate['counterparty'] or '')+' '+(candidate['description'] or '')):
                yield candidate['account_id'], candidate['external_id']


def _identity(data):
    return tuple(_text(data[key], key, 240) for key in ('account_id', 'external_id'))


def _booking(store, identity):
    row = store.db.execute(
        "SELECT t.*,COALESCE(c.counterparty,'') AS counterparty,COALESCE(c.description,'') AS description,"
        f'COALESCE({sql_transfer_predicate(context_alias="c")},0) AS is_transfer '
        'FROM active_transaction_versions t LEFT JOIN transaction_context c USING(account_id,external_id) '
        'WHERE t.account_id=? AND t.external_id=?', identity).fetchone()
    if row is None:
        raise ValueError('unknown_booking')
    return dict(row)


def _links(store):
    return {(r['account_id'], r['external_id']): dict(r)
            for r in store.db.execute('SELECT * FROM booking_contract_links')}


def attach_context(store, bookings, as_of, *, _view_cache=None):
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
        if _view_cache is None:
            result = recurring.view(store, {'as_of': cutoff, 'until': cutoff, 'account_id': ''})
        else:
            if cutoff not in _view_cache:
                _view_cache[cutoff] = recurring.view(store, {'as_of': cutoff, 'until': cutoff, 'account_id': ''})
            result = _view_cache[cutoff]
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
    candidates = []
    party_key = _local_party_key()
    for candidate in store.db.execute('SELECT account_id,external_id FROM active_transactions '
            'WHERE account_id=? AND currency=? ORDER BY date DESC,external_id DESC LIMIT 100',
            (booking['account_id'], booking['currency'])):
        try:
            source = _estimate_source(store, tuple(candidate), party_key=party_key)
        except ValueError:
            continue
        if (money(source['amount']) > 0) == (money(booking['amount']) > 0):
            candidates.append(source)
    return {'booking': public, **context, 'suggested_rules': suggestions, 'amount_candidates': candidates}


def _change(store, data, *, reject):
    expected = _IDENTITY | {'revision', 'confirmed'} | (set() if reject else {'rule'})
    if not isinstance(data, dict) or set(data) not in (expected, expected | ({'amount_estimate'} if not reject else set())) or data['confirmed'] is not True:
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
            estimate = data.get('amount_estimate')
            if estimate is not None:
                if (not isinstance(estimate, dict) or not isinstance(estimate.get('bookings'), list)
                        or any(not isinstance(b, dict) or not _IDENTITY <= b.keys() for b in estimate['bookings'])):
                    raise ValueError('invalid_amount_estimate')
                fresh = preview(store, {**dict(zip(('account_id', 'external_id'), identity)),
                    'method': estimate.get('method'), 'bookings': [
                        {k: b[k] for k in ('account_id', 'external_id')} for b in estimate.get('bookings', [])]})
                if fresh != estimate:
                    raise ValueError('stale_amount_estimate')
                if rule.get('document_id') is not None:
                    raise ValueError('contract_phase_amount_authoritative')
                if money(rule['amount']) != money(fresh['amount']):
                    raise ValueError('estimate_amount_mismatch')
                for source in fresh['bookings']:
                    member = source['membership']
                    if member and (member['decision'] == 'rejected' or member['rule_id'] != rule.get('id')):
                        raise ValueError('estimate_booking_already_decided')
                for existing_row in store.db.execute('SELECT * FROM recurring_rules'):
                    existing = recurring._row(existing_row)
                    if (existing['id'] == rule.get('id') or existing['account_id'] != booking['account_id']
                            or existing['currency'] != booking['currency'] or existing['document_id'] is not None
                            or (money(existing['amount']) > 0) != (money(booking['amount']) > 0)):
                        continue
                    if any(contracts._reference_present(existing['reference'], source['counterparty']+' '+source['description'])
                            or (not existing['reference'] and _party_key(existing['label']) == _party_key(source['counterparty']))
                            for source in fresh['bookings']):
                        raise ValueError('estimate_existing_rule_required')
                if rule.get('id') is not None:
                    old_row = store.db.execute('SELECT * FROM recurring_rules WHERE id=?', (rule['id'],)).fetchone()
                    if old_row is None:
                        raise ValueError('stale_recurring_revision')
                    old = recurring._row(old_row)
                    if any(rule[k] != old[k] for k in ('valid_from', 'valid_to')):
                        raise ValueError('estimate_preserves_contract_term')
            rule_id = recurring._save_within_transaction(store, rule, amount_derivation=estimate)['id']
            if estimate is not None:
                for source in estimate['bookings']:
                    source_id = (source['account_id'], source['external_id'])
                    if source_id == identity:
                        continue
                    store.db.execute('INSERT INTO booking_contract_links(account_id,external_id,rule_id,decision,revision,updated_at) '
                        "VALUES (?,?,?,'confirmed',1,?) ON CONFLICT(account_id,external_id) DO UPDATE SET "
                        "rule_id=excluded.rule_id,decision='confirmed',revision=revision+1,updated_at=excluded.updated_at",
                        (*source_id, rule_id, _now()))
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
