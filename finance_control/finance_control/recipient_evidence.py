"""Add operator-reviewed card-statement recipients without changing the ledger.

No document is fetched and no name is inferred. Repeated bookings are eligible
only when equivalent reviewed statement rows can cover all candidate bookings.
"""
import hashlib
import json
import re
from datetime import UTC, date, datetime

from .audit import actor_for
from .core import money

_FIELDS = {'source_id', 'source_reference', 'source_sha256', 'page',
           'document_date', 'receipt_date', 'booking_date', 'amount', 'currency',
           'recipient', 'source_text'}
_DECISIONS = ('eligible', 'already_present', 'already_named', 'unmatched',
              'ambiguous', 'conflict')
_GENERIC = {'kreditkartenumsatz', 'kartenumsatz', 'kartenzahlung', 'abrechnung karte'}
_CONTEXT_QUERY = (
    'SELECT t.*,c.counterparty,c.description FROM active_transactions t '
    'LEFT JOIN transaction_context c USING(account_id,external_id) ')


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def _digest(value):
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def _text(value, name, maximum):
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or any(ord(c) < 32 and c not in '\n\r\t' for c in value)):
        raise ValueError(f'invalid_{name}')
    return value.strip()


def _date(value, name):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value):
        raise ValueError(f'invalid_{name}')
    try:
        date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f'invalid_{name}') from error
    return value


def _validate(store, data):
    if not isinstance(data, dict) or set(data) - {'account_id', 'rows', 'confirmed', 'preview_digest'}:
        raise ValueError('invalid_recipient_evidence')
    account = _text(data.get('account_id'), 'account_id', 64)
    account_row = store.db.execute('SELECT kind FROM accounts WHERE id=?', (account,)).fetchone()
    if account_row is None:
        raise ValueError('unknown_account')
    if account_row['kind'] != 'CREDIT_CARD':
        raise ValueError('wrong_account_kind')
    rows = data.get('rows')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 500:
        raise ValueError('invalid_rows')
    normalized = []
    source_ids = set()
    for raw in rows:
        if not isinstance(raw, dict) or set(raw) != _FIELDS:
            raise ValueError('invalid_statement_row')
        row = {key: _text(raw[key], key, limit) for key, limit in (
            ('source_id', 160), ('source_reference', 512), ('source_sha256', 64),
            ('recipient', 240), ('source_text', 1000), ('currency', 3))}
        if row['source_id'] in source_ids:
            raise ValueError('duplicate_source_id')
        source_ids.add(row['source_id'])
        if (not re.fullmatch(r'https://drive\.google\.com/file/d/[A-Za-z0-9_-]+/view',
                             row['source_reference'])
                or row['source_reference'] != raw['source_reference']):
            raise ValueError('invalid_source_reference')
        if not re.fullmatch(r'[0-9a-f]{64}', raw['source_sha256']):
            raise ValueError('invalid_source_sha256')
        if not re.fullmatch(r'[A-Z]{3}', row['currency']):
            raise ValueError('invalid_currency')
        if type(raw['page']) is not int or not 1 <= raw['page'] <= 100000:
            raise ValueError('invalid_page')
        row['page'] = raw['page']
        for field in ('document_date', 'receipt_date', 'booking_date'):
            row[field] = _date(raw[field], field)
        amount = raw['amount']
        if not isinstance(amount, str) or not re.fullmatch(r'-?(?:0|[1-9][0-9]*)\.[0-9]{2}', amount):
            raise ValueError('invalid_amount')
        row['amount'] = format(money(amount), '.2f')
        if row['amount'] == '-0.00':
            raise ValueError('invalid_amount')
        normalized.append(row)
    return account, normalized


def _fingerprint(row):
    # Includes financial/source fields so an independently revised booking can
    # never silently inherit evidence from its predecessor.
    return _digest(dict(row))


def _generic(value):
    # Callers validate CREDIT_CARD before reconciliation. A missing/blank
    # source name is also eligible, without creating source context.
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    lines = [line.strip() for line in value.strip().splitlines() if line.strip()]
    if not lines:
        return True
    if ' '.join(lines[0].casefold().split()) not in _GENERIC:
        return False
    # Only a postal-code/city suffix is accepted after the generic label.
    return len(lines) == 1 or (len(lines) == 2 and re.fullmatch(
        r'[0-9]{3,10}\s+[^\W\d_][\w .\-/]*(?:,\s*[A-Z]{2,3},\s*[A-Z]{2,3})?',
        lines[1], re.UNICODE) is not None)


def _projection(row):
    result = {key: row[key] for key in ('recipient', 'source_reference', 'source_sha256',
                                      'page', 'document_date', 'source_text')}
    result['pages'] = sorted({entry['page'] for entry in row.get('rows', [row])})
    return result


def _covers_targets(target_ids, by_target):
    """Prove an injective target-to-source matching; never expose that assignment."""
    assigned = {}

    def augment(target, visited_sources):
        for source in sorted(by_target[target]):
            if source in visited_sources:
                continue
            visited_sources.add(source)
            previous = assigned.get(source)
            if previous is None or augment(previous, visited_sources):
                assigned[source] = target
                return True
        return False

    return all(augment(target, set()) for target in sorted(target_ids))


def _build(store, account, rows):
    ledger = [dict(row) for row in store.db.execute(
        _CONTEXT_QUERY + 'WHERE t.account_id=? ORDER BY t.external_id', (account,))]
    existing = {row['external_id']: dict(row) for row in store.db.execute(
        'SELECT * FROM recipient_evidence WHERE account_id=? ORDER BY external_id', (account,))}
    candidates = [[item for item in ledger if item['currency'] == row['currency']
                   and money(item['amount']) == money(row['amount'])
                   and item['date'] in {row['receipt_date'], row['booking_date']}]
                  for row in rows]
    by_target = {}
    for index, matches in enumerate(candidates):
        for target in matches:
            by_target.setdefault(target['external_id'], set()).add(index)
    items = [{'source_id': row['source_id'], 'recipient': row['recipient'],
              'matches': [{key: match[key] for key in
                           ('account_id', 'external_id', 'date', 'amount', 'currency')}
                          for match in matches], 'targets': [], 'decision': 'unmatched'}
             for row, matches in zip(rows, candidates)]
    targets = {target['external_id']: target for target in ledger}
    visited = set()
    inserts = {}
    for index, matches in enumerate(candidates):
        if index in visited or not matches:
            continue
        # Traverse the complete bipartite connected component. Looking only at
        # one source row would miss competing names on overlapping date sets.
        related = set()
        target_ids = set()
        pending = [index]
        while pending:
            source = pending.pop()
            if source in related:
                continue
            related.add(source)
            for target in candidates[source]:
                external_id = target['external_id']
                if external_id not in target_ids:
                    target_ids.add(external_id)
                    pending.extend(by_target[external_id] - related)
        visited.update(related)
        equivalent = (
            len(related) >= len(target_ids)
            and len({(rows[i]['recipient'], rows[i]['source_reference'],
                      rows[i]['source_sha256'], rows[i]['document_date'])
                     for i in related}) == 1
            and _covers_targets(target_ids, by_target))
        decision = 'ambiguous'
        statuses = {external_id: 'ambiguous' for external_id in target_ids}
        if equivalent:
            evidence_rows = sorted((rows[i] for i in related), key=lambda row: row['source_id'])
            evidence = {'rows': evidence_rows,
                        **_projection({**evidence_rows[0], 'rows': evidence_rows})}
            # Retain the complete component, without claiming a particular
            # one-to-one booking assignment. Each input text remains <=1000.
            evidence['source_text'] = '\n'.join(dict.fromkeys(
                entry['source_text'] for entry in evidence_rows))
            if (len(evidence['source_text']) > 8000
                    or len(_json(evidence).encode('utf-8')) > 32768):
                for source in related:
                    items[source].update(
                        decision='ambiguous', reason='evidence_group_too_large',
                        targets=[{**{key: targets[external_id][key] for key in
                                     ('account_id', 'external_id', 'date', 'amount',
                                      'currency', 'counterparty')}, 'decision': 'ambiguous'}
                                 for external_id in sorted(target_ids)])
                continue
            statuses = {}
            for external_id in sorted(target_ids):
                target = targets[external_id]
                prior = existing.get(external_id)
                if prior is not None:
                    statuses[external_id] = 'already_present' if (
                        prior['context_fingerprint'] == _fingerprint(target)
                        and prior['evidence_json'] == _json(evidence)) else 'conflict'
                else:
                    statuses[external_id] = ('eligible' if _generic(target['counterparty'])
                                             else 'already_named')
            if 'conflict' in statuses.values():
                decision = 'conflict'
                # A conflict blocks the entire component. Do not advertise
                # another generic target as insertable when it will be skipped.
                statuses = {key: 'conflict' if status == 'eligible' else status
                            for key, status in statuses.items()}
            elif 'eligible' in statuses.values():
                decision = 'eligible'
                for external_id, status in statuses.items():
                    if status == 'eligible':
                        inserts[external_id] = (targets[external_id], evidence)
            elif 'already_present' in statuses.values():
                decision = 'already_present'
            else:
                decision = 'already_named'
            if len(related) > 1 or len(target_ids) > 1:
                for source in related:
                    items[source]['recipient_equivalent_group'] = True
                    if len(related) > len(target_ids):
                        items[source]['surplus_statement_rows'] = len(related) - len(target_ids)
        for source in related:
            items[source]['decision'] = decision
            items[source]['targets'] = [
                {**{key: targets[external_id][key] for key in
                    ('account_id', 'external_id', 'date', 'amount', 'currency', 'counterparty')},
                 'decision': statuses[external_id]}
                for external_id in sorted(target_ids)]
    counts = {decision: sum(item['decision'] == decision for item in items)
              for decision in _DECISIONS}
    result = {'items': items, 'eligible_count': len(inserts), 'counts': counts}
    # Bind confirmation to every matching ledger/context and existing evidence,
    # including non-visible context changes and newly ambiguous matches.
    result['preview_digest'] = _digest({'account_id': account, 'rows': rows,
                                        'ledger': ledger, 'existing': existing,
                                        'result': result})
    return result, inserts


def preview(store, data):
    """Validate supplied evidence and return a read-only reconciliation preview."""
    return _build(store, *_validate(store, data))[0]


def commit(store, data):
    """Atomically insert only explicitly confirmed, unchanged preview evidence."""
    account, rows = _validate(store, data)
    if data.get('confirmed') is not True:
        raise ValueError('recipient_evidence_confirmation_required')
    supplied_digest = data.get('preview_digest')
    if not isinstance(supplied_digest, str) or not re.fullmatch(r'[0-9a-f]{64}', supplied_digest):
        raise ValueError('invalid_preview_digest')
    store.db.execute('BEGIN IMMEDIATE')
    try:
        result, inserts = _build(store, account, rows)
        if result['preview_digest'] != supplied_digest:
            raise ValueError('stale_recipient_preview')
        timestamp = datetime.now(UTC).isoformat()
        actor = actor_for(store)
        for external_id, (target, evidence) in inserts.items():
            store.db.execute(
                'INSERT INTO recipient_evidence VALUES (?,?,?,?,?,?,?)',
                (account, external_id, _fingerprint(target), evidence['recipient'],
                 _json(evidence), timestamp, actor))
        store.db.execute(
            'INSERT INTO recipient_evidence_audit '
            '(preview_digest,actor,created_at,inserted_count,financial_data_unchanged) '
            'VALUES (?,?,?,?,1)', (supplied_digest, actor, timestamp, len(inserts)))
        store.db.commit()
        return {**result, 'inserted_count': len(inserts), 'saved': len(inserts),
                'financial_data_unchanged': True}
    except Exception:
        store.db.rollback()
        raise


def attach(store, rows):
    """Return copied presentation rows with valid evidence, never persist changes."""
    result = [dict(raw) for raw in rows]
    keys = list(dict.fromkeys((row['account_id'], row['external_id']) for row in result))
    overlays = {}
    for offset in range(0, len(keys), 200):
        chunk = keys[offset:offset + 200]
        placeholders = ','.join('(?,?)' for _ in chunk)
        evidence_by_key = {
            (row['account_id'], row['external_id']): row for row in store.db.execute(
                'SELECT * FROM recipient_evidence WHERE (account_id,external_id) IN '
                f'({placeholders})', [value for key in chunk for value in key])}
        if not evidence_by_key:
            continue
        matched_keys = list(evidence_by_key)
        placeholders = ','.join('(?,?)' for _ in matched_keys)
        for original in store.db.execute(
                _CONTEXT_QUERY + f'WHERE (t.account_id,t.external_id) IN ({placeholders})',
                [value for key in matched_keys for value in key]):
            key = (original['account_id'], original['external_id'])
            evidence = evidence_by_key[key]
            if _fingerprint(original) == evidence['context_fingerprint']:
                overlays[key] = _projection(json.loads(evidence['evidence_json']))
    for row in result:
        row.pop('recipient_evidence', None)
        key = (row['account_id'], row['external_id'])
        if key in overlays:
            row['recipient_evidence'] = dict(overlays[key])
    return result
