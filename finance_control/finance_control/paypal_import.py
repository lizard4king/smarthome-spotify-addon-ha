"""Strict, network-free PayPal booking normalization and explicit legacy adoption."""
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
import secrets

from .audit import validated_actor
from .bank_source_policy import _ledger_fingerprint
from .finanzguru_snapshot import _dependent_state, _event
from .ledger import insert_transaction, void_transaction


_FIELDS = ('entry_reference', 'date', 'amount', 'currency', 'counterparty', 'description')
_LEGACY = re.compile(r'fg:[0-9a-f]{64}:[0-9a-f]{64}\Z')
_SOURCE = re.compile(r'paypal:[0-9a-f]{64}\Z')


def _is_paypal_institution(value):
    """Match the exact PayPal label using ASCII case-insensitive comparison."""
    return isinstance(value, str) and value.isascii() and value.lower() == 'paypal'


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _text(value, limit, *, required=False, multiline=False):
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise ValueError('invalid_paypal_text')
    if any(not c.isprintable() and not (multiline and c in '\n\r\t') for c in value):
        raise ValueError('invalid_paypal_text')
    return value


def _day(value):
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    if not isinstance(value, str):
        raise ValueError('invalid_paypal_booking_date')
    try:
        result = date.fromisoformat(value).isoformat()
    except ValueError:
        raise ValueError('invalid_paypal_booking_date') from None
    if result != value:
        raise ValueError('invalid_paypal_booking_date')
    return result


def _amount(value):
    if isinstance(value, (float, bool)):
        raise ValueError('invalid_paypal_amount')
    try:
        result = Decimal(value)
        if not result.is_finite() or abs(result) >= Decimal('1000000000000') or result != result.quantize(Decimal('.01')):
            raise ValueError('invalid_paypal_amount')
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError('invalid_paypal_amount') from None
    return result


def normalize_transactions(raw, start, end, currency='EUR'):
    """Accept BOOK rows, retaining transaction-date provenance when no booking date exists."""
    start, end = _day(start), _day(end)
    if start > end or currency not in {'EUR', 'XXX'} or not isinstance(raw, list) or len(raw) > 10000:
        raise ValueError('invalid_paypal_batch')
    result, seen = [], set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError('invalid_paypal_transaction')
        status = item.get('status')
        if status in {'PDNG', 'CNCL', 'HOLD', 'RJCT', 'SCHD', 'OTHR'}:
            continue
        if status != 'BOOK':
            raise ValueError('unknown_paypal_booking_status')
        reference = _text(item.get('entry_reference'), 256, required=True)
        if reference != reference.strip() or reference in seen:
            raise ValueError('invalid_paypal_entry_reference')
        seen.add(reference)
        # Only absent/NULL booking dates permit the explicitly labelled PayPal
        # transaction date. Empty/malformed dates never fall back; timestamps
        # and value dates are never interpreted as calendar booking dates.
        transaction_date = item.get('booking_date') is None
        booked = _day(item.get('transaction_date') if transaction_date else item['booking_date'])
        if not start <= booked <= end:
            raise ValueError('paypal_booking_outside_period')
        amount = item.get('transaction_amount')
        if not isinstance(amount, dict):
            raise ValueError('invalid_paypal_amount')
        unit = amount.get('currency')
        if not isinstance(amount.get('amount'), str):
            raise ValueError('invalid_paypal_amount')
        amount = _amount(amount.get('amount'))
        if unit != 'EUR':
            raise ValueError('unsupported_paypal_currency')
        direction = item.get('credit_debit_indicator')
        if direction not in {'CRDT', 'DBIT'} or (direction == 'CRDT' and amount < 0):
            raise ValueError('invalid_paypal_credit_debit_indicator')
        amount = -abs(amount) if direction == 'DBIT' else amount
        if not amount:
            amount = Decimal('0.00')
        description = item.get('remittance_information')
        if description is None:
            description = []
        if not isinstance(description, list) or len(description) > 1000:
            raise ValueError('invalid_paypal_remittance')
        description = '\n'.join(_text(part, 8000, multiline=True) for part in description)
        party = item.get('creditor' if direction == 'DBIT' else 'debtor')
        if party is None:
            party = {}
        if not isinstance(party, dict):
            raise ValueError('invalid_paypal_counterparty')
        code = item.get('bank_transaction_code')
        if code is None:
            code = {}
        if not isinstance(code, dict):
            raise ValueError('invalid_paypal_transaction_code')
        code_description = _text('' if code.get('description') is None else code['description'], 1000, multiline=True)
        description = '\n'.join(part for part in (description, code_description) if part)
        result.append(dict(entry_reference=reference, date=booked, amount=format(amount, '.2f'),
                           currency=unit, counterparty=_text('' if party.get('name') is None else party['name'], 500),
                           description=_text(description, 8000, multiline=True)))
        if transaction_date:
            result[-1]['date_basis'] = 'transaction_date'
    return sorted(result, key=lambda row: (row['date'], row['entry_reference']))


def _rows(rows):
    if not isinstance(rows, list) or len(rows) > 10000:
        raise ValueError('invalid_paypal_batch')
    result, seen = [], set()
    for row in rows:
        if (not isinstance(row, dict)
                or set(row) not in (set(_FIELDS), set(_FIELDS) | {'date_basis'})
                or ('date_basis' in row and row['date_basis'] != 'transaction_date')):
            raise ValueError('invalid_normalized_paypal_row')
        reference = _text(row['entry_reference'], 256, required=True)
        if reference.strip() != reference or reference in seen or row['currency'] != 'EUR':
            raise ValueError('invalid_normalized_paypal_row')
        seen.add(reference)
        result.append(dict(entry_reference=reference, date=_day(row['date']),
                           amount=format(_amount(row['amount']), '.2f'), currency='EUR',
                           counterparty=_text(row['counterparty'], 500),
                           description=_text(row['description'], 8000, multiline=True)))
        if 'date_basis' in row:
            result[-1]['date_basis'] = row['date_basis']
    return sorted(result, key=lambda row: (row['date'], row['entry_reference']))


def _binding(store, owner_id, source_key, account_id, rows):
    if not isinstance(source_key, str) or not _SOURCE.fullmatch(source_key):
        raise ValueError('invalid_paypal_source_key')
    owner = store.db.execute('SELECT * FROM app_users WHERE id=? AND active=1', (owner_id,)).fetchone()
    account = store.db.execute('SELECT * FROM accounts WHERE id=?', (account_id,)).fetchone()
    if owner is None or account is None or not _is_paypal_institution(account['institution']) or account['kind'] != 'CHECKING' or account['currency'] != 'EUR':
        raise ValueError('invalid_paypal_target')
    if account['closed_on'] is not None or any(row['date'] <= account['opening_date'] for row in rows):
        raise ValueError('invalid_paypal_account_lifecycle')
    bindings = [dict(row) for row in store.db.execute(
        'SELECT * FROM paypal_source_accounts WHERE source_key=? OR account_id=?', (source_key, account_id))]
    if any(row['source_key'] != source_key or row['account_id'] != account_id or row['owner_id'] != owner_id for row in bindings):
        raise ValueError('paypal_source_binding_conflict')
    for table in ('bank_source_accounts', 'bank_statement_account_bindings', 'server_bank_refresh_bindings'):
        if store.db.execute(f'SELECT 1 FROM {table} WHERE account_id=?', (account_id,)).fetchone():
            raise ValueError('paypal_foreign_bank_binding')
    if not bindings and not rows:
        raise ValueError('empty_paypal_initial_import')
    return dict(owner), dict(account), bindings


def preview_import(store, *, owner_id, source_key, account_id, rows, matches):
    rows = _rows(rows)
    binding = _binding(store, owner_id, source_key, account_id, rows)
    if not isinstance(matches, dict) or set(matches) - {row['entry_reference'] for row in rows}:
        raise ValueError('invalid_paypal_matches')
    registry = [dict(row) for row in store.db.execute(
        'SELECT * FROM paypal_source_transactions WHERE source_key=? OR account_id=? ORDER BY source_key,entry_reference',
        (source_key, account_id))]
    by_ref = {row['entry_reference']: row for row in registry if row['source_key'] == source_key}
    used = {row['external_id']: row['entry_reference'] for row in registry}
    items, claimed = [], set()
    for row in rows:
        ref = row['entry_reference']
        mapped = by_ref.get(ref)
        if mapped:
            if mapped['normalized_json'] != _json(row) or mapped['account_id'] != account_id:
                raise ValueError('paypal_direct_transaction_changed')
            target = mapped['external_id']
            active = store.db.execute('SELECT * FROM active_transactions WHERE account_id=? AND external_id=?', (account_id, target)).fetchone()
            if active is None or any(active[key] != row[key] for key in ('date', 'amount', 'currency')):
                raise ValueError('paypal_registry_ledger_conflict')
            if ref in matches and matches[ref] != target:
                raise ValueError('paypal_mapping_conflict')
            decision, candidates = 'unchanged', []
        else:
            collisions = [dict(old) for old in store.db.execute(
                'SELECT t.*, COALESCE(c.counterparty,\'\') counterparty, COALESCE(c.description,\'\') description '
                'FROM active_transactions t LEFT JOIN transaction_context c USING(account_id,external_id) '
                'WHERE t.account_id=? AND t.date=? AND t.currency=? ORDER BY t.external_id',
                (account_id, row['date'], row['currency'])) if _amount(old['amount']) == _amount(row['amount'])]
            if any(not _LEGACY.fullmatch(old['external_id']) and old['external_id'] not in used for old in collisions):
                raise ValueError('paypal_foreign_source_collision')
            candidates = [{key: old[key] for key in ('external_id', 'counterparty', 'description')}
                          for old in collisions if _LEGACY.fullmatch(old['external_id']) and old['external_id'] not in used]
            # A changed provider reference is not proof of another booking.
            # Require an explicit new decision when economics collide with a
            # direct identity; an existing direct mapping is never retargeted.
            direct_collision = any(old['external_id'] in used for old in collisions)
            target = matches.get(ref)
            if ref not in matches and (candidates or direct_collision):
                decision = 'review'
            elif target is None:
                decision = 'new'
                target = 'paypal:' + _digest([source_key, ref])
                if store.db.execute('SELECT 1 FROM transaction_keys WHERE account_id=? AND external_id=?', (account_id, target)).fetchone():
                    raise ValueError('paypal_target_identity_exists')
            else:
                if not isinstance(target, str) or target not in {old['external_id'] for old in candidates}:
                    raise ValueError('invalid_paypal_legacy_match')
                decision = 'adopted'
        if decision != 'review':
            if target in claimed:
                raise ValueError('duplicate_paypal_target_match')
            claimed.add(target)
        items.append(row | {'external_id': target, 'candidates': candidates, 'decision': decision})
    # Ledger transfer peers can live outside the PayPal account. Bind their
    # complete ledger and dependencies as well as correction-pair dependencies.
    peers = sorted({old['account_id'] for old in store.db.execute(
        "SELECT DISTINCT account_id FROM active_transactions WHERE transfer_id != '' AND transfer_id IN "
        "(SELECT transfer_id FROM active_transactions WHERE account_id=? AND transfer_id != '')",
        (account_id,))} - {account_id})
    transfer_ledgers = {peer: _ledger_fingerprint(store, peer, '0001-01-01', '9999-12-31') for peer in peers}
    revision = _digest({'rows': rows, 'matches': matches, 'binding': binding, 'registry': registry,
                        'transfer_ledgers': transfer_ledgers, 'ledger': _ledger_fingerprint(store, account_id, '0001-01-01', '9999-12-31')})
    return {'items': items, 'inserted': sum(item['decision'] == 'new' for item in items),
            'adopted': sum(item['decision'] == 'adopted' for item in items),
            'unchanged': sum(item['decision'] == 'unchanged' for item in items),
            'unmatched_legacy': sum(
                _LEGACY.fullmatch(old['external_id']) is not None
                and old['external_id'] not in used and old['external_id'] not in claimed
                for old in store.db.execute('SELECT external_id FROM active_transactions WHERE account_id=?', (account_id,))),
            'revision': revision, 'ready': all(item['decision'] != 'review' for item in items)}


def commit_import(store, *, owner_id, source_key, account_id, rows, matches, revision, actor, guard=None):
    actor = validated_actor(actor)
    if store.db.in_transaction:
        raise ValueError('paypal_import_requires_idle_transaction')
    previous_actor = getattr(store, 'audit_actor', 'system:local')
    store.db.execute('BEGIN IMMEDIATE')
    try:
        store.audit_actor = actor
        if guard is not None:
            guard(store.db)
        plan = preview_import(store, owner_id=owner_id, source_key=source_key, account_id=account_id, rows=rows, matches=matches)
        if not isinstance(revision, str) or not secrets.compare_digest(plan['revision'], revision):
            raise ValueError('stale_paypal_preview')
        if not plan['ready']:
            raise ValueError('paypal_legacy_review_required')
        now = datetime.now(UTC).isoformat()
        manifest = _json({'kind': 'paypal_direct_import', 'source_key': source_key, 'rows': _rows(rows),
                          'matches': matches, 'revision': revision, 'nonce': secrets.token_hex(16)})
        import_id = store.db.execute('INSERT INTO imports(digest,imported_at,source) VALUES (?,?,?)',
            (hashlib.sha256(manifest.encode()).hexdigest(), now, manifest)).lastrowid
        store.db.execute('INSERT OR IGNORE INTO paypal_source_accounts(source_key,owner_id,account_id) VALUES (?,?,?)',
                         (source_key, owner_id, account_id))
        for item in plan['items']:
            if item['decision'] == 'unchanged':
                continue
            key = (account_id, item['external_id'])
            transfer = ''
            category = 'Bankabruf'
            if item['decision'] == 'adopted':
                old = store.db.execute('SELECT * FROM active_transaction_versions WHERE account_id=? AND external_id=?', key).fetchone()
                transfer = old['transfer_id']
                category = old['category']
                event_id = _event(store, source=source_key, target=account_id, exported_at=now,
                    sha=revision, action='updated', key=key,
                    previous={'transaction': dict(old), 'dependencies': _dependent_state(store, key)}, current=item)
                void_transaction(store, key, event_id, 'superseded')
            insert_transaction(store, (*key, item['date'], item['amount'], item['currency'], category, transfer, import_id))
            store.db.execute('INSERT INTO transaction_context(account_id,external_id,counterparty,description) VALUES (?,?,?,?) '
                'ON CONFLICT(account_id,external_id) DO UPDATE SET counterparty=excluded.counterparty,description=excluded.description',
                (*key, item['counterparty'], item['description']))
            # Optional provider names can be empty or exceed the classification
            # rule limit (240 stripped characters). Preserve the full source
            # context and simply omit automatic classification in those cases.
            if item['decision'] == 'new' and 0 < len(item['counterparty'].strip()) <= 240:
                from .classification import apply_exact_rule_if_safe
                apply_exact_rule_if_safe(store, *key)
            store.db.execute('INSERT INTO paypal_source_transactions(source_key,entry_reference,account_id,external_id,normalized_json) VALUES (?,?,?,?,?)',
                (source_key, item['entry_reference'], *key, _json({field: item[field] for field in (*_FIELDS, 'date_basis') if field in item})))
        store.db.execute('INSERT INTO paypal_import_receipts(source_key,account_id,owner_id,import_id,occurred_at,actor,revision,receipt_json) VALUES (?,?,?,?,?,?,?,?)',
            (source_key, account_id, owner_id, import_id, now, actor, revision, _json(plan)))
        store.db.commit()
        return plan
    except BaseException:
        store.db.rollback()
        raise
    finally:
        store.audit_actor = previous_actor
