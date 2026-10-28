"""Complete, read-only booking series and separate, non-posting continuation previews.

The catalogue deliberately includes rows rejected by automatic recurrence detection.
It never changes review decisions, rules, ledger rows or the official forecast.
"""
import calendar
import hashlib
import json
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from statistics import median

from . import budget, contracts, recurring
from .classification import _cash_withdrawal_row_evidence
from .core import money
from .recurring_detection import _REVERSAL, _SETTLEMENT, _contract_marker_label, _month, _party_key
from .transfer_corrections import sql_transfer_predicate

_HISTORY_FIELDS = ('account_id', 'external_id', 'date', 'amount', 'currency', 'counterparty', 'description')
_WARNING_TEXT = {
    'missing_recurring_period': 'Eine erwartete Periode fehlt.',
    'ambiguous_source_history': 'Die Buchungshistorie ist nicht eindeutig.',
    'insufficient_recurring_history': 'Noch zu wenige Buchungen für eine verlässliche Fortsetzung.',
    'unsupported_cadence': 'Der Zahlungsrhythmus ist offen.',
    'conflicting_contract_markers': 'Widersprüchliche Vertragskennzeichnungen.',
    'large_amount_variation': 'Die bisherigen Beträge schwanken deutlich.',
    'end_date_unknown': 'Ein Enddatum ist nicht bekannt.',
}


def _keys(rows):
    return {(r['account_id'], r['external_id']) for r in rows
            if isinstance(r, dict) and 'account_id' in r and 'external_id' in r}


def _preview(history, marker_intervals, lower, upper):
    """Preview monthly totals, keeping all observations separate in history."""
    months = defaultdict(list)
    for row in history:
        months[row['date'][:7]].append(row)
    ordered = sorted(months)
    gaps = [(_month(date.fromisoformat(b + '-01')) - _month(date.fromisoformat(a + '-01')))
            for a, b in zip(ordered, ordered[1:])]
    intervals = set(marker_intervals)
    valid_markers = {v for v in intervals if type(v) is int and 1 <= v <= 120}
    if intervals:
        interval = next(iter(valid_markers)) if len(intervals) == len(valid_markers) == 1 else None
        assumption = False
    else:
        # A repeated shortest gap can explain missing periods, but remains an
        # explicitly conditional display preview, never an automatic cashflow.
        gap_counts = Counter(gaps)
        interval = min(gap_counts, default=None)
        if interval not in {1, 3, 12} or not all(gap % interval == 0 for gap in gaps):
            interval = None
        assumption = bool(interval and (len(set(gaps)) > 1 or len(ordered) < 3))
    if interval is None:
        return None, None, None, [], 'none', 'Rhythmus nicht eindeutig; noch keine Fortsetzung geschätzt.'
    recent = ordered[-3:]
    totals = [sum((money(r['amount']) for r in months[month]), Decimal(0)) for month in recent]
    amount = median(totals).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
    if not amount:
        return interval, format(amount, '.2f'), None, [], 'none', 'Bisheriger Monatsbetrag ist null; keine weitere Zahlung geschätzt.'
    days = [date.fromisoformat(months[month][-1]['date']).day for month in recent]
    month_end = all(date.fromisoformat(months[month][-1]['date']).day == calendar.monthrange(
        int(month[:4]), int(month[5:]))[1] for month in recent)
    day = 31 if month_end else int(median(days))
    # Use the existing date iterator to preserve month-end clamping and bounds.
    start = history[-1]['date']
    preview = [{'date': when, 'amount': format(amount, '.2f'), 'currency': history[0]['currency'],
                'status': 'preview', 'estimated': True}
               for when in recurring._dates(start, None, day, interval, lower, upper)
               if when > lower]
    return interval, format(amount, '.2f'), day, preview, 'assumption' if assumption else 'monthly_totals', None


def _schedule_directions(schedule):
    values = ([schedule['amount']] if schedule.get('amount') is not None else
              [p['amount'] for p in schedule.get('exact_payments', [])])
    if schedule.get('document_id') is not None:
        values = [-money(p['amount']) for p in schedule.get('phases', [])] or ['-1.00']
    elif schedule.get('basis') == 'confirmed_paypal_credit' and not values:
        values = ['-1.00']
    return {1 if money(value) > 0 else -1 if money(value) < 0 else 0 for value in values}


def catalogue(store, scope, recurring_data, proposals):
    """Return every current ledger row grouped by account, party, direction and transfer.

    Only the explicit account selection and the real evidence cutoff limit rows.
    Forecast previews are separate display data; authoritative schedules retain
    their existing events and boundaries rather than receiving an extrapolation.
    """
    cutoff = min(scope['as_of'], date.today().isoformat())
    lower = max(scope['as_of'], date.today().isoformat())
    upper = scope['until']
    accounts = {r['id']: dict(r) for r in store.db.execute('SELECT * FROM accounts')}
    rows = [dict(r) for r in store.db.execute(
        "SELECT t.*,t.category AS source_category,o.category_id,COALESCE(c.counterparty,'') AS counterparty,"
        "COALESCE(c.description,'') AS description,"
        f'COALESCE({sql_transfer_predicate(context_alias="c")},0) AS is_transfer '
        'FROM active_transaction_versions t LEFT JOIN transaction_context c USING(account_id,external_id) '
        'LEFT JOIN classification_overrides o USING(account_id,external_id) '
        "WHERE t.date<=? AND (?='' OR t.account_id=?) ORDER BY t.date,t.external_id",
        (cutoff, scope['account_id'], scope['account_id']))]
    groups = defaultdict(list)
    owner = {}
    for row in rows:
        label = row['counterparty'].strip() or row['description'].strip() or 'Gegenpartei offen'
        amount = money(row['amount'])
        identity = (row['account_id'], row['currency'], _party_key(label),
                    1 if amount > 0 else -1 if amount < 0 else 0, bool(row['is_transfer']))
        groups[identity].append(row)
        owner[(row['account_id'], row['external_id'])] = identity

    candidates = defaultdict(list)
    for candidate in recurring_data.get('candidates', []):
        for identity in {owner[key] for key in _keys(candidate.get('bookings', [])) if key in owner}:
            candidates[identity].append(candidate)
    proposal_ids = defaultdict(set)
    for proposal in proposals:
        payload = proposal.get('_payload', {})
        refs = _keys(payload.get('candidate', {}).get('bookings', []))
        if payload.get('booking'):
            refs |= _keys([payload['booking']])
        for identity in {owner[key] for key in refs if key in owner}:
            proposal_ids[identity].add(proposal['id'])
        for identity, items in candidates.items():
            if any(proposal['id'] == 'series:' + c['id'] for c in items):
                proposal_ids[identity].add(proposal['id'])

    rejected = set()
    for row in store.db.execute("SELECT payload FROM payment_review_decisions WHERE kind='recurring' AND decision='reject'"):
        refs = _keys(json.loads(row['payload']).get('candidate', {}).get('bookings', []))
        rejected.update(owner[key] for key in refs if key in owner)
    rule_refs, doc_refs = defaultdict(set), defaultdict(set)
    for row in store.db.execute('SELECT * FROM booking_contract_links'):
        identity = owner.get((row['account_id'], row['external_id']))
        if identity is None:
            continue
        if row['decision'] == 'confirmed':
            rule_refs[identity].add(row['rule_id'])
        else:
            rejected.add(identity)
    for row in store.db.execute(
            'SELECT l.account_id,l.external_id,l.document_id FROM classification_document_links l '
            'JOIN classification_documents d ON d.id=l.document_id '
            "WHERE d.kind='contract' AND d.status='confirmed' AND NOT EXISTS "
            '(SELECT 1 FROM rejected_document_links r WHERE r.account_id=l.account_id '
            'AND r.external_id=l.external_id AND r.document_id=l.document_id)'):
        identity = owner.get((row['account_id'], row['external_id']))
        if identity is not None:
            doc_refs[identity].add(row['document_id'])
    for identity, items in candidates.items():
        for candidate in items:
            for ref in candidate.get('covered_by', []):
                if ref['type'] in {'rule', 'booking_contract_rule', 'paypal_credit'}:
                    rule_refs[identity].add(ref['id'])
                elif ref['type'] == 'document':
                    doc_refs[identity].add(ref['document_id'])
    schedules = [s for s in recurring_data.get('schedules', [])
                 if s.get('basis') != 'auto_detected']
    for schedule in schedules:
        refs = _keys(schedule.get('bookings', [])) | _keys((schedule.get('amount_derivation') or {}).get('bookings', []))
        for identity in {owner[key] for key in refs if key in owner}:
            if schedule['id'] is not None:
                rule_refs[identity].add(schedule['id'])
            elif schedule.get('document_id') is not None:
                doc_refs[identity].add(schedule['document_id'])
        # Exact contract references in actual source rows are provenance, unlike
        # a same-name vendor heuristic. Known loan ends remain authoritative.
        for identity, history in groups.items():
            if (schedule.get('account_id') == identity[0] and schedule.get('currency') == identity[1]
                    and identity[3] in _schedule_directions(schedule)
                    and any(contracts._reference_present(schedule.get('reference', ''),
                        r['counterparty'] + ' ' + r['description']) for r in history)):
                if schedule['id'] is not None:
                    rule_refs[identity].add(schedule['id'])
                elif schedule.get('document_id') is not None:
                    doc_refs[identity].add(schedule['document_id'])
    for event in recurring_data.get('events', []):
        if event.get('basis') == 'auto_detected':
            continue
        for identity in {owner[key] for key in _keys(event.get('matched_bookings', [])) if key in owner}:
            if event['rule_id'] is not None:
                rule_refs[identity].add(event['rule_id'])
            if event.get('document_id') is not None:
                doc_refs[identity].add(event['document_id'])
    markers = defaultdict(set)
    for row in store.db.execute(
            'SELECT m.* FROM finanzguru_row_metadata m JOIN active_transaction_versions t USING(account_id,external_id) '
            'WHERE m.booked_on=t.date AND m.currency=t.currency AND t.date<=?', (cutoff,)):
        identity = owner.get((row['account_id'], row['external_id']))
        if (identity is not None and (row['contract_id'] or row['contract_label'])
                and _contract_marker_label(row['contract_label']) != 'negative'):
            markers[identity].add(row['interval_months'])

    # Group ownership is computed once. One contract event is never repeated
    # in several payee groups merely because all contain a linked source row.
    authoritative_by_group = {}
    schedule_owners = defaultdict(set)
    for identity in groups:
        authoritative = [s for s in schedules
                         if identity[3] in _schedule_directions(s) and (
                             (s['id'] is not None and s['id'] in rule_refs[identity])
                             or (s.get('document_id') is not None and s['document_id'] in doc_refs[identity]))]
        authoritative_by_group[identity] = authoritative
        for schedule in authoritative:
            schedule_owners[(schedule['id'], schedule.get('document_id'))].add(identity)

    cash_audits = defaultdict(list)
    for audit in store.db.execute(
            "SELECT account_id,external_id,action,previous,current FROM classification_audit "
            "WHERE action IN ('classification_saved','classification_auto_applied','classification_reset') "
            'ORDER BY id DESC'):
        key = (audit['account_id'], audit['external_id'])
        if key in owner:
            cash_audits[key].append(audit)
    aliases = dict(store.db.execute('SELECT alias_id,canonical_id FROM category_aliases'))

    def cash_category(value):
        if value is None:
            return None
        return aliases.get(value, value) if isinstance(value, str) else budget.canonical_category_id(store, value)

    result = []
    for identity, raw in groups.items():
        aid, currency, _, _, is_transfer = identity
        history = [{k: r[k] for k in _HISTORY_FIELDS} for r in raw]
        for row in history:
            row['amount'] = format(money(row['amount']), '.2f')
        items = candidates[identity]
        states = {c['status'] for c in items}
        status = next((state for state in ('blocked', 'rejected', 'covered', 'needs_review', 'proposed', 'stable')
                       if state in states), 'transfer' if is_transfer else 'unclassified')
        if identity in rejected and status != 'blocked':
            status = 'rejected'
        authoritative = authoritative_by_group[identity]
        is_cash_withdrawal = any(_cash_withdrawal_row_evidence(
            r, cash_audits[(r['account_id'], r['external_id'])], cash_category) for r in raw)
        warning_codes = dict.fromkeys(w for c in items for w in c.get('warnings', []))
        warnings = [_WARNING_TEXT.get(w, 'Vertragsangaben oder bisherige Zuordnung prüfen.') for w in warning_codes]
        warnings.append('Gleiche Gegenpartei fasst Buchungen zusammen; kein Nachweis eines einzelnen Vertrags.')
        ambiguous = any(len(schedule_owners[(s['id'], s.get('document_id'))]) > 1 for s in authoritative)
        if ambiguous:
            warnings.append('Ein Vertrag gehört zu mehreren Buchungsreihen; seine Fortsetzung wird nicht mehrfach angezeigt.')
        forecast, interval, amount, day, basis, reason = [], None, None, None, 'none', None
        if upper <= lower:
            reason = 'Der gewählte Zeitraum enthält keine künftigen Termine.'
        elif authoritative or doc_refs[identity]:
            basis = 'confirmed_schedule'
            owned = {(s['id'], s.get('document_id')) for s in authoritative
                     if s.get('active') and not s.get('missing_fields')
                     and len(schedule_owners[(s['id'], s.get('document_id'))]) == 1}
            forecast = [{'date': e['expected_date'], 'amount': e['expected_amount'], 'currency': e['currency'],
                         'status': e['status'], 'estimated': bool(e.get('estimated')), 'rule_id': e['rule_id']}
                        for e in recurring_data.get('events', [])
                        if (e['rule_id'], e.get('document_id')) in owned and e['account_id'] == aid and e['currency'] == currency
                        and lower < e['expected_date'] <= upper]
            if status not in {'blocked', 'rejected'}:
                status = 'covered'
            if not forecast:
                reason = ('Vertragszuordnung mehrdeutig; keine doppelte Fortsetzung angezeigt.' if ambiguous else
                          'Keine weiteren Vertragstermine im Zeitraum; Vertragsgrenzen bleiben maßgeblich.')
        elif status in {'blocked', 'rejected'}:
            reason = ('Bereits abgelehnt; keine geschätzte Fortsetzung.' if status == 'rejected'
                      else 'Verbindliche Vertragsangaben müssen geprüft werden.')
        else:
            excluded = accounts[aid]['kind'] == 'CASH' or is_cash_withdrawal or any(
                pattern.search(' '.join(str(r.get(k) or '') for k in ('counterparty', 'description', 'category')))
                for r in raw for pattern in (contracts._EXCLUDED, _REVERSAL, _SETTLEMENT))
            if excluded and not is_transfer:
                reason = 'Bargeld, Erstattung oder Abrechnung: sichtbar, aber ohne geschätzte Fortsetzung.'
            else:
                interval, amount, day, forecast, basis, reason = _preview(history, markers[identity], lower, upper)
            if basis == 'assumption':
                warnings.append('Fortsetzung nur als Annahme; Lücken oder kurze Historie vorhanden.')
            if forecast:
                warnings.append('Geschätzte Vorschau, nicht zusätzlich in die Kontoprognose eingerechnet.')
        if is_transfer:
            warnings.append('Umbuchung: Vorschau nur pro Konto; kein zusätzlicher Haushaltsaufwand.')
        if accounts[aid]['kind'] == 'CASH':
            warnings.append('Bargeldkonto: Vorschau nur für dieses Konto.')
        if len({r['date'][:7] for r in history}) < len(history):
            warnings.append('Mehrere Buchungen je Monat: Fortsetzung verwendet deren Monatssumme.')
        result.append({'id': 'ledger:' + hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:20],
                       'label': raw[-1]['counterparty'] or raw[-1]['description'] or 'Gegenpartei offen',
                       'account_id': aid, 'account_label': accounts[aid].get('display_name') or aid,
                       'currency': currency, 'is_transfer': is_transfer,
                       'is_cash_withdrawal': is_cash_withdrawal, 'status': status,
                       'warnings': list(dict.fromkeys(warnings)), 'history': history,
                       'forecast': sorted(forecast, key=lambda e: (e['date'], str(e.get('rule_id', '')))),
                       'forecast_empty_reason': reason, 'projection_basis': basis,
                       'interval_months': interval, 'amount': amount, 'day_of_month': day,
                       'proposal_ids': sorted(proposal_ids[identity]),
                       'candidate_ids': sorted(c['id'] for c in items)})
    return sorted(result, key=lambda r: (r['label'].casefold(), r['account_id'], r['id']))


def attach_proposals(rows, proposals):
    """Attach fresh decision identities without rescanning the ledger or contracts."""
    owners = {(b['account_id'], b['external_id']): row for row in rows for b in row['history']}
    for proposal in proposals:
        payload = proposal.get('_payload', {})
        refs = _keys(payload.get('candidate', {}).get('bookings', []))
        if payload.get('booking'):
            refs |= _keys([payload['booking']])
        for key in refs:
            if key in owners:
                row = owners[key]
                row['proposal_ids'] = sorted(set(row['proposal_ids']) | {proposal['id']})
    return rows
