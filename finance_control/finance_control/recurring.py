"""Dated, explicitly confirmed recurring payments. Views never mutate the ledger.

Money is signed decimal text; contract phase prices remain authoritative expenses.
Historical evidence estimates a day only after three distinct cadence-consistent
linked payments. Manual rules need an identifying reference for automatic matching.
A projection is conditional on recorded balance and imported ledger completeness;
no budget is converted into a second payment and no transfer is inferred.
"""
import calendar
from bisect import bisect_left, bisect_right
import json
from datetime import date, timedelta
from decimal import Decimal
from statistics import median

from . import contracts, recurring_detection
from .classification import _audit, _filter_date, _now, _text, _document_link_rejected
from .paypal_date_basis import transaction_date_accounts
from .core import money, parse_money_input
from .current_balances import latest_by_account
from .transfer_corrections import sql_transfer_predicate

_FIELDS = {'id', 'revision', 'document_id', 'document_revision', 'label', 'account_id',
           'currency', 'amount', 'valid_from', 'valid_to', 'day_of_month',
           'interval_months', 'reference', 'confirmed'}


def _row(row):
    result = json.loads(row['payload'])
    return {**result, 'id': row['id'], 'revision': row['revision'],
            'active': bool(row['active'])}


def save(store, data):
    """Confirm a manual schedule or document-bound account/day setting.

    id=None/revision=0 creates; later saves require the current rule revision.
    Document-bound rules require current document_revision and amount=None;
    their contract's phases control amounts, cadence and inclusive validity.
    """
    store.db.execute('BEGIN IMMEDIATE')
    try:
        result = _save_within_transaction(store, data)
        store.db.commit()
        return result
    except Exception:
        store.db.rollback()
        raise


def _save_within_transaction(store, data):
    """Validate/save inside the caller's atomic transaction; never commit here."""
    if not isinstance(data, dict) or set(data) != _FIELDS or data['confirmed'] is not True:
        raise ValueError('explicit_recurring_confirmation_required')
    rid, revision = data['id'], contracts._revision(data['revision'])
    if rid is not None and (type(rid) is not int or rid < 1):
        raise ValueError('invalid_recurring_id')
    if type(data['day_of_month']) is not int or not 1 <= data['day_of_month'] <= 31:
        raise ValueError('invalid_day_of_month')
    if type(data['interval_months']) is not int or not 1 <= data['interval_months'] <= 120:
        raise ValueError('invalid_interval_months')
    start, end = contracts._date_range(data['valid_from'], data['valid_to'])
    account = store.db.execute('SELECT * FROM accounts WHERE id=?', (data['account_id'],)).fetchone()
    if account is None or data['currency'] != account['currency']:
        raise ValueError('invalid_recurring_account_currency')
    payload = {key: data[key] for key in _FIELDS - {'id', 'revision', 'confirmed'}}
    payload.update(label=_text(data['label'], 'label', 160),
                   reference=_text(data['reference'], 'reference', 240, empty=True),
                   valid_from=start, valid_to=end)
    if data['document_id'] is None:
        if data['document_revision'] is not None:
            raise ValueError('invalid_document_revision')
        amount = parse_money_input(data['amount'])
        if not amount:
            raise ValueError('zero_recurring_amount')
        payload['amount'] = format(amount, '.2f')
    elif data['amount'] is not None:
        raise ValueError('contract_phase_amount_authoritative')
    previous = None
    if rid is not None:
        previous_row = store.db.execute('SELECT * FROM recurring_rules WHERE id=?', (rid,)).fetchone()
        if previous_row is None or previous_row['revision'] != revision:
            raise ValueError('stale_recurring_revision')
        previous = _row(previous_row)
        if previous['document_id'] != data['document_id']:
            raise ValueError('recurring_document_immutable')
    elif revision != 0:
        raise ValueError('stale_recurring_revision')
    if data['document_id'] is not None:
        document = contracts._document(store, data['document_id'])
        if document['status'] != 'confirmed' or not contracts._load(store, document)['configured']:
            raise ValueError('confirmed_contract_required')
        contract = contracts._load(store, document)
        if data['currency'] != 'EUR':
            raise ValueError('contract_phase_currency_eur_required')
        if start < contract['valid_from'] or (contract['valid_to'] is not None and (end is None or end > contract['valid_to'])):
            raise ValueError('recurring_setting_outside_contract')
        if document['revision'] != contracts._revision(data['document_revision']):
            raise ValueError('stale_document_revision')
        if set(json.loads(document['warnings'])) & contracts._EXCLUDED_DOCUMENT_WARNINGS:
            raise ValueError('contract_document_not_eligible')
    if data['document_id'] is None:
        keys = ('account_id', 'currency', 'amount', 'valid_from', 'valid_to', 'day_of_month', 'interval_months', 'reference')
        for other in store.db.execute('SELECT * FROM recurring_rules WHERE active=1 AND document_id IS NULL'):
            if other['id'] != rid and all(json.loads(other['payload'])[k] == payload[k] for k in keys):
                raise ValueError('duplicate_recurring_schedule')
    if data['document_id'] is not None and store.db.execute(
            'SELECT 1 FROM recurring_rules WHERE document_id=? AND (? IS NULL OR id!=?)',
            (data['document_id'], rid, rid)).fetchone():
        raise ValueError('duplicate_recurring_schedule')
    now = _now()
    if rid is None:
        rid = store.db.execute(
            'INSERT INTO recurring_rules(revision,document_id,payload,active,created_at,updated_at) '
            'VALUES (1,?,?,1,?,?)', (data['document_id'], json.dumps(payload), now, now)).lastrowid
    else:
        store.db.execute('UPDATE recurring_rules SET revision=revision+1,payload=?,active=1,updated_at=? '
                         'WHERE id=?', (json.dumps(payload), now, rid))
    result = _row(store.db.execute('SELECT * FROM recurring_rules WHERE id=?', (rid,)).fetchone())
    _audit(store, 'recurring_saved', result, previous=previous)
    return result


def disable(store, data):
    if not isinstance(data, dict) or set(data) != {'id', 'revision', 'confirmed'} or data['confirmed'] is not True:
        raise ValueError('explicit_recurring_confirmation_required')
    if type(data['id']) is not int or data['id'] < 1:
        raise ValueError('invalid_recurring_id')
    revision = contracts._revision(data['revision'])
    store.db.execute('BEGIN IMMEDIATE')
    try:
        row = store.db.execute('SELECT * FROM recurring_rules WHERE id=?', (data['id'],)).fetchone()
        if row is None or row['revision'] != revision:
            raise ValueError('stale_recurring_revision')
        previous = _row(row)
        store.db.execute('UPDATE recurring_rules SET active=0,revision=revision+1,updated_at=? WHERE id=?',
                         (_now(), data['id']))
        result = _row(store.db.execute('SELECT * FROM recurring_rules WHERE id=?', (data['id'],)).fetchone())
        _audit(store, 'recurring_disabled', result, previous=previous)
        store.db.commit()
        return result
    except Exception:
        store.db.rollback()
        raise


def _dates(start, end, day, interval, lower, upper):
    anchor = date.fromisoformat(start)
    first = date.fromisoformat(lower)
    offset = max(0, (first.year-anchor.year)*12 + first.month-anchor.month)
    offset -= offset % interval
    while True:
        year, month0 = divmod(anchor.year*12 + anchor.month-1 + offset, 12)
        if year > 9999:
            break
        value = date(year, month0+1, min(day, calendar.monthrange(year, month0+1)[1])).isoformat()
        if value > upper or (end and value > end):
            break
        if value >= start and value >= lower:
            yield value
        offset += interval


def _linked(store, document_id, cutoff, *, full_only=True, is_rejected):
    return [dict(row) for row in store.db.execute(
        'SELECT t.*,l.allocated_amount,l.allocation_type FROM active_transactions t '
        'LEFT JOIN transaction_context c USING(account_id,external_id) '
        'JOIN classification_document_links l USING(account_id,external_id) '
        'JOIN accounts a ON a.id=t.account_id AND a.currency=t.currency '
        "WHERE l.document_id=? AND l.allocation_type IN ('payment','evidence') AND t.date<=? "
        f'AND NOT COALESCE({sql_transfer_predicate(context_alias="c")},0) '
        'ORDER BY t.date,t.external_id', (document_id, cutoff))
        if money(row['amount']) < 0 and (not full_only or money(row['allocated_amount']) == -money(row['amount']))
        and not is_rejected(row['account_id'], row['external_id'], document_id)]


def _unconfigured_schedule(document):
    """Expose missing contract terms without inferring payments from the document."""
    return {'id': None, 'revision': 0, 'document_id': document['id'],
            'document_revision': document['revision'], 'label': document['vendor'],
            'account_id': None, 'currency': None, 'amount': None,
            'valid_from': None, 'valid_to': None, 'day_of_month': None,
            'interval_months': None, 'reference': '', 'active': True,
            'estimated': True, 'amount_estimated': False, 'date_estimated': False,
            'basis': 'unconfigured_document', 'phases': [],
            'requires_contract_setup': True,
            'missing_fields': ['contract_terms', 'account_id', 'currency', 'day_of_month', 'phases']}


def _doc_schedule(document, contract, setting, evidence):
    if contract['phases']:
        evidence = [r for r in evidence if (phase := contracts._phase_on(contract, r['date'])) is not None
                    and money(phase['amount']) == -money(r['amount'])]
    recent = evidence[-3:]
    amount_estimated = False
    if not contract['phases'] and len(recent) == 3:
        dates = [date.fromisoformat(r['date']) for r in recent]
        monthly = all((b.year-a.year)*12+b.month-a.month == 1 for a,b in zip(dates, dates[1:]))
        if monthly and len({(r['account_id'], r['currency']) for r in evidence}) == 1:
            contract = {**contract, 'phases': [{'valid_from': contract['valid_from'], 'valid_to': contract['valid_to'],
                       'amount': format(median([-money(r['amount']) for r in recent]), '.2f'), 'interval_months': 1}]}
            amount_estimated = True
    # Never infer account/currency from ambiguous evidence or duplicate months.
    identities = {(r['account_id'], r['currency']) for r in evidence}
    distinct = len({r['date'][:7] for r in recent}) == 3
    cadence_ok = len(recent) == 3 and distinct and all(
        contracts._phase_on(contract, r['date']) is not None for r in recent)
    if cadence_ok:
        for left, right in zip(recent, recent[1:]):
            a, b = date.fromisoformat(left['date']), date.fromisoformat(right['date'])
            cadence_ok = cadence_ok and (b.year-a.year)*12+b.month-a.month == contracts._phase_on(contract, right['date'])['interval_months']
    inferred = len(identities) == 1 and cadence_ok
    account_id, currency = next(iter(identities)) if inferred else (None, None)
    result = {'id': None, 'revision': 0, 'document_id': document['id'],
              'document_revision': document['revision'], 'label': document['vendor'],
              'account_id': account_id, 'currency': currency if currency == 'EUR' else None, 'amount': None,
              'valid_from': contract['valid_from'], 'valid_to': contract['valid_to'],
              'day_of_month': int(median([date.fromisoformat(r['date']).day for r in recent])) if inferred else None,
              'interval_months': None, 'reference': contract['reference'], 'active': True,
              'estimated': True, 'amount_estimated': amount_estimated, 'date_estimated': True,
              'basis': 'linked_history', 'phases': contract['phases']}
    if setting:
        result.update({key: setting[key] for key in ('id', 'revision', 'active', 'label', 'account_id', 'currency', 'day_of_month', 'valid_from', 'valid_to')})
        result['valid_from'] = max(result['valid_from'], contract['valid_from'])
        result['valid_to'] = min((v for v in (result['valid_to'], contract['valid_to']) if v is not None), default=None)
        result.update(estimated=amount_estimated, date_estimated=False, basis='confirmed_document_setting')
    result['missing_fields'] = [key for key in ('account_id', 'currency', 'day_of_month') if result[key] is None]
    if not result['phases']:
        result['missing_fields'].append('phases')
    return result


def _credit_events(schedule, bookings, cutoff, lower, upper, membership):
    """Exact source dates and amounts; a bank match also needs its reference.

    Seven days is a collection-date tolerance, never a claim that two equal
    amounts belong together. The common claim resolver below rejects ambiguity.
    """
    events = []
    for payment in schedule['exact_payments']:
        expected = payment['date']
        if not lower <= expected <= upper:
            continue
        matches = [b for b in bookings if b['account_id'] == schedule['account_id']
                      and b['currency'] == schedule['currency'] and not b['transfer_id']
                      and money(b['amount']) < 0
                      and abs((date.fromisoformat(b['date'])-date.fromisoformat(expected)).days) <= 7
                      and contracts._reference_present(schedule['reference'], b['counterparty']+' '+b['description'])]
        # An explicit existing decision owns the booking. Keep the loan open
        # rather than stealing that actual or debiting it again in the future.
        blocked = [b for b in matches if (b['account_id'], b['external_id']) in membership]
        candidates = [b for b in matches if (b['account_id'], b['external_id']) not in membership
                      and money(b['amount']) == money(payment['amount'])]
        warnings = ['paypal_credit_booking_assignment_conflict'] if blocked else []
        if any(money(b['amount']) != money(payment['amount']) for b in matches):
            # A reference-identical early debit contradicts the saved regular
            # rate. Do not charge the full virtual rate again or invent a remainder.
            warnings.append('partial_payment_conflict')
        events.append({'id': f"{schedule['id']}:{expected}", 'rule_id': schedule['id'],
                       'document_id': None, 'label': schedule['label'], 'expected_date': expected,
                       'observed_date': None, 'amount': payment['amount'],
                       'expected_amount': payment['amount'], 'observed_amount': None,
                       'currency': schedule['currency'], 'account_id': schedule['account_id'],
                       'status': 'expected' if expected > cutoff else 'unresolved',
                       'estimated': False, 'date_estimated': False, 'amount_estimated': False,
                       'basis': schedule['basis'], 'matched_bookings': [],
                       'warnings': warnings,
                       '_reference': schedule['reference'], '_candidates': candidates, '_linked_candidates': []})
    return events


def _credit_collisions(events):
    """Do not add a second virtual debit for a possibly competing payment row.

    A same-account rate with no distinct identifying reference near another due
    date is a warning, not a match. Different identified plans remain separate. Two
    distinct booked payments are proof of separate movements; otherwise the
    unbooked rows stay unresolved. This intentionally prefers a visible gap in
    a conditional forecast over silently charging a loan twice.
    """
    grouped = {}
    for event in events:
        grouped.setdefault((event['account_id'], event['currency']), []).append(event)
    for group in grouped.values():
        group.sort(key=lambda event: event['expected_date'])
        days = [date.fromisoformat(e['expected_date']).toordinal() for e in group]
        for credit in group:
            if credit['basis'] != 'confirmed_paypal_credit':
                continue
            day = date.fromisoformat(credit['expected_date']).toordinal()
            due = date.fromisoformat(credit['expected_date'])
            month_first = due.replace(day=1).toordinal()
            month_last = due.replace(day=calendar.monthrange(due.year, due.month)[1]).toordinal()
            # A reference-identical monthly rule can use a different guessed
            # collection day. It must not double the exact source-plan debit.
            for other in group[bisect_left(days, min(day-7, month_first)):
                               bisect_right(days, max(day+7, month_last))]:
                if credit['rule_id'] == other['rule_id'] and credit['document_id'] == other['document_id']:
                    continue
                if money(other['amount']) >= 0:
                    continue
                left, right = credit['_reference'].casefold(), other['_reference'].casefold()
                same_reference = (contracts._reference_present(left, right)
                                  or contracts._reference_present(right, left))
                if not same_reference and contracts._reference_present(right, right):
                    continue
                if not same_reference and abs(date.fromisoformat(other['expected_date']).toordinal()-day) > 7:
                    continue
                if money(credit['expected_amount']) != money(other['expected_amount']) and not same_reference:
                    continue
                if credit['status'] == other['status'] == 'booked':
                    continue
                for event in (credit, other):
                    if event['status'] != 'booked':
                        event['status'] = 'unresolved'
                        if 'competing_payment_schedule' not in event['warnings']:
                            event['warnings'].append('competing_payment_schedule')


def view(store, data):
    """Read-only JSON forecast. Account points are conditional ledger projections.

    Events cover the as-of month through until, inclusive. Past unmatched events
    are unresolved and never silently carried forward as another debit. Future
    ledger rows neither train inference nor become booked actuals before cutoff.
    An identifying booking can match within the expected calendar month or up
    to seven days across a month boundary. This is a date-proximity assumption,
    not proof of the provider's collection date. Within one schedule the unique
    closest due date wins; competing schedules or equal distances stay open.
    """
    if not isinstance(data, dict) or set(data) != {'as_of', 'until', 'account_id'}:
        raise ValueError('invalid_recurring_view')
    cutoff = _filter_date(data['as_of'], 'as_of')
    upper = _filter_date(data['until'], 'until')
    if not 0 <= (date.fromisoformat(upper)-date.fromisoformat(cutoff)).days <= 366:
        raise ValueError('invalid_forecast_range')
    account_rows = [dict(row) for row in store.db.execute('SELECT * FROM accounts ORDER BY id')]
    if type(data['account_id']) is not str or (data['account_id'] and data['account_id'] not in {r['id'] for r in account_rows}):
        raise ValueError('invalid_account_id')
    if data['account_id']:
        account_rows = [r for r in account_rows if r['id'] == data['account_id']]
    rules = [_row(row) for row in store.db.execute('SELECT * FROM recurring_rules ORDER BY id')]
    settings = {r['document_id']: r for r in rules if r['document_id'] is not None}
    schedules = [{**r, 'basis': 'confirmed_manual', 'estimated': False, 'date_estimated': False, 'amount_estimated': False, 'missing_fields': [], 'phases': []}
                 for r in rules if r['document_id'] is None]
    # Per-view caches preserve the read-only cutoff and cannot outlive edits.
    rejected = {}

    def is_rejected(account_id, external_id, document_id):
        key = (account_id, external_id, document_id)
        if key not in rejected:
            rejected[key] = _document_link_rejected(store, account_id, external_id, document_id)
        return rejected[key]

    active_contracts = contracts._active_contracts(store, include_unconfigured=True)
    linked_rows = {d['id']: _linked(store, d['id'], cutoff, full_only=False, is_rejected=is_rejected)
                   for d, c in active_contracts if c['configured']}
    full_rows = {document_id: [row for row in rows
                              if money(row['allocated_amount']) == abs(money(row['amount']))]
                 for document_id, rows in linked_rows.items()}
    schedules.extend(_doc_schedule(d, c, settings.get(d['id']), full_rows[d['id']])
                     if c['configured'] else _unconfigured_schedule(d)
                     for d, c in active_contracts)
    # These exact dates are published into the shared household forecast only
    # after the plan owner explicitly confirms the account and source revision.
    from .paypal_credit_forecast import forecast_schedules
    schedules.extend(forecast_schedules(store, as_of=cutoff))
    membership = {(r['account_id'], r['external_id']): dict(r)
                  for r in store.db.execute('SELECT * FROM booking_contract_links')}
    detected_candidates = recurring_detection.detect(store, as_of=cutoff, known_schedules=schedules)
    # Explicit provenance covers the original evidence even when the chosen
    # future amount/label differs. It must not spawn a second inferred plan.
    for candidate in detected_candidates:
        linked_rules = {membership[(b['account_id'], b['external_id'])]['rule_id']
                        for b in candidate['bookings']
                        if (b['account_id'], b['external_id']) in membership
                        and membership[(b['account_id'], b['external_id'])]['decision'] == 'confirmed'}
        if linked_rules:
            if candidate['status'] != 'blocked':
                candidate['status'] = 'covered'
            candidate['covered_by'].extend({'type': 'booking_contract_rule', 'id': rid}
                                           for rid in sorted(linked_rules))
    if data['account_id']:
        detected_candidates = [c for c in detected_candidates if c['account_id'] == data['account_id']]
    schedules.extend({'id': c['id'], 'revision': 0, 'document_id': None, 'document_revision': None,
                      'label': c['label'], 'account_id': c['account_id'], 'currency': c['currency'],
                      'amount': c['amount'], 'valid_from': c['valid_from'], 'valid_to': c['valid_to'],
                      'day_of_month': c['day_of_month'], 'interval_months': c['interval_months'],
                      'reference': '', 'active': True, 'basis': 'auto_detected', 'estimated': True,
                      'date_estimated': True, 'amount_estimated': True, 'missing_fields': [], 'phases': [],
                      'inferred': True, 'candidate_id': c['id'], 'bookings': c['bookings'], 'warnings': c['warnings']}
                     for c in detected_candidates if c['status'] == 'stable')
    schedules = [s for s in schedules if not data['account_id'] or s['account_id'] in (None, data['account_id'])]
    lower = cutoff[:8] + '01'
    matching_lower = max(date.min, date.fromisoformat(lower)-timedelta(days=min(7, date.fromisoformat(lower).toordinal()-1))).isoformat()
    matching_upper = (date.fromisoformat(upper)+timedelta(days=min(7, date.max.toordinal()-date.fromisoformat(upper).toordinal()))).isoformat()
    bookings = [dict(row) for row in store.db.execute(
        'SELECT t.*,COALESCE(c.counterparty,\'\') AS counterparty,COALESCE(c.description,\'\') AS description '
        'FROM active_transactions t LEFT JOIN transaction_context c USING(account_id,external_id) WHERE t.date>=? AND t.date<=? '
        f'AND NOT COALESCE({sql_transfer_predicate(context_alias="c")},0)',
        (matching_lower, cutoff))]
    events = []
    for schedule in schedules:
        if not schedule['active'] or schedule['missing_fields']:
            continue
        if schedule['basis'] == 'confirmed_paypal_credit':
            events.extend(_credit_events(schedule, bookings, cutoff, matching_lower, matching_upper, membership))
            continue
        phases = schedule['phases'] if schedule['document_id'] else [{
            'valid_from': schedule['valid_from'], 'valid_to': schedule['valid_to'],
            'amount': schedule['amount'], 'interval_months': schedule['interval_months']}]
        explicit = {(r['account_id'], r['external_id']) for r in full_rows.get(schedule['document_id'], [])}
        inferred_bookings = {(r['account_id'], r['external_id']) for r in schedule.get('bookings', [])}
        explicit |= inferred_bookings
        explicit |= {key for key, link in membership.items()
                     if link['decision'] == 'confirmed' and link['rule_id'] == schedule['id']}
        partial = linked_rows.get(schedule['document_id'], [])
        for phase in phases:
            end = min(value for value in (phase['valid_to'], schedule['valid_to'], matching_upper) if value is not None)
            for expected in _dates(phase['valid_from'], end, schedule['day_of_month'], phase['interval_months'], max(matching_lower, schedule['valid_from']), matching_upper):
                amount = -money(phase['amount']) if schedule['document_id'] else money(phase['amount'])
                candidates = [b for b in bookings if b['account_id'] == schedule['account_id']
                              and b['currency'] == schedule['currency'] and not b['transfer_id']
                              and ((b['account_id'], b['external_id']) not in membership or (
                                  membership[(b['account_id'], b['external_id'])]['decision'] == 'confirmed'
                                  and membership[(b['account_id'], b['external_id'])]['rule_id'] == schedule['id']))
                              and (not schedule['document_id'] or not is_rejected(b['account_id'], b['external_id'], schedule['document_id']))
                              and (money(b['amount']) == amount or (b['account_id'], b['external_id']) in inferred_bookings)
                              and (b['date'][:7] == expected[:7]
                                  or abs((date.fromisoformat(b['date'])-date.fromisoformat(expected)).days) <= 7)
                              and ((b['account_id'], b['external_id']) in explicit or contracts._reference_present(
                                  schedule['reference'], b['counterparty']+' '+b['description']))]
                linked_candidates = [b for b in partial if b['account_id'] == schedule['account_id']
                                     and ((b['account_id'], b['external_id']) not in membership or (
                                         membership[(b['account_id'], b['external_id'])]['decision'] == 'confirmed'
                                         and membership[(b['account_id'], b['external_id'])]['rule_id'] == schedule['id']))
                                     and b['currency'] == schedule['currency']
                                     and (b['date'][:7] == expected[:7]
                                          or abs((date.fromisoformat(b['date'])-date.fromisoformat(expected)).days) <= 7)]
                events.append({'id': f"{schedule['id'] or 'd'+str(schedule['document_id'])}:{expected}",
                               'rule_id': schedule['id'], 'document_id': schedule['document_id'],
                               'label': schedule['label'], 'expected_date': expected, 'observed_date': None,
                               'amount': format(amount, '.2f'), 'expected_amount': format(amount, '.2f'), 'observed_amount': None,
                               'currency': schedule['currency'],
                               'account_id': schedule['account_id'], 'status': 'expected' if expected > cutoff else 'unresolved',
                               'estimated': schedule['estimated'], 'date_estimated': schedule['date_estimated'],
                               'amount_estimated': schedule['amount_estimated'], 'basis': schedule['basis'],
                               '_reference': schedule['reference'],
                               'matched_bookings': [], 'warnings': [], '_candidates': candidates, '_linked_candidates': linked_candidates})
    # Include neighbouring due dates outside the returned range in ownership:
    # March 31 belongs to April 1 even if a view ends on March 31.
    nearest = {}
    for event in events:
        for booking in event['_candidates'] + event['_linked_candidates']:
            key = (event['rule_id'], event['document_id'], booking['account_id'], booking['external_id'])
            distance = abs((date.fromisoformat(booking['date'])-date.fromisoformat(event['expected_date'])).days)
            nearest[key] = min(nearest.get(key, distance), distance)
    for event in events:
        for field in ('_candidates', '_linked_candidates'):
            event[field] = [booking for booking in event[field]
                if abs((date.fromisoformat(booking['date'])-date.fromisoformat(event['expected_date'])).days)
                == nearest[(event['rule_id'], event['document_id'], booking['account_id'], booking['external_id'])]]
    claims = {}
    for event in events:
        for booking in event['_candidates']:
            key = (booking['account_id'], booking['external_id'])
            claims[key] = claims.get(key, 0)+1
    ambiguous_bookings = set()
    for event in events:
        candidates = event.pop('_candidates')
        linked_candidates = event.pop('_linked_candidates')
        if any(money(b['allocated_amount']) != abs(money(b['amount'])) or money(b['amount']) != money(event['amount'])
               for b in linked_candidates):
            event['warnings'].append('partial_payment_conflict')
        if event['warnings']:
            event['status'] = 'unresolved'
        elif len(candidates) == 1 and claims[(candidates[0]['account_id'], candidates[0]['external_id'])] == 1:
            booking = candidates[0]
            if booking['date'][:7] != event['expected_date'][:7]:
                event['warnings'].append('booking_date_proximity_estimate')
            event.update(status='booked', observed_date=booking['date'], amount=format(money(booking['amount']), '.2f'),
                         observed_amount=format(money(booking['amount']), '.2f'), matched_bookings=[{
                key: booking[key] for key in ('account_id', 'external_id', 'date', 'amount')}])
        elif candidates:
            ambiguous_bookings.update((b['account_id'], b['external_id']) for b in candidates)
            event.update(status='unresolved', warnings=['ambiguous_booking_match'])
        elif event['status'] == 'unresolved':
            event['warnings'].append('past_payment_unresolved')
    _credit_collisions(events)
    for event in events:
        event.pop('_reference', None)
    events = [event for event in events if lower <= event['expected_date'] <= upper]
    events.sort(key=lambda e: (e['expected_date'], e['id']))
    # A virtual intermediate balance uses this explicit logical event order,
    # not a bank-reported collection time. Actual and unresolved events are
    # never another projected movement.
    future_counts = {}
    for event in events:
        event['projected_balance'] = None
        event['projection_sequence_basis'] = 'rule_id_order'
        if event['expected_date'] > cutoff and event['status'] == 'expected':
            key = (event['account_id'], event['currency'], event['expected_date'])
            future_counts[key] = future_counts.get(key, 0) + 1
    balances = latest_by_account(store, account_rows, as_of=cutoff)
    uncertain_accounts = transaction_date_accounts(store, [row['id'] for row in account_rows])
    accounts = []
    for account in account_rows:
        aid = account['id']
        recorded = balances.get(aid)
        warnings, points = [], []
        current = projected = None
        if aid in uncertain_accounts:
            # A recorded balance remains visible, but a transaction date is not
            # evidence for rolling it across a booking-date cutoff.
            warnings.append('paypal_transaction_date_not_booking_date')
        elif recorded and recorded.get('status') != 'ambiguous' and recorded.get('amount') is not None:
            current = money(recorded['amount']) + sum((money(r['amount']) for r in store.db.execute(
                'SELECT amount FROM active_transactions WHERE account_id=? AND currency=? AND date>? AND date<=?',
                (aid, account['currency'], recorded['booked_on'], cutoff))), Decimal('0'))
            projected = current
            points.append({'date': cutoff, 'amount': format(current, '.2f')})
            if recorded['booked_on'] < cutoff:
                warnings.append('balance_rollforward_ledger_completeness_unverified')
            if (date.fromisoformat(cutoff)-date.fromisoformat(recorded['booked_on'])).days > 7:
                warnings.append('stale_recorded_balance')
            for event in events:
                if event['account_id'] == aid and event['expected_date'] > cutoff and event['status'] == 'expected' and event['currency'] == account['currency']:
                    projected += money(event['amount'])
                    event['projected_balance'] = format(projected, '.2f')
                    if future_counts[(aid, event['currency'], event['expected_date'])] > 1:
                        event['warnings'].append('same_day_projection_order_assumed')
                    point = {'date': event['expected_date'], 'amount': format(projected, '.2f')}
                    if points[-1]['date'] == point['date']:
                        points[-1] = point
                    else:
                        points.append(point)
            if points[-1]['date'] != upper:
                points.append({'date': upper, 'amount': format(projected, '.2f')})
        else:
            warnings.append('recorded_balance_ambiguous' if recorded and recorded.get('status') == 'ambiguous'
                            else 'recorded_balance_missing')
        if any(e['account_id'] == aid and e['status'] == 'unresolved' for e in events):
            warnings.append('unresolved_payments_excluded')
        for schedule in schedules:
            if schedule['account_id'] == aid and schedule.get('basis') == 'confirmed_paypal_credit':
                warnings.extend(w for w in schedule.get('warnings', []) if w not in warnings)
        accounts.append({'account_id': aid, 'label': account.get('display_name') or aid,
                         'currency': account['currency'], 'recorded_balance': recorded,
                         'as_of_balance': format(current, '.2f') if current is not None else None,
                         'projected_balance': format(projected, '.2f') if projected is not None else None,
                         'points': points, 'warnings': warnings})
    return {'ambiguous_bookings': [{'account_id': a, 'external_id': e} for a, e in sorted(ambiguous_bookings)],
            'as_of': cutoff, 'until': upper, 'schedules': schedules, 'events': events,
            'accounts': accounts, 'candidates': detected_candidates, 'warnings': ['projection_conditional_on_imported_ledger',
                                              'transfers_not_inferred', 'budgets_not_payment_events']}
