"""Small, explicit decisions for inferred schedules and unmatched due payments.

Views never write. A decision recomputes its evidence under the write lock; the
browser cannot supply financial values or silently change another assignment.
"""
import hashlib
import json
from collections import defaultdict
from datetime import date, timedelta

from . import budget, contracts, recurring
from .classification import _audit, _now, _cash_withdrawal_row_evidence
from .core import money
from .recurring_detection import _party_key, _next_date, _REVERSAL, _SETTLEMENT
from .transfer_corrections import sql_transfer_predicate

SCOPE = {'as_of', 'until', 'account_id'}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def apply_rejections(store, candidates):
    """A declined series stays declined when another observation arrives."""
    rejected = {r[0] for r in store.db.execute(
        "SELECT proposal_id FROM payment_review_decisions WHERE kind='recurring' AND decision='reject'")}
    for candidate in candidates:
        if 'series:' + candidate['id'] in rejected and candidate['status'] not in {'covered', 'blocked'}:
            candidate['status'] = 'rejected'


def _proposals(store, scope):
    data = recurring.view(store, scope)
    decisions = {r['proposal_id']: dict(r) for r in store.db.execute('SELECT * FROM payment_review_decisions')}
    evidence = {(r['account_id'], r['external_id']): dict(r) for r in store.db.execute(
        "SELECT t.*,c.counterparty,c.description FROM active_transaction_versions t "
        'LEFT JOIN transaction_context c USING(account_id,external_id) WHERE t.date<=?', (scope['as_of'],))}
    members = {(r['account_id'], r['external_id']): dict(r)
               for r in store.db.execute('SELECT * FROM booking_contract_links')}
    result = []
    today = date.today().isoformat()
    for candidate in data['candidates']:
        # Conflicting/overdue/authoritative series require the detailed editor.
        # Two clean monthly observations may be proposed, never auto-projected.
        if candidate['status'] not in {'stable', 'proposed'}:
            continue
        if any(b['date'] > today for b in candidate['bookings']):
            continue
        if any((b['account_id'], b['external_id']) in members for b in candidate['bookings']):
            continue
        pid = 'series:' + candidate['id']
        payload = {'candidate': candidate, 'sources': [evidence[(b['account_id'], b['external_id'])]
                                                     for b in candidate['bookings']]}
        token = _digest(payload)
        if decisions.get(pid, {}).get('decision') == 'reject':
            continue
        result.append({'id': pid, 'token': token, 'kind': 'recurring',
                       'label': candidate['label'], 'amount': candidate['amount'],
                       'currency': candidate['currency'],
                       'forecast_included': candidate['status'] == 'stable',
                       'expected_date': _next_date(date.fromisoformat(candidate['bookings'][-1]['date']),
                                                   candidate['day_of_month'], candidate['interval_months']).isoformat(),
                       'reason': f"Regelmäßige Zahlung aus {len(candidate['bookings'])} Buchungen. Betrag und Termin geschätzt.",
                       'confidence': 'high' if candidate['status'] == 'stable' else 'medium',
                       '_payload': payload})
    price_events = None
    for schedule in data['schedules']:
        if (type(schedule['id']) is not int or not schedule['active'] or schedule['document_id'] is not None
                or schedule['valid_from'] > scope['as_of']
                or (schedule['valid_to'] is not None and schedule['valid_to'] <= scope['as_of'])):
            continue
        candidates = [c for c in data['candidates']
                      if c['account_id'] == schedule['account_id'] and c['currency'] == schedule['currency']
                      and c['interval_months'] == schedule['interval_months']
                      and c['status'] not in {'blocked', 'rejected'}
                      and not any(ref['type'] in {'paypal_credit', 'document'} for ref in c['covered_by'])
                      and len(c['bookings']) == 3 and not (set(c['warnings']) - {'end_date_unknown'})
                      and all(schedule['valid_from'] <= b['date'] <= today for b in c['bookings'])
                      and all((link := members.get((b['account_id'], b['external_id']))) is None
                              or (link['decision'] == 'confirmed' and link['rule_id'] == schedule['id'])
                              for b in c['bookings'])
                      and any(ref.get('id') == schedule['id'] and ref.get('type') in {'rule', 'booking_contract_rule'}
                              for ref in c['covered_by'])]
        # Several distinct same-vendor series must be resolved in contract details.
        if len(candidates) != 1:
            continue
        candidate = candidates[0]
        if money(candidate['amount']) == money(schedule['amount']):
            continue
        start = date.fromisoformat(scope['as_of'])
        if start == date.max:
            continue
        upper = min(date.max, start + timedelta(days=min(366, date.max.toordinal() - start.toordinal()))).isoformat()
        if price_events is None:
            # Display scope can end before an already paid future instalment.
            # Check the full price-search horizon before selecting an unpaid due.
            price_events = (data['events'] if scope['until'] == upper else recurring.view(
                store, {**scope, 'until': upper})['events'])
        settled = {e['expected_date'] for e in price_events if e['rule_id'] == schedule['id'] and e['status'] == 'booked'}
        end = min(value for value in (schedule['valid_to'], upper) if value is not None)
        effective = next((day for day in recurring._dates(schedule['valid_from'], end, schedule['day_of_month'],
                         schedule['interval_months'], (start + timedelta(days=1)).isoformat(), upper)
                         if day not in settled), None)
        if effective is None:
            continue
        explicitly_owned = all((link := members.get((b['account_id'], b['external_id']))) is not None
                               and link['decision'] == 'confirmed' and link['rule_id'] == schedule['id']
                               for b in candidate['bookings'])
        covered_rules = {ref['id'] for ref in candidate['covered_by']
                         if ref['type'] in {'rule', 'booking_contract_rule'}}
        if not explicitly_owned and any(
                other['id'] != schedule['id'] and other['id'] in covered_rules and other['active']
                and (other['valid_to'] is None or other['valid_to'] >= effective)
                and other['valid_from'] <= (schedule['valid_to'] or upper)
                for other in data['schedules']):
            continue
        pid = 'amount:' + _digest([schedule['id'], candidate['amount']])[:32]
        if decisions.get(pid, {}).get('decision') == 'reject':
            continue
        payload = {'candidate': candidate, 'schedule': schedule, 'effective': effective,
                   'sources': [evidence[(b['account_id'], b['external_id'])] for b in candidate['bookings']]}
        result.append({'id': pid, 'token': _digest(payload), 'kind': 'amount',
                       'label': schedule['label'], 'amount': candidate['amount'], 'previous_amount': schedule['amount'],
                       'currency': schedule['currency'], 'expected_date': effective,
                       'reason': 'Neuer Median aus den letzten drei Buchungen. Gilt nach Bestätigung nur für künftige offene Zahlungen.',
                       'confidence': 'medium', '_payload': payload})
    # A chosen booking is never offered to another contract. Rejection of one
    # proposed pair does not reject the booking for every other contract.
    document_links = defaultdict(set)
    for row in store.db.execute(
            "SELECT l.account_id,l.external_id,l.document_id FROM classification_document_links l "
            "JOIN classification_documents d ON d.id=l.document_id WHERE d.kind='contract' AND d.status='confirmed'"):
        document_links[(row['account_id'], row['external_id'])].add(row['document_id'])
    rejected_documents = {(r['account_id'], r['external_id'], r['document_id'])
                          for r in store.db.execute('SELECT * FROM rejected_document_links')}
    matched = {(b['account_id'], b['external_id']) for e in data['events']
               if e['status'] == 'booked' for b in e['matched_bookings']}
    lower_date = date.fromisoformat(scope['as_of'][:8] + '01')
    lower = max(date.min, lower_date - timedelta(days=min(14, lower_date.toordinal() - 1))).isoformat()
    bookings = [dict(r) for r in store.db.execute(
        "SELECT t.*,t.category AS source_category,o.category_id,COALESCE(c.counterparty,'') AS counterparty,COALESCE(c.description,'') AS description "
        'FROM active_transaction_versions t LEFT JOIN transaction_context c USING(account_id,external_id) '
        'LEFT JOIN classification_overrides o USING(account_id,external_id) '
        'WHERE t.date>=? AND t.date<=? '
        f'AND NOT COALESCE({sql_transfer_predicate(context_alias="c")},0)', (lower, scope['as_of']))]
    audits = defaultdict(list)
    for row in store.db.execute(
            'SELECT a.* FROM classification_audit a JOIN active_transactions t USING(account_id,external_id) '
            "WHERE t.date>=? AND t.date<=? AND a.action IN ('classification_saved','classification_auto_applied','classification_reset') "
            'ORDER BY a.id DESC', (lower, scope['as_of'])):
        audits[(row['account_id'], row['external_id'])].append(row)
    aliases = dict(store.db.execute('SELECT alias_id,canonical_id FROM category_aliases'))
    def canonical(value):
        if value is None:
            return None
        return aliases.get(value, value) if isinstance(value, str) else budget.canonical_category_id(store, value)
    reversed_dates = defaultdict(list)
    for booking in bookings:
        if _REVERSAL.search(booking['counterparty'] + ' ' + booking['description'] + ' ' + booking['category']):
            reversed_dates[(booking['account_id'], booking['currency'], _party_key(booking['counterparty']),
                            -money(booking['amount']))].append(date.fromisoformat(booking['date']))
    bookings = [b for b in bookings
                if not any(pattern.search(b['counterparty'] + ' ' + b['description'] + ' ' + b['category'])
                           for pattern in (contracts._EXCLUDED, _REVERSAL, _SETTLEMENT))
                and not any(abs((date.fromisoformat(b['date']) - reversed_on).days) <= 7
                            for reversed_on in reversed_dates[(b['account_id'], b['currency'],
                                                               _party_key(b['counterparty']), money(b['amount']))])
                and not _cash_withdrawal_row_evidence(b, audits[(b['account_id'], b['external_id'])], canonical)]
    schedules = {s['id']: s for s in data['schedules'] if type(s['id']) is int and s['active']}
    for event in data['events']:
        schedule = schedules.get(event['rule_id'])
        if (event['status'] not in {'unresolved', 'expected'} or schedule is None
                or schedule.get('missing_fields')
                or 'partial_payment_conflict' in event['warnings']):
            continue
        expected = money(event['expected_amount'])
        if not expected:
            continue
        ranked = []
        for booking in bookings:
            identity = (booking['account_id'], booking['external_id'])
            actual = money(booking['amount'])
            distance = abs((date.fromisoformat(booking['date']) - date.fromisoformat(event['expected_date'])).days)
            if (identity in members or identity in matched or booking['account_id'] != event['account_id']
                    or booking['currency'] != event['currency'] or booking['transfer_id']
                    or booking['date'] > today
                    or not actual or (actual > 0) != (expected > 0) or distance > 14):
                continue
            if booking['date'][:7] != event['expected_date'][:7] and distance > 7:
                continue
            if (document_links[identity] - {schedule['document_id']}
                    or (*identity, schedule['document_id']) in rejected_documents
                    or any(s['basis'] == 'confirmed_paypal_credit' and s['account_id'] == booking['account_id']
                           and s['currency'] == booking['currency'] and contracts._reference_present(
                               s['reference'], booking['counterparty'] + ' ' + booking['description'])
                           for s in data['schedules'])):
                continue
            if any(other['rule_id'] == event['rule_id'] and other['id'] != event['id']
                   and abs((date.fromisoformat(booking['date']) - date.fromisoformat(other['expected_date'])).days) <= distance
                   and (booking['date'][:7] == other['expected_date'][:7]
                        or abs((date.fromisoformat(booking['date']) - date.fromisoformat(other['expected_date'])).days) <= 7)
                   for other in data['events']):
                continue
            vendor = (recurring._counterparty_matches(schedule, booking)
                      or contracts._reference_present(schedule['reference'], booking['counterparty'] + ' ' + booking['description']))
            if not vendor and (actual != expected or distance > 7):
                continue
            pid = 'match:' + _digest([event['id'], *identity])[:32]
            payload = {'event': event, 'schedule': schedule, 'booking': booking}
            # A clock tick from expected to overdue is not new source evidence.
            # Keep a rejected pair rejected while all underlying facts agree.
            token = _digest({**payload, 'event': {k: event[k] for k in (
                'id', 'rule_id', 'document_id', 'label', 'expected_date', 'expected_amount', 'currency', 'account_id')}})
            prior = decisions.get(pid, {})
            if prior.get('decision') == 'reject' and prior.get('token') == token:
                continue
            proposal = {'id': pid, 'token': token, 'kind': 'match', 'label': event['label'],
                        'amount': event['expected_amount'], 'currency': event['currency'],
                        'expected_date': event['expected_date'],
                        'booking': {k: booking[k] for k in ('date', 'amount', 'counterparty')},
                        'reason': 'Anbieter und Termin passen; Betrag prüfen.' if vendor else 'Betrag und Termin passen; Anbieter prüfen.',
                        'confidence': 'high' if vendor else 'low', '_payload': payload}
            ranked.append((not vendor, distance, booking['external_id'], proposal))
        result.extend(item[-1] for item in sorted(ranked, key=lambda item: item[:3])[:3])
    return result


def view(store, scope):
    if not isinstance(scope, dict) or set(scope) != SCOPE:
        raise ValueError('invalid_payment_review_view')
    return {**scope, 'proposals': [{k: v for k, v in p.items() if not k.startswith('_')}
                                 for p in _proposals(store, scope)]}


def _link(store, identity, rule_id):
    # The fresh proposal has checked membership; never overwrite a decision.
    store.db.execute('INSERT INTO booking_contract_links(account_id,external_id,rule_id,decision,revision,updated_at) '
                     "VALUES (?,?,?,'confirmed',1,?)", (*identity, rule_id, _now()))
    _audit(store, 'booking_contract_confirmed', {'account_id': identity[0], 'external_id': identity[1],
           'rule_id': rule_id, 'decision': 'confirmed', 'revision': 1},
           account_id=identity[0], external_id=identity[1])


def decide(store, data):
    if (not isinstance(data, dict) or set(data) != SCOPE | {'id', 'token', 'decision', 'confirmed'}
            or data['confirmed'] is not True or type(data['decision']) is not str
            or data['decision'] not in {'accept', 'reject'}
            or type(data['id']) is not str or len(data['id']) > 128 or type(data['token']) is not str):
        raise ValueError('explicit_payment_review_confirmation_required')
    scope = {k: data[k] for k in SCOPE}
    store.db.execute('BEGIN IMMEDIATE')
    try:
        proposal = next((p for p in _proposals(store, scope) if p['id'] == data['id']), None)
        if proposal is None or proposal['token'] != data['token']:
            raise ValueError('stale_payment_review')
        payload = proposal['_payload']
        rule_id = None
        if data['decision'] == 'accept':
            if proposal['kind'] == 'match':
                booking, schedule = payload['booking'], payload['schedule']
                rule_id = schedule['id']
                _link(store, (booking['account_id'], booking['external_id']), rule_id)
            elif proposal['kind'] == 'amount':
                from . import booking_contracts
                candidate, schedule, effective = payload['candidate'], payload['schedule'], payload['effective']
                rows = candidate['bookings']
                derivation = booking_contracts.preview(store, {
                    'account_id': rows[-1]['account_id'], 'external_id': rows[-1]['external_id'], 'method': 'median',
                    'bookings': [{k: b[k] for k in ('account_id', 'external_id')} for b in rows]})
                if derivation['amount'] != candidate['amount']:
                    raise ValueError('stale_payment_review')
                rule = {k: schedule[k] for k in recurring._FIELDS - {'confirmed'}}
                rule['confirmed'] = True
                if schedule['valid_from'] <= scope['as_of']:
                    # Close the old price period; do not retroactively replace
                    # its amount or move its already assigned source bookings.
                    recurring._save_within_transaction(store, {**rule,
                        'valid_to': (date.fromisoformat(effective) - timedelta(days=1)).isoformat()})
                    rule.update(id=None, revision=0, valid_from=effective)
                rule['amount'] = derivation['amount']
                rule_id = recurring._save_within_transaction(store, rule, amount_derivation=derivation)['id']
            else:
                from . import booking_contracts
                candidate = payload['candidate']
                keys = [(b['account_id'], b['external_id']) for b in candidate['bookings']]
                # Explicit prior rejections and assignments stay authoritative.
                if any(store.db.execute('SELECT 1 FROM booking_contract_links WHERE account_id=? AND external_id=?', key).fetchone()
                       for key in keys):
                    raise ValueError('stale_payment_review')
                rule = {k: candidate[k] for k in ('label', 'account_id', 'currency', 'amount', 'valid_from',
                                                'valid_to', 'day_of_month', 'interval_months')}
                rule.update(id=None, revision=0, document_id=None, document_revision=None,
                            reference='', confirmed=True)
                derivation = booking_contracts.preview(store, {
                    'account_id': keys[-1][0], 'external_id': keys[-1][1], 'method': 'median',
                    'bookings': [{'account_id': aid, 'external_id': eid} for aid, eid in keys]})
                if derivation['amount'] != candidate['amount']:
                    raise ValueError('stale_payment_review')
                derivation['candidate_id'] = candidate['id']
                rule_id = recurring._save_within_transaction(store, rule, amount_derivation=derivation)['id']
                for key in keys:
                    _link(store, key, rule_id)
        store.db.execute('INSERT INTO payment_review_decisions VALUES (?,?,?,?,?,?) '
                         'ON CONFLICT(proposal_id) DO UPDATE SET token=excluded.token,decision=excluded.decision,'
                         'payload=excluded.payload,updated_at=excluded.updated_at',
                         (proposal['id'], proposal['kind'], proposal['token'], data['decision'], json.dumps(payload), _now()))
        _audit(store, 'payment_review_' + data['decision'],
               {'proposal_id': proposal['id'], 'kind': proposal['kind'], 'rule_id': rule_id, 'evidence': payload})
        store.db.commit()
    except Exception:
        store.db.rollback()
        raise
    return {'decision': data['decision'], 'id': data['id'], 'rule_id': rule_id}
