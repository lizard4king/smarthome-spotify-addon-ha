"""Additional explicit-review candidates from the complete, read-only catalogue.

This path never supplies automatic forecast schedules. Existing decisions and
contract boundaries stay authoritative even when strict detection found nothing.
"""
import hashlib
import json
from datetime import date

from . import booking_contracts, contracts
from .classification_rules import is_intermediary
from .core import money
from .recurring_detection import _party_key, _REVERSAL, _SETTLEMENT


def _keys(rows):
    return {(row['account_id'], row['external_id']) for row in rows}


def additions(store, scope, data, catalogue_rows):
    """Return review-only candidates; the caller must still validate decisions."""
    cutoff = min(scope['as_of'], date.today().isoformat())
    accounts = {row['id']: dict(row) for row in store.db.execute('SELECT * FROM accounts')}
    members = {(row['account_id'], row['external_id'])
               for row in store.db.execute('SELECT account_id,external_id FROM booking_contract_links')}
    rejected = set()
    for row in store.db.execute(
            "SELECT payload FROM payment_review_decisions WHERE kind='recurring' AND decision='reject'"):
        rejected.update(_keys(json.loads(row['payload']).get('candidate', {}).get('bookings', [])))
    result = []
    context_cache = {}  # Private to this read/decision; never retained across requests.
    for series in catalogue_rows:
        history = sorted(series.get('history', []), key=lambda row: (row['date'], row['external_id']))
        account = accounts.get(series['account_id'])
        interval, day, amount = (series.get(key) for key in ('interval_months', 'day_of_month', 'amount'))
        if (series.get('candidate_ids') or series['status'] != 'unclassified'
                or series.get('is_transfer') or account is None or account['kind'] == 'CASH'
                or len(history) < 2 or type(interval) is not int or not 1 <= interval <= 120
                or type(day) is not int or not 1 <= day <= 31 or amount is None
                or any(row['date'] > cutoff for row in history)
                or (scope['account_id'] and scope['account_id'] != series['account_id'])
                or _keys(history) & (members | rejected)):
            continue
        recent = history[-3:]
        parties = {_party_key(row['counterparty']) for row in recent}
        if (len({row['date'][:7] for row in recent}) != len(recent)
                or len(parties) != 1 or not next(iter(parties))
                or any(row['account_id'] != series['account_id'] or row['currency'] != series['currency']
                       for row in history)
                or any(is_intermediary(row['counterparty'], row['description'])
                       or any(pattern.search(row['counterparty'] + ' ' + row['description'])
                              for pattern in (contracts._EXCLUDED, _REVERSAL, _SETTLEMENT))
                       for row in history)):
            continue
        party = next(iter(parties))
        if any(schedule.get('basis') != 'auto_detected'
               and schedule.get('account_id') in (None, series['account_id'])
               and schedule.get('currency') in (None, series['currency'])
               and (_party_key(schedule['label']) == party
                    or any(contracts._reference_present(schedule.get('reference', ''),
                           row['counterparty'] + ' ' + row['description']) for row in history))
               for schedule in data.get('schedules', [])):
            continue
        identities = [{key: row[key] for key in ('account_id', 'external_id')} for row in recent]
        try:
            derivation = booking_contracts.preview(store, {
                **identities[-1], 'method': 'median', 'bookings': identities}, _context_cache=context_cache)
        except ValueError:
            # The shared validator rejects transfers, reversals, cash and
            # authoritative document/credit context. The catalogue stays visible.
            continue
        if money(derivation['amount']) != money(amount):
            # A monthly total is a display assumption, never a payment estimate.
            continue
        result.append({
            'id': 'review:' + hashlib.sha256(series['id'].encode()).hexdigest()[:20],
            'label': series['label'][:160], 'account_id': series['account_id'],
            'currency': series['currency'], 'amount': derivation['amount'],
            'day_of_month': day, 'interval_months': interval,
            'valid_from': recent[0]['date'], 'valid_to': None,
            'source': 'booking_history_review', 'source_refs': [],
            'bookings': [{key: row[key] for key in ('account_id', 'external_id', 'date', 'amount')}
                         for row in recent],
            'status': 'needs_review', 'warnings': ['ambiguous_source_history', 'end_date_unknown'],
            'estimated': True, 'covered_by': [],
            'derivation': {'amount_method': 'median', 'sample_count': len(recent),
                           'rounding': 'ROUND_HALF_UP', 'date_method': 'median_day', 'as_of': cutoff},
        })
    return result
