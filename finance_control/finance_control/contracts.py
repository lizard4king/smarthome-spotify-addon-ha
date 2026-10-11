"""Confirmed contract schedules and deliberately manual booking evidence.

CHECK24 identifies the supplied document's origin, never an API connection.
Schedules use calendar months anchored at each phase start. They do not prove
the exact day a provider collects a payment, balances, or outstanding debt.
"""
import json
import re
from datetime import date

from .classification import (
    _audit,
    _cash_withdrawal_evidence,
    _document_link_rejected,
    _filter_date,
    _now,
    _require_not_duplicate_document,
    _text,
    document_recipient_matches,
)
from .core import money, parse_money_input
from .transfer_corrections import sql_transfer_predicate

_PAGE_SIZE = 25
_EXCLUDED = re.compile(
    r'\b(?:\w*bonus\w*|cashback\w*|refund\w*|\w*erstattung\w*|gutschrift\w*|rückzahlung\w*|rueckzahlung\w*|'
    r'einmalkauf|einzelkauf|einmalig\w*|einmalzahlung|einmalbetrag|einrichtung\w*|anschlussgebühr\w*|'
    r'anschlussgebuehr\w*|single\s+purchase|one[- ](?:off|time))\b', re.IGNORECASE)
_LIMIT_NOTES = [
    'Intervalle sind Kalendermonate ab Phasenbeginn; genaue Abbuchungstage sind nicht belegt.',
    'Vorschläge erfordern Einzelbestätigung; keine Saldo- oder Restschuldableitung.',
]
_EXCLUDED_DOCUMENT_WARNINGS = {
    'duplicate_source_document', 'source_excluded_bonsy', 'not_invoice_like',
}


def _document(store, document_id):
    if type(document_id) is not int or document_id < 1:
        raise ValueError('invalid_document_id')
    row = store.db.execute(
        'SELECT * FROM classification_documents WHERE id=?', (document_id,)).fetchone()
    if row is None or row['kind'] != 'contract':
        raise ValueError('contract_document_required')
    return row


def _revision(value):
    if type(value) is not int or value < 0:
        raise ValueError('invalid_revision')
    return value


def _load(store, document):
    row = store.db.execute(
        'SELECT * FROM contract_metadata WHERE document_id=?', (document['id'],)).fetchone()
    return {
        'document_id': document['id'], 'revision': document['revision'],
        'configured': row is not None, 'provider': document['vendor'],
        'reference': row['reference'] if row else '',
        'valid_from': row['valid_from'] if row else None,
        'valid_to': row['valid_to'] if row else None,
        'origin': row['origin'] if row else 'manual',
        'phases': json.loads(row['phases']) if row else [],
    }


def load(store, data):
    if not isinstance(data, dict) or set(data) != {'document_id'}:
        raise ValueError('invalid_contract_request')
    return _load(store, _document(store, data['document_id']))


def _date_range(start, end):
    start = _filter_date(start, 'valid_from')
    end = None if end is None else _filter_date(end, 'valid_to')
    if end is not None and end < start:
        raise ValueError('invalid_contract_date_range')
    return start, end


def _phases(values, valid_from, valid_to):
    if not isinstance(values, list) or len(values) > 24:
        raise ValueError('invalid_contract_phases')
    phases = []
    for value in values:
        if not isinstance(value, dict) or set(value) != {
                'valid_from', 'valid_to', 'amount', 'interval_months'}:
            raise ValueError('invalid_contract_phase')
        start, end = _date_range(value['valid_from'], value['valid_to'])
        amount = parse_money_input(value['amount'])
        interval = value['interval_months']
        if amount <= 0 or type(interval) is not int or not 1 <= interval <= 120:
            raise ValueError('invalid_contract_phase')
        if start < valid_from or (valid_to is not None
                                and (end is None or end > valid_to)):
            raise ValueError('contract_phase_outside_term')
        phases.append({'valid_from': start, 'valid_to': end,
                       'amount': format(amount, '.2f'), 'interval_months': interval})
    phases.sort(key=lambda phase: phase['valid_from'])
    for previous, current in zip(phases, phases[1:]):
        # End dates are inclusive. An open phase must be the final phase.
        if previous['valid_to'] is None or previous['valid_to'] >= current['valid_from']:
            raise ValueError('overlapping_contract_phases')
    return phases


def save(store, data):
    required = {'document_id', 'revision', 'reference', 'valid_from', 'valid_to',
                'origin', 'phases', 'confirmed'}
    if not isinstance(data, dict) or set(data) != required or data['confirmed'] is not True:
        raise ValueError('explicit_contract_confirmation_required')
    revision = _revision(data['revision'])
    reference = _text(data['reference'], 'contract_reference', 240, empty=True)
    start, end = _date_range(data['valid_from'], data['valid_to'])
    if data['origin'] not in ('manual', 'check24'):
        raise ValueError('invalid_contract_origin')
    phases = _phases(data['phases'], start, end)
    store.db.execute('BEGIN IMMEDIATE')
    try:
        document = _document(store, data['document_id'])
        _require_not_duplicate_document(document)
        if document['status'] != 'confirmed':
            raise ValueError('confirmed_contract_required')
        if set(json.loads(document['warnings'])) & _EXCLUDED_DOCUMENT_WARNINGS:
            raise ValueError('contract_document_not_eligible')
        if document['revision'] != revision:
            raise ValueError('stale_document_revision')
        previous = _load(store, document)
        now = _now()
        store.db.execute(
            'INSERT INTO contract_metadata(document_id,reference,valid_from,valid_to,origin,'
            'phases,revision,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?) '
            'ON CONFLICT(document_id) DO UPDATE SET reference=excluded.reference,'
            'valid_from=excluded.valid_from,valid_to=excluded.valid_to,origin=excluded.origin,'
            'phases=excluded.phases,revision=excluded.revision,updated_at=excluded.updated_at',
            (document['id'], reference, start, end, data['origin'], json.dumps(phases),
             revision + 1, now, now))
        if store.db.execute(
                'UPDATE classification_documents SET revision=revision+1 WHERE id=? AND revision=?',
                (document['id'], revision)).rowcount != 1:
            raise ValueError('stale_document_revision')
        result = load(store, {'document_id': document['id']})
        _audit(store, 'contract_saved', result, document_id=document['id'], previous=previous)
        store.db.commit()
    except Exception:
        store.db.rollback()
        raise
    return result


def _phase_on(contract, booking_date):
    if (booking_date < contract['valid_from']
            or (contract['valid_to'] is not None and booking_date > contract['valid_to'])):
        return None
    booked = date.fromisoformat(booking_date)
    for phase in contract['phases']:
        if (booking_date >= phase['valid_from']
                and (phase['valid_to'] is None or booking_date <= phase['valid_to'])):
            anchor = date.fromisoformat(phase['valid_from'])
            months = 12 * (booked.year - anchor.year) + booked.month - anchor.month
            if months % phase['interval_months'] == 0:
                return phase
    return None


def _reference_present(reference, text):
    # A short generic word is not an identifying contract reference.
    if len(reference) < 4 or not any(character.isdigit() for character in reference):
        return False
    return re.search(rf'(?<!\w){re.escape(reference)}(?!\w)', text, re.IGNORECASE) is not None


def _active_contracts(store):
    return [(document, _load(store, document)) for document in store.db.execute(
        "SELECT d.* FROM classification_documents d JOIN contract_metadata m ON m.document_id=d.id "
        "WHERE d.kind='contract' AND d.status='confirmed' ORDER BY d.id")
        if not set(json.loads(document['warnings'])) & _EXCLUDED_DOCUMENT_WARNINGS]


def _candidate_rows(store, contract, document):
    query = (
        'SELECT t.*,c.counterparty,c.description,COALESCE(o.revision,0) AS revision '
        'FROM transactions t JOIN accounts a ON a.id=t.account_id '
        'LEFT JOIN transaction_context c USING(account_id,external_id) '
        'LEFT JOIN classification_overrides o USING(account_id,external_id) '
        "LEFT JOIN category_catalog cat ON cat.id=o.category_id "
        "WHERE t.currency='EUR' AND CAST(t.amount AS REAL)<0 AND t.date>=? "
        "AND a.kind!='CASH' AND (cat.transaction_type IS NULL OR cat.transaction_type='expense') "
        f'AND NOT COALESCE({sql_transfer_predicate(context_alias="c")},0) ')
    params = [contract['valid_from']]
    if contract['valid_to'] is not None:
        query += 'AND t.date<=? '
        params.append(contract['valid_to'])
    rows = []
    for row in store.db.execute(query + 'ORDER BY t.date,t.account_id,t.external_id', params):
        phase = _phase_on(contract, row['date'])
        if (phase is None or -money(row['amount']) != money(phase['amount'])
                or not document_recipient_matches(document['vendor'], row['counterparty'],
                                                  row['description'], document['title'])):
            continue
        text = f"{row['counterparty'] or ''} {row['description'] or ''} {row['category']}"
        if _EXCLUDED.search(text) or _cash_withdrawal_evidence(
                store, row['account_id'], row['external_id']):
            continue
        if store.db.execute(
                'SELECT 1 FROM bonsy_cash_allocations WHERE account_id=? AND external_id=? LIMIT 1',
                (row['account_id'], row['external_id'])).fetchone():
            continue
        rows.append((row, phase))
    return rows


def _matches(store, document):
    contract = _load(store, document)
    if not contract['configured'] or document['status'] != 'confirmed':
        return [], 'contract_not_configured_or_unconfirmed'
    if not contract['phases']:
        return [], 'contract_phases_missing'
    if set(json.loads(document['warnings'])) & _EXCLUDED_DOCUMENT_WARNINGS:
        return [], 'contract_document_not_eligible'
    active = _active_contracts(store)
    suitable = _candidate_rows(store, contract, document)
    suggestions = []
    for row, phase in suitable:
        key = (row['account_id'], row['external_id'])
        # An invoice may describe the same payment; contract evidence adds no
        # allocation or cash flow. Only an existing contract blocks a proposal.
        if store.db.execute(
                'SELECT 1 FROM classification_document_links l '
                'JOIN classification_documents d ON d.id=l.document_id '
                "WHERE l.account_id=? AND l.external_id=? AND d.kind='contract' LIMIT 1",
                key).fetchone() or _document_link_rejected(store, *key, document['id']):
            continue
        text = f"{row['counterparty'] or ''} {row['description'] or ''}"
        reference_proven = _reference_present(contract['reference'], text)
        competing = []
        for other_document, other in active:
            if (other_document['id'] != document['id']
                    and _phase_on(other, row['date']) is not None
                    and document_recipient_matches(other_document['vendor'], row['counterparty'],
                                                   row['description'], other_document['title'])
                    and (not reference_proven or _reference_present(other['reference'], text))):
                competing.append(other_document['id'])
        # Repetition needs another booking in a different scheduled month of
        # this phase on the same account. Same-day duplicates do not prove it.
        repeated = any(
            other['account_id'] == row['account_id']
            and other['date'][:7] != row['date'][:7] and other_phase == phase
            and not _document_link_rejected(store, other['account_id'], other['external_id'], document['id'])
            and not store.db.execute(
                'SELECT 1 FROM classification_document_links l '
                'JOIN classification_documents d ON d.id=l.document_id '
                'WHERE l.account_id=? AND l.external_id=? '
                "AND d.kind='contract' AND l.document_id!=? LIMIT 1",
                (other['account_id'], other['external_id'], document['id'])).fetchone()
            and (not contract['reference'] or _reference_present(contract['reference'],
                 f"{other['counterparty'] or ''} {other['description'] or ''}"))
            for other, other_phase in suitable)
        reasons = ['Anbieter, Centbetrag und aktiver Kalendermonat passen zur Vertragsphase.']
        uncertainties = ['Der genaue Abbuchungstag ist nicht belegt.']
        if reference_proven:
            reasons.append('Die spezifische Vertragsreferenz steht im Buchungstext.')
        elif contract['reference']:
            uncertainties.append('Die gespeicherte Vertragsreferenz ist im Buchungstext nicht belegt.')
        if repeated:
            reasons.append('Eine weitere passende Buchung in einem anderen Phasenmonat liegt vor.')
        else:
            uncertainties.append('Eine wiederholte Zahlung für diese Phase ist nicht belegt.')
        if competing:
            uncertainties.append('Mehrere aktive Verträge desselben Anbieters kommen infrage.')
        confidence = ('high' if repeated and reference_proven and not competing else
                      'medium' if repeated and not competing and not contract['reference'] else 'low')
        suggestions.append({
            **{name: row[name] for name in ('account_id', 'external_id', 'date', 'amount',
                                          'currency', 'counterparty', 'description', 'revision')},
            'document_revision': document['revision'], 'expected_amount': phase['amount'],
            'phase': phase, 'confidence': confidence, 'reasons': reasons,
            'uncertainties': uncertainties, 'competing_document_ids': competing,
            'require_confirmation': True,
        })
    return suggestions, 'manual_review_required' if suggestions else 'no_candidates'


def matches(store, data):
    if (not isinstance(data, dict) or set(data) != {'document_id', 'page'}
            or type(data['page']) is not int or data['page'] < 0):
        raise ValueError('invalid_contract_matches_request')
    document = _document(store, data['document_id'])
    suggestions, status = _matches(store, document)
    start = data['page'] * _PAGE_SIZE
    notes = list(_LIMIT_NOTES)
    if status == 'contract_phases_missing':
        notes.append('Ohne bekannte Ratenphasen können keine Zahlungen vorgeschlagen werden.')
    return {'document_id': document['id'], 'revision': document['revision'],
            'suggestions': suggestions[start:start + _PAGE_SIZE], 'total': len(suggestions),
            'page': data['page'], 'pages': (len(suggestions) + _PAGE_SIZE - 1) // _PAGE_SIZE,
            'match_status': status, 'limit_notes': notes}


def link(store, data):
    """Confirm a current suggestion using the signed ledger amount as expectation."""
    required = {'document_id', 'document_revision', 'account_id', 'external_id', 'revision',
                'expected_date', 'expected_amount', 'confirmed'}
    if not isinstance(data, dict) or set(data) != required or data['confirmed'] is not True:
        raise ValueError('explicit_contract_link_confirmation_required')
    document_revision = _revision(data['document_revision'])
    revision = _revision(data['revision'])
    account_id = _text(data['account_id'], 'account_id', 120)
    external_id = _text(data['external_id'], 'external_id', 240)
    expected_date = _filter_date(data['expected_date'], 'expected_date')
    expected_amount = parse_money_input(data['expected_amount'])
    store.db.execute('BEGIN IMMEDIATE')
    try:
        document = _document(store, data['document_id'])
        if document['revision'] != document_revision:
            raise ValueError('stale_document_revision')
        candidates, _ = _matches(store, document)
        candidate = next((row for row in candidates if row['account_id'] == account_id
                          and row['external_id'] == external_id), None)
        if candidate is None:
            raise ValueError('contract_candidate_no_longer_eligible')
        if candidate['revision'] != revision:
            raise ValueError('stale_transaction_revision')
        if candidate['date'] != expected_date or money(candidate['amount']) != expected_amount:
            raise ValueError('stale_transaction_values')
        result = {'document_id': document['id'], 'account_id': account_id,
                  'external_id': external_id, 'allocated_amount': format(abs(money(candidate['amount'])), '.2f'),
                  'allocation_type': 'evidence'}
        store.db.execute(
            'INSERT INTO classification_document_links(account_id,external_id,document_id,'
            'allocated_amount,allocation_type) VALUES (?,?,?,?,?)',
            (account_id, external_id, document['id'], result['allocated_amount'], 'evidence'))
        _audit(store, 'contract_linked', result, account_id, external_id, document['id'])
        store.db.commit()
    except Exception:
        store.db.rollback()
        raise
    return {'link': result}
