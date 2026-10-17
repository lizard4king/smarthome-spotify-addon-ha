"""Read-only, provenance-bearing recurring NET ledger payment detection.

Existing decisions group evidence; budget and document prices are never cashflows.
Only actual bookings through the cutoff train estimates. Detection never writes.
"""
import calendar
import hashlib
import json
import re
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from statistics import median

from . import budget, contracts
from .booking_categories import _canonical_counterparty
from .classification import _cash_withdrawal_evidence
from .classification_rules import is_intermediary, rule_is_eligible
from .core import money
from .transfer_corrections import sql_transfer_predicate

_REVERSAL = re.compile(r'\b(?:storno\w*|reversal|refund|rückzahlung|rueckzahlung)\b', re.I)
_SETTLEMENT = re.compile(r'kreditkarten(?:ausgleich|abrechnung)|credit.?card.{0,20}(?:settlement|payment)|card settlement', re.I)


def _party_key(value):
    """Return a recurring-comparison key, ignoring a valid leading DE IBAN."""
    return _canonical_counterparty(value)


def _contract_marker_label(value):
    """Return source-label semantics without changing the retained raw value."""
    if value is None:
        return None
    normalized = value.strip().casefold()
    if normalized == 'nein':
        return 'negative'
    if normalized == 'ja':
        return 'boolean'
    return 'descriptive'


def _month(value):
    return value.year*12+value.month-1


def _next_date(last, day, interval):
    year, month = divmod(_month(last)+interval, 12)
    if year > 9999:
        return date.max
    return date(year, month+1, min(day, calendar.monthrange(year, month+1)[1]))


def detect(store, *, as_of, known_schedules):
    """Return candidates and their last three actual booking references.

    Explicit fixed/income budget allocations have priority over category mappings,
    which require one unique counterparty group. Unmarked rows need three monthly
    observations within 20% of their absolute median. A missing due period beyond
    the seven-day date tolerance suspends further prediction. Covered/disabled
    rules and explicit document links suppress duplicate inferred events.
    """
    cutoff = date.fromisoformat(as_of)
    snapshot = budget.load(store)
    plan = snapshot.get('plan') or {}
    items = {i['id']: i for i in plan.get('items', [])}
    markers = defaultdict(list)
    if store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='finanzguru_row_metadata'").fetchone():
        for row in store.db.execute('SELECT m.* FROM finanzguru_row_metadata m JOIN active_transactions t USING(account_id,external_id) '
                'WHERE m.booked_on=t.date AND m.currency=t.currency AND t.date<=?', (as_of,)):
            label_kind = _contract_marker_label(row['contract_label'])
            # An explicit negative label vetoes even a populated ID. Keep the
            # source row intact, but do not grant it marker-backed confidence.
            if label_kind != 'negative' and (row['contract_id'] or row['contract_label']):
                marker = dict(row)
                marker['_label_kind'] = label_kind
                key = (row['account_id'], row['external_id'])
                identity = (row['contract_id'], row['contract_label'], row['cadence_raw'], row['interval_months'])
                if not any((m['contract_id'], m['contract_label'], m['cadence_raw'], m['interval_months']) == identity for m in markers[key]):
                    markers[key].append(marker)
    rows, reversals = [], []
    for row in store.db.execute(
            'SELECT t.*,c.counterparty,c.description,o.category_id FROM active_transactions t '
            'JOIN accounts a ON a.id=t.account_id AND a.currency=t.currency '
            'LEFT JOIN transaction_context c USING(account_id,external_id) '
            'LEFT JOIN classification_overrides o USING(account_id,external_id) '
            "WHERE t.date<=? AND a.kind!='CASH' "
            f'AND NOT COALESCE({sql_transfer_predicate(context_alias="c")},0) ORDER BY t.date,t.external_id', (as_of,)):
        text = ' '.join(str(row[k] or '') for k in ('counterparty', 'description', 'category'))
        if _REVERSAL.search(text) and row['counterparty']:
            reversals.append(dict(row))
        if (not row['counterparty'] or not money(row['amount']) or contracts._EXCLUDED.search(text)
                or _REVERSAL.search(text) or _SETTLEMENT.search(text) or _cash_withdrawal_evidence(store, row['account_id'], row['external_id'])):
            continue
        item = dict(row)
        item['party'] = _party_key(row['counterparty'])
        if not markers.get((row['account_id'], row['external_id'])) and (
                item['party'] in {'lastschrift', 'abbuchung', 'zahlung', 'kartenzahlung', 'sonstige'}
                or is_intermediary(row['counterparty'], row['description'])):
            continue
        rows.append(item)
    rows = [r for r in rows if not any(v['account_id'] == r['account_id'] and v['currency'] == r['currency']
            and _party_key(v['counterparty']) == r['party'] and money(v['amount']) == -money(r['amount'])
            and abs((date.fromisoformat(v['date'])-date.fromisoformat(r['date'])).days) <= 7 for v in reversals)]
    row_by_key = {(r['account_id'], r['external_id']): r for r in rows}
    allocations = defaultdict(list)
    for allocation in plan.get('actual_allocations', []):
        allocations[(allocation['account_id'], allocation['external_id'])].append(allocation)
    mapped = defaultdict(set)
    for mapping in plan.get('actual_mappings', []):
        mapped[budget.canonical_category_id(store, mapping['category_id'])].add(mapping['item_id'])
    canonical_categories = {}

    def canonical(category):
        if category not in canonical_categories:
            canonical_categories[category] = budget.canonical_category_id(store, category)
        return canonical_categories[category]

    mapping_groups = defaultdict(set)
    for row in rows:
        for iid in mapped.get(canonical(row['category_id']), set()):
            mapping_groups[iid].add((row['account_id'], row['currency'], money(row['amount']) > 0, row['party']))
    groups = defaultdict(list)
    for key, row in row_by_key.items():
        assigned = allocations.get(key, [])
        iid, source = None, 'booking_history'
        if assigned:
            # Split allocations do not prove that the whole bank amount recurs.
            if len(assigned) != 1:
                continue
            iid, source = assigned[0]['item_id'], 'budget_allocations'
        elif markers.get(key):
            marker = markers[key][0]
            marker_identity = marker['contract_id'] or (
                row['party'] if marker.get('_label_kind') == 'boolean' else marker['contract_label'])
            iid = ('finanzguru_contract', marker_identity)
            source = 'finanzguru_contract_marker'
            row['marker'] = marker
            row['conflicting_marker'] = len(markers[key]) > 1
        else:
            targets = mapped.get(canonical(row['category_id']), set())
            if targets:
                if len(targets) != 1:
                    continue
                iid = next(iter(targets))
                if len(mapping_groups[iid]) != 1:
                    continue
                source = 'budget_category_mapping'
        if iid is not None and source != 'finanzguru_contract_marker':
            item = items.get(iid)
            if (not item or item['kind'] not in {'income', 'fixed'} or not item['confirmed']
                    or (item['kind'] == 'income') != (money(row['amount']) > 0)
                    or item.get('account_id', row['account_id']) != row['account_id']):
                continue
        groups[(row['account_id'], row['currency'], money(row['amount']) > 0, row['party'], iid)].append(row)
    links = defaultdict(set)
    for row in store.db.execute(
            'SELECT l.*,d.warnings FROM classification_document_links l JOIN classification_documents d ON d.id=l.document_id '
            "WHERE d.kind='contract' AND d.status='confirmed' AND NOT EXISTS (SELECT 1 FROM rejected_document_links a "
            "WHERE a.account_id=l.account_id AND a.external_id=l.external_id AND a.document_id=l.document_id)"):
        if not set(json.loads(row['warnings'])) & contracts._EXCLUDED_DOCUMENT_WARNINGS:
            links[(row['account_id'], row['external_id'])].add(row['document_id'])
    classification_rules = {(_party_key(r['counterparty_normalized']), r['direction']): dict(r)
                            for r in store.db.execute('SELECT * FROM classification_rules')}
    candidates = []
    for identity, evidence in sorted(groups.items(), key=lambda pair: str(pair[0])):
        aid, currency, positive, party, iid = identity
        marker = evidence[-1].get('marker')
        if len(evidence) < (1 if marker else 3):
            continue
        recent = evidence[-3:]
        dates = [date.fromisoformat(r['date']) for r in recent]
        item = items.get(iid) if iid and not marker else None
        interval = marker['interval_months'] if marker else item.get('interval_months', 1) if item else 1
        cadence_ok = interval is not None and all(_month(b)-_month(a) == interval for a,b in zip(dates, dates[1:]))
        unique_months = len({_month(d) for d in dates}) == len(dates) and all(
            sum(r['date'][:7] == d.isoformat()[:7] for r in evidence) == 1 for d in dates)
        if not marker and (not cadence_ok or not unique_months):
            continue
        month_end = all(d.day == calendar.monthrange(d.year, d.month)[1] for d in dates)
        day = 31 if month_end else int(median([d.day for d in dates]))
        normalized_days = [31 if d.day == calendar.monthrange(d.year, d.month)[1] else d.day for d in dates]
        day_ok = max(normalized_days)-min(normalized_days) <= 7
        if not day_ok and not marker:
            continue
        amount = median([money(r['amount']) for r in recent]).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
        spread = any(abs(abs(money(r['amount']))-abs(amount)) > abs(amount)*Decimal('.20') for r in recent)
        if spread and not item and not marker:
            continue
        source = ('budget_allocations' if any(allocations.get((r['account_id'], r['external_id'])) for r in recent)
                  else 'budget_category_mapping') if item else 'booking_history'
        if marker:
            source = 'finanzguru_contract_marker'
        refs = [{'type': 'budget_item', 'revision': snapshot['revision'], 'item_id': iid}] if item else []
        if marker:
            refs.append({'type': 'finanzguru_contract', 'source': 'finanzguru',
                         **{k: marker[k] for k in ('contract_id', 'contract_label', 'cadence_raw', 'source_sha256', 'source_reference')}})
        rule = classification_rules.get((party, 'income' if positive else 'expense'))
        if rule and all(rule_is_eligible(rule, row['date']) for row in recent):
            refs.append({'type': 'classification_rule', 'id': rule['id']})
        booking_refs = [{k: r[k] for k in ('account_id', 'external_id', 'date', 'amount')} for r in recent]
        for schedule in known_schedules:
            if schedule.get('requires_contract_setup') and _party_key(schedule['label']) == party:
                refs.append({'type': 'document', 'document_id': schedule['document_id']})
        covered = []
        effective_marker_label = (marker['contract_label'] if marker and marker.get('_label_kind') == 'descriptive'
                                  else None)
        for row in evidence:
            covered.extend({'type': 'document', 'document_id': did} for did in links.get((aid, row['external_id']), set()))
        for schedule in known_schedules:
            if (schedule.get('document_id') and not schedule.get('requires_contract_setup')
                    and schedule.get('account_id') in (None, aid) and schedule.get('currency') in (None, currency)
                    and _party_key(schedule['label']) == party):
                covered.append({'type': 'document', 'document_id': schedule['document_id']})
                continue
            if (schedule.get('account_id') != aid or schedule.get('currency') != currency
                    or schedule.get('amount') is None or (money(schedule['amount']) > 0) != positive):
                continue
            relevant_labels = {party}
            if item:
                relevant_labels.add(_party_key(item['label']))
            elif effective_marker_label:
                relevant_labels.add(_party_key(effective_marker_label))
            same_label = _party_key(schedule['label']) in relevant_labels
            same_ref = any(contracts._reference_present(schedule.get('reference', ''), r['description'] or '') for r in evidence)
            same_payment = (money(schedule['amount']) == amount and schedule.get('day_of_month') == day
                            and schedule.get('interval_months') == interval)
            if same_label or same_ref or same_payment:
                covered.append({'type': 'rule', 'id': schedule['id'], 'active': schedule['active']})
        warnings = ['large_amount_variation'] if spread else []
        if not item or item['end_month'] is None:
            warnings.append('end_date_unknown')
        source_review = False
        if marker:
            if interval is None:
                warnings.append('unsupported_cadence')
                source_review = True
            if len(recent) < 2:
                warnings.append('insufficient_recurring_history')
                source_review = True
            if not cadence_ok or not unique_months or not day_ok:
                warnings.append('ambiguous_source_history')
                source_review = True
            if any(r.get('conflicting_marker') for r in recent):
                warnings.append('conflicting_contract_markers')
                source_review = True
        overdue = interval is not None and _next_date(dates[-1], day, interval) < cutoff-timedelta(days=min(7, cutoff.toordinal()-1))
        if overdue:
            warnings.append('missing_recurring_period')
        status = 'covered' if covered else 'needs_review' if overdue or source_review else 'stable'
        stable_id = 'auto:' + hashlib.sha256('|'.join(str(v) for v in identity).encode()).hexdigest()[:20]
        candidates.append({'id': stable_id, 'label': item['label'] if item else effective_marker_label or recent[-1]['counterparty'],
                           'account_id': aid, 'currency': currency, 'amount': format(amount, '.2f'),
                           'day_of_month': day, 'interval_months': interval, 'valid_from': max(recent[0]['date'], item['start_month']+'-01') if item else recent[0]['date'],
                           'valid_to': (item['end_month']+'-'
                               + str(calendar.monthrange(int(item['end_month'][:4]), int(item['end_month'][5:]))[1])) if item and item['end_month'] else None,
                           'source': source, 'source_refs': refs, 'bookings': booking_refs,
                           'status': status, 'warnings': warnings, 'estimated': True, 'covered_by': covered,
                           'derivation': {'amount_method': 'median', 'sample_count': len(recent),
                                          'rounding': 'ROUND_HALF_UP', 'date_method': 'month_end' if month_end else 'median_day',
                                          'as_of': as_of}})
    preferred = {}
    for candidate in candidates:
        if candidate['source'] != 'booking_history':
            row = row_by_key[(candidate['account_id'], candidate['bookings'][-1]['external_id'])]
            key = (candidate['account_id'], candidate['currency'], money(candidate['amount']) > 0, row['party'])
            preferred[key] = candidate['id']
    for candidate in candidates:
        row = row_by_key[(candidate['account_id'], candidate['bookings'][-1]['external_id'])]
        key = (candidate['account_id'], candidate['currency'], money(candidate['amount']) > 0, row['party'])
        if candidate['source'] == 'booking_history' and key in preferred:
            candidate['status'] = 'covered'
            candidate['covered_by'].append({'type': 'candidate', 'id': preferred[key]})
    return candidates
