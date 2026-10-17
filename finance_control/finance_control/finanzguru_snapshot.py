"""Explicit full-export reconciliation of the current Finanzguru ledger.

Only the source-account namespaces present in the reviewed workbook are changed.
The event journal retains prior rows and dependent decisions before replacement.
"""
import csv
import hashlib
import io
import json
import secrets
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path

from .classification import apply_exact_rule_if_safe
from .core import money
from .finanzguru_import import (digest, load_batch, selected_source_rows,
                               validate_selected_transfer_pairs, _mapped_row_metadata,
                               _persist_row_metadata, _original_identity_proofs)


_CHILD_TABLES = (
    'transaction_context', 'classification_overrides',
    'classification_document_links', 'transfer_correction_members',
    'classification_model_reviews', 'transfer_single_legs',
    'payment_mail_events', 'bonsy_cash_allocations',
    'classification_model_rejections', 'booking_contract_links',
)
# Contract decisions are planning provenance; only explicit reconciliation may
# remove or retarget them. They must never follow generic child cleanup.
_DELETE_CHILDREN = tuple(name for name in _CHILD_TABLES
                         if name not in {'payment_mail_events', 'booking_contract_links'})


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def _source_prefix(source_account):
    return 'fg:' + digest(source_account) + ':'


def _timestamp(choices):
    value = choices.get('exported_at')
    if not isinstance(value, str):
        raise ValueError('invalid_exported_at')
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError('invalid_exported_at') from error
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError('invalid_exported_at')
    return stamp.astimezone(UTC).isoformat()


def _date_to(choices, exported_at):
    value = choices.get('date_to')
    if not isinstance(value, str):
        raise ValueError('invalid_date_to')
    try:
        normalized = date.fromisoformat(value).isoformat()
    except ValueError as error:
        raise ValueError('invalid_date_to') from error
    # The export date is the calendar day in the timestamp's supplied zone,
    # even when its UTC representation falls on the previous day.
    local_export_date = datetime.fromisoformat(choices['exported_at']).date().isoformat()
    if normalized != value or normalized > local_export_date:
        raise ValueError('invalid_date_to')
    return normalized


def _dependent_state(store, key):
    state = {}
    for table in _CHILD_TABLES:
        state[table] = [dict(row) for row in store.db.execute(
            f'SELECT * FROM {table} WHERE account_id=? AND external_id=?', key)]
    return state


def _transfer_pair_state(store, key):
    """Bind the complete correction pair, including its opposite ledger leg."""
    pair_ids = [row['pair_id'] for row in store.db.execute(
        'SELECT pair_id FROM transfer_correction_members '
        'WHERE account_id=? AND external_id=?', key)]
    result = []
    for pair_id in pair_ids:
        pair = store.db.execute(
            'SELECT * FROM transfer_correction_pairs WHERE id=?', (pair_id,)).fetchone()
        members = [dict(row) for row in store.db.execute(
            'SELECT * FROM transfer_correction_members WHERE pair_id=? '
            'ORDER BY account_id,external_id', (pair_id,))]
        opposite = []
        for member in members:
            member_key = (member['account_id'], member['external_id'])
            if member_key == key:
                continue
            transaction = store.db.execute(
                'SELECT * FROM transactions WHERE account_id=? AND external_id=?',
                member_key).fetchone()
            opposite.append({'transaction': None if transaction is None else dict(transaction),
                             'dependencies': _dependent_state(store, member_key)})
        result.append({'pair': None if pair is None else dict(pair),
                       'members': members, 'opposite': opposite})
    return result


def _reviewable_transfer_dependency(store, key, old, existing, transfer_members, selected):
    """Manual approval can preserve exactly one intact cross-selection pair."""
    corrections = _transfer_pair_state(store, key)
    if old['transfer_id']:
        members = transfer_members.get(old['transfer_id'], set())
        if corrections or len(members) != 2 or key not in members:
            return None
        peer_key = next(iter(members - {key}))
        kind = 'ledger_transfer'
    else:
        if len(corrections) != 1:
            return None
        pair = corrections[0]
        if not pair['pair'] or pair['pair'].get('revoked_at') is not None or len(pair['members']) != 2:
            return None
        members = {(row['account_id'], row['external_id']) for row in pair['members']}
        if len(members) != 2 or key not in members:
            return None
        peer_key = next(iter(members - {key}))
        kind = 'confirmed_correction_pair'
    peer = existing.get(peer_key)
    if (peer_key in selected or peer is None or peer_key[0] == key[0]
            or peer['currency'] != old['currency'] or money(old['amount']) == 0
            or money(old['amount']) + money(peer['amount']) != 0):
        return None
    if ((kind == 'confirmed_correction_pair' and peer['transfer_id'])
            or (kind == 'ledger_transfer' and _transfer_pair_state(store, peer_key))):
        return None
    return kind


def _check_foreign_keys(store):
    """Never delete a booking when a new dependent table escaped reconciliation."""
    known = set(_CHILD_TABLES)
    tables = [row[0] for row in store.db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    for table in tables:
        foreign = store.db.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
        if any(row['table'] == 'transactions' for row in foreign) and table not in known:
            raise ValueError('unknown_transaction_dependency')


def _event(store, *, source, target, exported_at, sha, action, key=None,
           previous=None, current=None):
    store.db.execute(
        'INSERT INTO finanzguru_snapshot_events('
        'occurred_at,source_account,target_account,exported_at,source_sha256,'
        'action,account_id,external_id,previous,current) VALUES (?,?,?,?,?,?,?,?,?,?)',
        (datetime.now(UTC).isoformat(), source, target, exported_at, sha, action,
         None if key is None else key[0], None if key is None else key[1],
         None if previous is None else _json(previous),
         None if current is None else _json(current)))


def _source_context(store, root, existing, identity_keys=(), display_references=None):
    """Find the last accepted raw context for each still present booking."""
    baseline = {}
    proof_cache = {}
    review_proof_cache = {}
    display_cache = {}
    accounts = {row['id']: row for row in store.accounts()}
    imports = [dict(row) for row in store.db.execute(
        'SELECT id,digest,imported_at,source FROM imports ORDER BY id DESC')]
    legacy = {}
    archive = Path(root).resolve()
    if archive.exists():
        for receipt_path in archive.glob('*/decision-*.json'):
            try:
                receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
                csv_sha = receipt.get('csv_sha256')
                if not isinstance(csv_sha, str):
                    continue
                folder, staged = load_batch(archive, receipt_path.parent.name)
                if (folder != receipt_path.parent
                        or staged['source_sha256'] != receipt.get('source_sha256')):
                    continue
                legacy.setdefault(csv_sha, []).append((
                    receipt_path.stat().st_mtime, receipt, staged))
            except (OSError, ValueError, KeyError, TypeError):
                # A receipt left by a rolled-back import is not source evidence.
                continue
    for imported in imports:
        try:
            source = imported['source']
            if hashlib.sha256(source.encode()).hexdigest() != imported['digest']:
                continue
            manifest = json.loads(source)
        except (TypeError, ValueError):
            manifest = None
        if isinstance(manifest, dict) and manifest.get('kind') == 'finanzguru_full_snapshot':
            rows = manifest.get('rows')
            choices = manifest.get('choices')
            if (not isinstance(rows, list) or not isinstance(choices, dict)
                    or not isinstance(manifest.get('source_sha256'), str)):
                continue
            candidates = [(choices, rows, False, manifest['source_sha256'])]
            imported_records = None
        else:
            try:
                reader = csv.DictReader(io.StringIO(source))
                if reader.fieldnames != [
                        'external_id', 'account_id', 'date', 'amount',
                        'currency', 'category', 'transfer_id']:
                    continue
                imported_records = {(row['account_id'], row['external_id'],
                                     row['date'], row['amount'], row['currency'])
                                    for row in reader}
            except (KeyError, TypeError):
                continue
            imported_at = datetime.fromisoformat(imported['imported_at'])
            receipts = [(stamp, receipt, staged) for stamp, receipt, staged
                        in legacy.get(imported['digest'], [])
                        if _receipt_precedes_import(stamp, receipt, imported_at)]
            # The receipt is written before imports.imported_at. A failed later
            # attempt can have the same CSV digest but different raw context.
            candidates = [(receipt.get('choices'), staged['rows'], True, staged['source_sha256'])
                          for _, receipt, staged in sorted(
                              receipts, key=_receipt_order_key, reverse=True)]
        for prior_choices, rows, append, original_sha in candidates:
            if not isinstance(prior_choices, dict):
                continue
            mapping = prior_choices.get('mapping')
            cutoff = prior_choices.get('date_to')
            if (not isinstance(mapping, dict)
                    or (cutoff is not None and not isinstance(cutoff, str))):
                continue
            candidate_proof_key = None
            for row in rows:
                if not isinstance(row, dict):
                    continue
                target = mapping.get(row.get('source_account'))
                account = accounts.get(target)
                booked = row.get('date')
                identifier = row.get('external_id')
                if (account is None or not isinstance(booked, str)
                        or not isinstance(identifier, str) or booked <= account['opening_date']
                        or (cutoff is not None and booked > cutoff)):
                    continue
                key = (target, identifier)
                if key not in existing or key in baseline:
                    continue
                amount = format(money(row['amount']), '.2f')
                if append and (target, identifier, booked, amount, 'EUR') not in imported_records:
                    continue
                baseline[key] = {'date': booked,
                                 'amount': amount,
                                 'currency': 'EUR',
                                 'counterparty': row.get('counterparty', ''),
                                 'description': row.get('description', ''),
                                 'category': row.get('category', '')}
                # CSV digests do not bind append receipts to their original XLSX.
                # Only a digest-bound full manifest can prove historical IBAN identity.
                if (not append and key in identity_keys
                        and (existing[key]['transfer_id'] or _transfer_pair_state(store, key))):
                    if candidate_proof_key is None:
                        candidate_proof_key = (original_sha, digest(rows))
                    if candidate_proof_key not in proof_cache:
                        proof_cache[candidate_proof_key] = _original_identity_proofs(archive, original_sha, rows)
                    if candidate_proof_key not in review_proof_cache:
                        review_proof_cache[candidate_proof_key] = _original_identity_proofs(
                            archive, original_sha, rows, _review=True)
                    review_proof = review_proof_cache[candidate_proof_key].get(
                        (row['source_account'], identifier))
                    if review_proof:
                        baseline[key]['full_snapshot_proof'] = review_proof
                        if display_references is not None:
                            if candidate_proof_key not in display_cache:
                                display_cache[candidate_proof_key] = _original_identity_proofs(
                                    archive, original_sha, rows, _review=True, _display=True)
                            display_references[key] = display_cache[candidate_proof_key].get(
                                (row['source_account'], identifier), {}).get('counterparty_reference', '')
                    proof = proof_cache[candidate_proof_key].get((row['source_account'], identifier))
                    if proof:
                        baseline[key]['identity_proof'] = proof
                        baseline[key]['category'] = row.get('category', '')
    return baseline


def _receipt_precedes_import(file_stamp, receipt, imported_at):
    """Use the receipt's shared clock for new imports, file time for old ones."""
    if 'recorded_at' not in receipt:
        return file_stamp <= imported_at.timestamp()
    try:
        recorded_at = datetime.fromisoformat(receipt['recorded_at'])
        return (recorded_at.tzinfo is not None and imported_at.tzinfo is not None
                and recorded_at <= imported_at)
    except (TypeError, ValueError):
        return False


def _receipt_order_key(item):
    file_stamp, receipt, _ = item
    if 'recorded_at' in receipt:
        try:
            recorded_at = datetime.fromisoformat(receipt['recorded_at'])
            if recorded_at.tzinfo is not None:
                return recorded_at.astimezone(UTC)
        except (TypeError, ValueError):
            pass
    return datetime.fromtimestamp(file_stamp, UTC)


def _accepted_metadata_source(store, staged, source, target, head):
    """Require an intact receipt for this exact, successfully accepted original."""
    if (head is None or head['source_sha256'] != staged['source_sha256']
            or head['target_account'] != target or not head['fingerprint']):
        return False
    imported = store.db.execute('SELECT digest,source FROM imports WHERE id=?',
                                (head['import_id'],)).fetchone()
    if imported is None or hashlib.sha256(imported['source'].encode()).hexdigest() != imported['digest']:
        return False
    try:
        manifest = json.loads(imported['source'])
        prior_choices = manifest['choices']
        prior_rows = manifest['rows']
        eligible = {row['reference']: row for row in prior_rows
                    if row['date'] <= prior_choices['date_to']}
        accepted_pairs = [pair for pair in prior_choices['transfer_pairs']
                          if all(ref in eligible for ref in pair)]
        expected_fingerprint = 'source-v1:' + digest({
            'rows': [row for row in prior_rows if row['source_account'] == source],
            'mapping': {source: target},
            'transfer_pairs': [pair for pair in accepted_pairs if any(
                eligible[ref]['source_account'] == source for ref in pair)],
            'exported_at': manifest['exported_at'], 'date_to': prior_choices['date_to'],
        })
        return (head['fingerprint'] == expected_fingerprint
                and prior_choices['mode'] == 'snapshot' and prior_choices['full_export'] is True
                and manifest['kind'] == 'finanzguru_full_snapshot'
                and manifest['source_sha256'] == staged['source_sha256']
                and manifest['exported_at'] == head['exported_at']
                and manifest['choices']['mapping'][source] == target
                and [row for row in manifest['rows'] if row['source_account'] == source]
                == [row for row in staged['rows'] if row['source_account'] == source])
    except (KeyError, TypeError, ValueError):
        return False


def _plan(store, staged, choices, root):
    if not isinstance(choices, dict) or choices.get('mode') != 'snapshot':
        raise ValueError('invalid_snapshot_choices')
    reviewed_refs = choices.get('reviewed_transfer_name_changes', [])
    if (not isinstance(reviewed_refs, list) or len(reviewed_refs) > 10000
            or any(not isinstance(ref, str) or not 0 < len(ref) <= 120 for ref in reviewed_refs)
            or len(set(reviewed_refs)) != len(reviewed_refs)):
        raise ValueError('invalid_reviewed_transfer_name_changes')
    reviewed_refs = set(reviewed_refs)
    metadata_only = choices.get('metadata_only', False)
    if not isinstance(metadata_only, bool):
        raise ValueError('invalid_snapshot_choices')
    exported_at = _timestamp(choices)
    cutoff = _date_to(choices, exported_at)
    if choices.get('full_export') is not True:
        return {'blockers': ['full_export_required'], 'ready': False, 'rows': [],
                'counts': {}, 'new_net_amount': '0.00', 'net_change': '0.00',
                'date_to': cutoff, 'ledger_written': False, 'review_token': ''}, None
    # Apply the existing import's source validation, then reconcile IDs against
    # the current ledger instead of treating a changed ID as a fatal conflict.
    accounts = {row['id']: row for row in store.accounts()}
    mapping = choices.get('mapping')
    pairs = choices.get('transfer_pairs', [])
    if not isinstance(mapping, dict) or not isinstance(pairs, list):
        raise ValueError('invalid_choices')
    if choices.get('transfers_reviewed') is not True:
        raise ValueError('transfer_review_required')
    rows, sources, unselected_rows = selected_source_rows(staged, choices)
    validate_selected_transfer_pairs(staged['rows'], pairs, sources)
    if set(mapping) != sources or any(target not in accounts for target in mapping.values()):
        raise ValueError('complete_account_mapping_required')
    if len(set(mapping.values())) != len(mapping):
        raise ValueError('source_accounts_must_map_to_distinct_targets')
    blockers = list(staged['blockers'])
    from .bank_source_policy import bound_finanzguru_targets, partition_finanzguru_transfer_pairs
    directly_bound = bound_finanzguru_targets(store, mapping.values())
    direct_sources = {source for source, target in mapping.items() if target in directly_bound}
    active_mapping = {source: target for source, target in mapping.items()
                      if source not in direct_sources}
    if staged.get('split_row_count', 0) and choices.get('split_policy') != 'originals_only':
        blockers.append('split_policy_required')
    eligible = [row for row in rows if row['date'] <= cutoff
                and row['source_account'] not in direct_sources]
    references = {row['reference']: row for row in eligible}
    cross_source_postable_references = {
        row['reference'] for row in eligible
        if row['date'] > accounts[mapping[row['source_account']]]['opening_date']
    }
    (active_pairs, cross_source_references, skipped_direct_source_pairs,
     skipped_out_of_scope_pairs) = partition_finanzguru_transfer_pairs(
        rows, pairs, direct_sources, references, cross_source_postable_references)
    if cross_source_references:
        blockers.append('cross_source_transfer_requires_reconciliation')
    transfer_ids = {}
    for pair in active_pairs:
        if any(ref not in references or ref in transfer_ids for ref in pair):
            raise ValueError('invalid_transfer_pair')
        first, second = (references[ref] for ref in pair)
        if (mapping[first['source_account']] == mapping[second['source_account']]
                or money(first['amount']) == 0
                or money(first['amount']) + money(second['amount']) != 0):
            raise ValueError('invalid_transfer_amounts')
        if any(row['date'] <= accounts[mapping[row['source_account']]]['opening_date']
               for row in (first, second)):
            raise ValueError('transfer_crosses_opening_date')
        identity = 'fg-transfer:' + digest(sorted([first['external_id'], second['external_id']]))
        for ref in pair:
            transfer_ids[ref] = identity
    heads = {row['source_account']: dict(row) for row in store.db.execute(
        'SELECT * FROM finanzguru_snapshot_heads')}
    ledger = [dict(row) for row in store.db.execute(
        'SELECT * FROM transactions ORDER BY account_id,external_id')]
    contexts = [dict(row) for row in store.db.execute(
        'SELECT * FROM transaction_context ORDER BY account_id,external_id')]
    existing = {(row['account_id'], row['external_id']): row for row in ledger}
    transfer_members = {}
    for peer in ledger:
        if peer['transfer_id']:
            transfer_members.setdefault(peer['transfer_id'], set()).add(
                (peer['account_id'], peer['external_id']))
    selected_prefix_targets = {
        _source_prefix(source): target for source, target in active_mapping.items()}
    selected_existing_keys = {
        key for key in existing
        if selected_prefix_targets.get(key[1].rsplit(':', 1)[0] + ':') == key[0]}
    previous_references = {}
    source_context = _source_context(store, root, existing, selected_existing_keys, previous_references)
    accounts_by_id, ids_by_booking = {}, {}
    for old in ledger:
        accounts_by_id.setdefault(old['external_id'], set()).add(old['account_id'])
        ids_by_booking.setdefault(
            (old['account_id'], old['date'], old['amount']), []).append(old['external_id'])
    context_by_key = {(row['account_id'], row['external_id']): row for row in contexts}
    desired, detail, counts = {}, [], Counter()
    name_changes = {}
    name_reviews = []
    accepted_review_refs = set()
    manual_proofs = None
    current_references = None
    new_identity_proofs = None
    if unselected_rows:
        counts['source_not_selected'] = len(unselected_rows)
        for row in unselected_rows:
            detail.append({'reference': row['reference'], 'source_account': row['source_account'],
                           'date': row['date'], 'amount': row['amount'],
                           'category': row['category'], 'status': 'source_not_selected',
                           'transfer': False})
    if direct_sources:
        counts['direct_bank_source'] = sum(row['source_account'] in direct_sources for row in rows)
        for row in rows:
            if row['source_account'] in direct_sources:
                detail.append({'reference': row['reference'],
                               'source_account': row['source_account'],
                               'account_id': mapping[row['source_account']],
                               'date': row['date'], 'amount': row['amount'],
                               'category': row['category'], 'status': 'direct_bank_source',
                               'transfer': False})
    if skipped_direct_source_pairs:
        counts['transfer_pairs_skipped_for_direct_source'] = skipped_direct_source_pairs
    if skipped_out_of_scope_pairs:
        counts['transfer_pairs_outside_date_range'] = skipped_out_of_scope_pairs
    for row in rows:
        if row['source_account'] in direct_sources:
            continue
        if row['date'] > cutoff:
            counts['after_date_to'] += 1
            detail.append({'reference': row['reference'], 'account_id': mapping[row['source_account']],
                           'date': row['date'], 'amount': row['amount'], 'category': row['category'],
                           'status': 'after_date_to', 'transfer': False})
            continue
        source, target = row['source_account'], mapping[row['source_account']]
        if row['date'] <= accounts[target]['opening_date']:
            counts['before_or_on_opening'] += 1
            detail.append({'reference': row['reference'], 'account_id': target,
                           'date': row['date'], 'amount': row['amount'], 'category': row['category'],
                           'status': 'before_or_on_opening', 'transfer': False})
            continue
        key = (target, row['external_id'])
        values = {'account_id': target, 'external_id': row['external_id'],
                  'date': row['date'], 'amount': format(money(row['amount']), '.2f'),
                  'currency': 'EUR', 'category': row['category'],
                  'transfer_id': transfer_ids.get(row['reference'], ''),
                  'counterparty': row.get('counterparty', ''),
                  'description': row.get('description', '')}
        old_context = context_by_key.get(key) or {}
        previous_source = source_context.get(key)
        if (key in existing and previous_source is not None
                and all(values[name] == previous_source[name] for name in
                        ('date', 'amount', 'currency', 'counterparty', 'description'))):
            for name in ('counterparty', 'description'):
                values[name] = old_context.get(name, '')
        old = existing.get(key)
        if ('selected_sources' in choices and old is not None and (old['transfer_id'] or any(
                    pair['pair'] and pair['pair'].get('revoked_at') is None
                    for pair in _transfer_pair_state(store, key)))
                and (transfer_members.get(old['transfer_id'], set()) - selected_existing_keys
                     or any((member['account_id'], member['external_id']) not in selected_existing_keys
                            for pair in _transfer_pair_state(store, key) for member in pair['members']))
                and previous_source is not None and (previous_source.get('identity_proof')
                                                     or previous_source.get('full_snapshot_proof'))
                and values['counterparty'] != previous_source['counterparty']
                and all(values[name] == previous_source[name] for name in
                        ('date', 'amount', 'currency', 'description'))
                and all(old[name] == values[name] for name in ('date', 'amount', 'currency'))
                and all(old_context.get(name, '') == previous_source[name]
                        for name in ('counterparty', 'description'))
                and not values['transfer_id']):
            from .transfer_corrections import source_declares_transfer
            if (source_declares_transfer(previous_source) == source_declares_transfer(values)
                    == source_declares_transfer({'category': old['category'],
                                                'description': old_context.get('description', '')})):
                if new_identity_proofs is None:
                    new_identity_proofs = _original_identity_proofs(
                        Path(root), staged['source_sha256'], staged['rows'])
                new_proof = new_identity_proofs.get((source, row['external_id']))
                if (new_proof and previous_source.get('identity_proof')
                        and new_proof['identity_sha256']
                        == previous_source['identity_proof']['identity_sha256']):
                    name_changes[key] = {'previous': previous_source['identity_proof'],
                                         'current': new_proof}
                    values['transfer_id'] = old['transfer_id']
                elif (previous_source.get('full_snapshot_proof')
                      and not (new_proof and previous_source.get('identity_proof'))):
                    # Two different valid IBANs are contradictory bank evidence,
                    # not merely an unavailable identity or display-only reference.
                    if manual_proofs is None:
                        manual_proofs = _original_identity_proofs(
                            Path(root), staged['source_sha256'], staged['rows'], _review=True)
                    manual = manual_proofs.get((source, row['external_id']))
                    prior = previous_source['full_snapshot_proof']
                    dependency_kind = _reviewable_transfer_dependency(
                        store, key, old, existing, transfer_members, selected_existing_keys)
                    if (manual and dependency_kind and manual['stable_fields_sha256']
                            == prior['stable_fields_sha256']):
                        if current_references is None:
                            current_references = _original_identity_proofs(
                                Path(root), staged['source_sha256'], staged['rows'],
                                _review=True, _display=True)
                        reviewed = row['reference'] in reviewed_refs
                        name_reviews.append({
                            'reference': row['reference'], 'account_id': target,
                            'date': values['date'], 'amount': values['amount'],
                            'previous_counterparty': previous_source['counterparty'],
                            'current_counterparty': values['counterparty'],
                            'previous_counterparty_reference': previous_references.get(key, ''),
                            'current_counterparty_reference': current_references.get(
                                (source, row['external_id']), {}).get('counterparty_reference', ''),
                            'description': values['description'], 'reviewed': reviewed,
                            'dependency_kind': dependency_kind})
                        if reviewed:
                            accepted_review_refs.add(row['reference'])
                            name_changes[key] = {'kind': 'manual', 'reference': row['reference'],
                                                 'previous': prior, 'current': manual}
                            values['transfer_id'] = old['transfer_id']
        if ('selected_sources' in choices and old is not None
                and old['transfer_id'] and not values['transfer_id']
                and transfer_members.get(old['transfer_id'], set()) - selected_existing_keys
                and all(old[name] == values[name] for name in ('date', 'amount', 'currency'))
                and all(old_context.get(name, '') == values[name]
                        for name in ('counterparty', 'description'))):
            from .transfer_corrections import source_declares_transfer
            if source_declares_transfer({
                    'category': old['category'],
                    'description': old_context.get('description', '')}) == source_declares_transfer(values):
                # An omitted cross-source pair is not permission to dissolve an
                # unchanged existing transfer in a selected-source snapshot.
                values['transfer_id'] = old['transfer_id']
        if key in desired:
            status = 'duplicate_in_file' if desired[key] == values else 'source_id_conflict'
            if status == 'source_id_conflict':
                blockers.append(status)
        else:
            desired[key] = values
            if old is None:
                status = 'new'
            else:
                old_values = {name: old[name] for name in (
                    'account_id', 'external_id', 'date', 'amount', 'currency', 'category', 'transfer_id')}
                old_values.update({'counterparty': (context_by_key.get(key) or {}).get('counterparty', ''),
                                   'description': (context_by_key.get(key) or {}).get('description', '')})
                status = 'already_imported' if old_values == values else 'updated'
        counts[status] += 1
        detail.append({'reference': row['reference'], 'account_id': target,
                       'date': row['date'], 'amount': row['amount'], 'category': row['category'],
                       'status': status, 'transfer': bool(values['transfer_id']),
                       'blocking_reason': ('cross_source_transfer_requires_reconciliation'
                                           if row['reference'] in cross_source_references else None)})
    # The hash namespace binds a source account independently of the old import ID.
    for source, target in active_mapping.items():
        head = heads.get(source)
        if head and head['target_account'] != target:
            blockers.append('account_mapping_conflict')
        prefix = _source_prefix(source)
        if any(row['external_id'].startswith(prefix) and row['account_id'] != target
               for row in ledger):
            blockers.append('account_mapping_conflict')
    for key, values in desired.items():
        if accounts_by_id.get(key[1], set()) - {key[0]}:
            blockers.append('account_mapping_conflict')
        own_prefix = key[1].rsplit(':', 1)[0] + ':'
        if key not in existing and any(
                not identifier.startswith(own_prefix) for identifier in ids_by_booking.get(
                    (key[0], values['date'], values['amount']), [])):
            blockers.append('possible_other_import_overlap')
    removed = {}
    for source, target in active_mapping.items():
        prefix = _source_prefix(source)
        for key, old in existing.items():
            if key[0] == target and key[1].startswith(prefix) and key not in desired:
                removed[key] = old
                old_context = context_by_key.get(key) or {}
                detail.append({'reference': None, 'source_account': source,
                               'account_id': target, 'external_id': key[1],
                               'date': old['date'], 'amount': old['amount'],
                               'category': old['category'],
                               'counterparty': old_context.get('counterparty', ''),
                               'description': old_context.get('description', ''),
                               'status': 'removed', 'transfer': bool(old['transfer_id'])})
    counts['removed'] = len(removed)
    contract_dependencies = []
    from .transfer_corrections import source_declares_transfer
    for key in sorted(set(desired) | set(removed)):
        if not store.db.execute(
                'SELECT 1 FROM booking_contract_links WHERE account_id=? AND external_id=?',
                key).fetchone():
            continue
        old, new = existing.get(key), desired.get(key)
        context = context_by_key.get(key) or {}
        economic = new is None or (old is not None and (
            any(old[name] != new[name] for name in ('date', 'amount', 'currency', 'transfer_id'))
            or any(context.get(name, '') != new[name] for name in ('counterparty', 'description')
                   if name != 'counterparty' or key not in name_changes)
            or source_declares_transfer({
                'category': old['category'], 'description': context.get('description', '')
            }) != source_declares_transfer(new)))
        if economic:
            blockers.append('booking_contract_dependency')
            contract_dependencies.append({'account_id': key[0], 'external_id': key[1],
                                          'change_kind': 'removed' if new is None else 'updated'})
    transfer_dependencies = []
    if 'selected_sources' in choices:
        from .transfer_corrections import source_declares_transfer
        selected_keys = set(desired) | set(removed)
        reference_by_key = {
            (mapping[row['source_account']], row['external_id']): row['reference']
            for row in references.values()}
        for key in sorted(selected_keys):
            old = existing.get(key)
            if old is None:
                continue
            new = desired.get(key)
            context = context_by_key.get(key) or {}
            changed_fields = [] if new is None else [
                name for name in ('date', 'amount', 'currency', 'transfer_id')
                if old[name] != new[name]] + [
                name for name in ('counterparty', 'description')
                if context.get(name, '') != new[name]
                and (name != 'counterparty' or key not in name_changes)] + ([
                    'transfer_declaration'] if source_declares_transfer({
                        'category': old['category'], 'description': context.get('description', '')
                    }) != source_declares_transfer(new) else [])
            if new is not None and not changed_fields:
                continue
            item = {'reference': reference_by_key.get(key, key[1]),
                    'change_kind': 'removed' if new is None else 'updated',
                    'changed_fields': changed_fields}
            if old['transfer_id'] and transfer_members.get(old['transfer_id'], set()) - selected_keys:
                blockers.append('selected_source_transfer_dependency')
                transfer_dependencies.append(item | {'dependency_kind': 'ledger_transfer'})
            for pair in _transfer_pair_state(store, key):
                if any((member['account_id'], member['external_id']) not in selected_keys
                       for member in pair['members']):
                    blockers.append('selected_source_transfer_dependency')
                    transfer_dependencies.append(item | {
                        'dependency_kind': 'confirmed_correction_pair'})
                    break
    # Each head describes one source, so changing the selection of another
    # source must not make an unchanged head appear to be a conflicting export.
    source_fingerprints = {
        source: 'source-v1:' + digest({
            'rows': [row for row in rows if row['source_account'] == source],
            'mapping': {source: target},
            'transfer_pairs': [pair for pair in active_pairs if any(
                references[ref]['source_account'] == source for ref in pair)],
            'exported_at': exported_at, 'date_to': cutoff,
        }) for source, target in active_mapping.items()
    }
    fingerprint = digest({'sources': source_fingerprints, 'selected_sources': sorted(sources)})
    source_by_reference = {row['reference']: row['source_account'] for row in rows}
    changed_sources = {entry.get('source_account') or source_by_reference.get(entry['reference'])
        for entry in detail if entry['status'] in {'new', 'updated', 'removed'}}
    def same_source(head, source):
        if head is None or head['exported_at'] != exported_at:
            return False
        if head['fingerprint'].startswith('source-v1:'):
            return head['fingerprint'] == source_fingerprints[source]
        # Legacy heads used one fingerprint for the entire selection. Reconstruct
        # only what their archived, successful snapshot decision proves.
        if (head['source_sha256'] != staged['source_sha256']
                or head['target_account'] != active_mapping[source]
                or source in changed_sources):
            return False
        receipt = store.db.execute('SELECT source FROM imports WHERE id=?',
                                   (head['import_id'],)).fetchone()
        if receipt is None:
            return False
        try:
            archived = json.loads(receipt['source'])
            prior_choices = archived['choices']
            if archived['kind'] != 'finanzguru_full_snapshot':
                return False
            return (archived['exported_at'] == exported_at
                    and prior_choices['date_to'] == cutoff
                    and prior_choices['mapping'][source] == active_mapping[source]
                    and prior_choices['transfer_pairs'] == choices['transfer_pairs']
                    and [row for row in archived['rows'] if row['source_account'] == source]
                    == [row for row in rows if row['source_account'] == source])
        except (KeyError, TypeError, ValueError):
            return False
    replay = bool(active_mapping) and all(
        same_source(heads.get(source), source) for source in active_mapping)
    if replay:
        for source, target in active_mapping.items():
            head = heads.get(source)
            if not _accepted_metadata_source(store, staged, source, target, head):
                continue
            imported = store.db.execute('SELECT source FROM imports WHERE id=?',
                                        (head['import_id'],)).fetchone()
            archived = json.loads(imported['source'])
            prior_reviews = archived.get('reviewed_transfer_name_change_proofs', {})
            prior_refs = archived['choices'].get('reviewed_transfer_name_changes', [])
            for ref in reviewed_refs & set(prior_refs):
                if ref in prior_reviews and ref in references and references[ref]['source_account'] == source:
                    accepted_review_refs.add(ref)
    if reviewed_refs - accepted_review_refs:
        raise ValueError('invalid_reviewed_transfer_name_changes')
    for source in active_mapping:
        head = heads.get(source)
        if metadata_only:
            if not _accepted_metadata_source(store, staged, source, active_mapping[source], head):
                blockers.append('metadata_source_not_accepted')
            continue
        if head and exported_at < head['exported_at']:
            blockers.append('stale_export')
        elif head and exported_at == head['exported_at'] and not same_source(head, source):
            blockers.append('same_export_timestamp_conflict')
    new_total = sum((money(row['amount']) for key, row in desired.items() if key not in existing), money('0'))
    updated_delta = sum((money(row['amount']) - money(existing[key]['amount'])
                         for key, row in desired.items() if key in existing), money('0'))
    removed_total = sum((money(row['amount']) for row in removed.values()), money('0'))
    net_change = new_total + updated_delta - removed_total
    if replay and any(counts[name] for name in ('new', 'updated', 'removed')):
        blockers.append('snapshot_state_diverged')
    if metadata_only and any(counts[name] for name in ('new', 'updated', 'removed')):
        blockers.append('metadata_ledger_diverged')
    token = digest({'metadata_only': metadata_only, 'fingerprint': fingerprint, 'heads': heads,
                    'direct_bank_targets': sorted(directly_bound),
                    'direct_bank_sources': sorted(direct_sources),
                    'active_transfer_pairs': active_pairs,
                    'name_change_proofs': {key[0] + '\0' + key[1]: name_changes[key]
                                           for key in sorted(name_changes)},
                    'cross_source_references': sorted(cross_source_references),
                    'accounts': list(accounts.values()), 'ledger': ledger,
                    'contexts': contexts,
                    'source_context': {key[0] + '\0' + key[1]: source_context[key]
                                       for key in sorted(source_context)},
                    'dependencies': {key[0] + '\0' + key[1]: _dependent_state(store, key)
                                     for key in sorted(set(desired) | set(removed))},
                    'transfer_pairs': {key[0] + '\0' + key[1]: _transfer_pair_state(store, key)
                                       for key in sorted(set(desired) | set(removed))},
                    'metadata_sha256': staged.get('metadata_sha256'),
                    'reviewed_transfer_name_changes': sorted(reviewed_refs)})
    for name in ('new', 'updated', 'removed', 'already_imported'):
        counts[name] += 0
    report = {'counts': dict(counts), 'blockers': sorted(set(blockers)), 'rows': detail,
              'transfer_dependencies': transfer_dependencies,
              'contract_dependencies': contract_dependencies,
              'preserved_transfer_name_changes': sum(proof.get('kind') != 'manual' for proof in name_changes.values()),
              'reviewed_transfer_name_changes': sum(proof.get('kind') == 'manual' for proof in name_changes.values()),
              'transfer_name_change_reviews': name_reviews,
              'date_to': cutoff, 'new_net_amount': format(new_total, '.2f'),
              'net_change': format(net_change, '.2f'), 'ready': not blockers,
              'ledger_written': False, 'review_token': token,
              'auto_classification_count': 0, 'no_op': not active_mapping}
    return report, {'mapping': active_mapping, 'desired': desired, 'existing': existing,
                    'name_changes': name_changes,
                    'removed': removed, 'heads': heads, 'fingerprint': fingerprint,
                    'source_fingerprints': source_fingerprints,
                    'exported_at': exported_at, 'replay': replay, 'metadata_only': metadata_only,
                    'no_op': not active_mapping,
                    'skipped_direct_source_pairs': skipped_direct_source_pairs,
                    'skipped_out_of_scope_pairs': skipped_out_of_scope_pairs}


def preview_snapshot(store, root, batch, choices):
    _, staged = load_batch(root, batch)
    return _plan(store, staged, choices, root)[0]


def _clear_dependents(store, key, *, remove):
    state = _dependent_state(store, key)
    if state['booking_contract_links']:
        # This helper runs only for removals or economic changes. Neither is
        # evidence that a manually reviewed contract decision is still valid.
        raise ValueError('booking_contract_dependency')
    pair_ids = {row['pair_id'] for row in state['transfer_correction_members']}
    for pair_id in pair_ids:
        members = [dict(row) for row in store.db.execute(
            'SELECT * FROM transfer_correction_members WHERE pair_id=?', (pair_id,))]
        stamp = datetime.now(UTC).isoformat()
        store.db.execute('DELETE FROM transfer_correction_members WHERE pair_id=?', (pair_id,))
        store.db.execute('UPDATE transfer_correction_pairs SET revoked_at=? WHERE id=? AND revoked_at IS NULL',
                         (stamp, pair_id))
        store.db.execute(
            'INSERT INTO transfer_correction_audit(occurred_at,action,pair_id,current) VALUES (?,?,?,?)',
            (stamp, 'transfer_correction_revoked', pair_id,
             _json({'id': pair_id, 'revoked_at': stamp, 'members': members,
                    'reason': 'finanzguru_snapshot_reconciliation'})))
    for table in _DELETE_CHILDREN:
        if table == 'transfer_correction_members':
            continue
        if not remove and table in {'classification_model_rejections'}:
            continue
        store.db.execute(f'DELETE FROM {table} WHERE account_id=? AND external_id=?', key)
    store.db.execute(
        "UPDATE payment_mail_events SET account_id=NULL,external_id=NULL,match_status='stale' "
        'WHERE account_id=? AND external_id=?', key)
    return state


def _validate_transfers(store):
    from collections import defaultdict
    grouped = defaultdict(list)
    for row in store.db.execute("SELECT * FROM transactions WHERE transfer_id != ''"):
        grouped[row['transfer_id']].append(row)
    for pair in grouped.values():
        if (len(pair) != 2 or pair[0]['account_id'] == pair[1]['account_id']
                or pair[0]['currency'] != pair[1]['currency']
                or money(pair[0]['amount']) + money(pair[1]['amount']) != 0):
            raise ValueError('invalid_transfer_after_snapshot')


def commit_snapshot(store, root, batch, choices, review_token):
    folder, staged = load_batch(root, batch)
    store.db.execute('BEGIN IMMEDIATE')
    try:
        report, plan = _plan(store, staged, choices, root)
        if not report['ready'] or report['review_token'] != review_token:
            raise ValueError('review_stale_or_blocked')
        if plan['no_op']:
            store.db.commit()
            return {'inserted': 0, 'updated': 0, 'removed': 0, 'auto_classified': 0,
                    'auto_transfers': {'status': 'skipped', 'confirmed': 0, 'pair_ids': []},
                    'model_review': {'status': 'skipped', 'counts': {}, 'reviewed': 0},
                    'source_sha256': staged['source_sha256'], 'ledger_written': False,
                    'no_op': True}
        if plan['replay'] or plan['metadata_only']:
            metadata_rows = _mapped_row_metadata(
                staged, plan['mapping'], date_to=report.get('date_to'))
            _persist_row_metadata(store, metadata_rows, staged['source_sha256'])
            if plan['metadata_only']:
                receipt = {'kind': 'finanzguru_metadata_enrichment',
                           'recorded_at': datetime.now(UTC).isoformat(),
                           'source_sha256': staged['source_sha256'],
                           'rows_sha256': staged['rows_sha256'],
                           'metadata_sha256': staged.get('metadata_sha256'),
                           'choices': choices, 'review_token': review_token,
                           'ledger_written': False}
                receipt['manifest_sha256'] = hashlib.sha256(_json(receipt).encode()).hexdigest()
                with (folder / ('decision-' + secrets.token_hex(8) + '.json')).open(
                        'x', encoding='utf-8') as stream:
                    json.dump(receipt, stream, ensure_ascii=False, indent=2)
            store.db.commit()
            return {'inserted': 0, 'updated': 0, 'removed': 0, 'auto_classified': 0,
                    'auto_transfers': {'status': 'skipped', 'confirmed': 0, 'pair_ids': []},
                    'model_review': {'status': 'skipped', 'counts': {}, 'reviewed': 0},
                    'source_sha256': staged['source_sha256'], 'ledger_written': False}
        _check_foreign_keys(store)
        receipt = {'source_sha256': staged['source_sha256'],
                   'rows_sha256': staged['rows_sha256'],
                   'metadata_sha256': staged.get('metadata_sha256'),
                   'choices': choices, 'review_token': review_token,
                   'fingerprint': plan['fingerprint'],
                   'counts': report['counts'],
                   'reviewed_transfer_name_change_proofs': {
                       proof['reference']: proof for proof in plan['name_changes'].values()
                       if proof.get('kind') == 'manual'}}
        receipt['manifest_sha256'] = hashlib.sha256(_json(receipt).encode()).hexdigest()
        with (folder / ('decision-' + secrets.token_hex(8) + '.json')).open(
                'x', encoding='utf-8') as stream:
            json.dump(receipt, stream, ensure_ascii=False, indent=2)
        metadata_rows = _mapped_row_metadata(
            staged, plan['mapping'], date_to=report.get('date_to'))
        source_text = _json({'kind': 'finanzguru_full_snapshot',
                             'source_sha256': staged['source_sha256'],
                             'metadata_sha256': staged.get('metadata_sha256'),
                             'exported_at': plan['exported_at'],
                             'choices': choices,
                             'reviewed_transfer_name_change_proofs': {
                                 proof['reference']: proof for proof in plan['name_changes'].values()
                                 if proof.get('kind') == 'manual'},
                             'rows': [row for row in staged['rows']
                                      if row['source_account'] in plan['mapping']],
                             'row_metadata': metadata_rows})
        import_id = store.db.execute(
            'INSERT INTO imports(digest,imported_at,source) VALUES (?,?,?)',
            (hashlib.sha256(source_text.encode()).hexdigest(),
             datetime.now(UTC).isoformat(), source_text)).lastrowid
        changed = Counter()
        auto_classified = 0
        for key, old in plan['removed'].items():
            source = next(source for source, target in plan['mapping'].items()
                          if target == key[0] and key[1].startswith(_source_prefix(source)))
            dependencies = _clear_dependents(store, key, remove=True)
            _event(store, source=source, target=key[0], exported_at=plan['exported_at'],
                   sha=staged['source_sha256'], action='removed', key=key,
                   previous={'transaction': dict(old), 'dependencies': dependencies})
            store.db.execute('DELETE FROM transactions WHERE account_id=? AND external_id=?', key)
            changed['removed'] += 1
        for key, row in plan['desired'].items():
            source = next(source for source, target in plan['mapping'].items()
                          if target == key[0] and key[1].startswith(_source_prefix(source)))
            old = plan['existing'].get(key)
            if old is None:
                store.db.execute(
                    'INSERT INTO transactions(account_id,external_id,date,amount,currency,category,transfer_id,import_id) '
                    'VALUES (?,?,?,?,?,?,?,?)',
                    (*key, row['date'], row['amount'], row['currency'], row['category'],
                     row['transfer_id'], import_id))
                store.db.execute('INSERT INTO transaction_context VALUES (?,?,?,?)',
                                 (*key, row['counterparty'], row['description']))
                auto_classified += apply_exact_rule_if_safe(store, *key) is not None
                _event(store, source=source, target=key[0], exported_at=plan['exported_at'],
                       sha=staged['source_sha256'], action='inserted', key=key, current=row)
                changed['inserted'] += 1
                continue
            context = store.db.execute(
                'SELECT counterparty,description FROM transaction_context '
                'WHERE account_id=? AND external_id=?', key).fetchone()
            prior_context = dict(context) if context else {'counterparty': '', 'description': ''}
            same = all(old[name] == row[name] for name in
                       ('date', 'amount', 'currency', 'category', 'transfer_id')) and all(
                           prior_context[name] == row[name] for name in ('counterparty', 'description'))
            if same:
                continue
            from .transfer_corrections import source_declares_transfer
            source_transfer_changed = source_declares_transfer({
                'category': old['category'], 'description': prior_context['description']}) != source_declares_transfer(row)
            economic = (any(old[name] != row[name] for name in
                            ('date', 'amount', 'currency', 'transfer_id'))
                        or any(prior_context[name] != row[name]
                               for name in ('counterparty', 'description')
                               if name != 'counterparty' or key not in plan['name_changes'])
                        or source_transfer_changed)
            dependencies = _dependent_state(store, key)
            if economic:
                _clear_dependents(store, key, remove=False)
                # Preserve the revision anchor while revoking its confirmed category.
                override = dependencies['classification_overrides']
                if override:
                    store.db.execute(
                        'INSERT INTO classification_overrides('
                        'account_id,external_id,category_id,confirmed,revision) VALUES (?,?,NULL,0,?)',
                        (*key, override[0]['revision'] + 1))
                    store.db.execute(
                        'INSERT INTO classification_audit('
                        'occurred_at,action,account_id,external_id,previous,current) VALUES (?,?,?,?,?,?)',
                        (datetime.now(UTC).isoformat(), 'snapshot_category_reset', *key,
                         _json(override[0]), _json({'category_id': None, 'confirmed': 0,
                                                    'revision': override[0]['revision'] + 1})))
            store.db.execute(
                'UPDATE transactions SET date=?,amount=?,currency=?,category=?,transfer_id=?,import_id=? '
                'WHERE account_id=? AND external_id=?',
                (row['date'], row['amount'], row['currency'], row['category'],
                 row['transfer_id'], import_id, *key))
            store.db.execute(
                'INSERT INTO transaction_context(account_id,external_id,counterparty,description) '
                'VALUES (?,?,?,?) ON CONFLICT(account_id,external_id) DO UPDATE SET '
                'counterparty=excluded.counterparty,description=excluded.description',
                (*key, row['counterparty'], row['description']))
            if economic:
                auto_classified += apply_exact_rule_if_safe(
                    store, *key, allow_empty_override=True) is not None
            _event(store, source=source, target=key[0], exported_at=plan['exported_at'],
                   sha=staged['source_sha256'], action='updated', key=key,
                   previous={'transaction': dict(old), 'context': prior_context,
                             'dependencies': dependencies}, current=(
                                 row | {'transfer_name_change_review': plan['name_changes'][key]}
                                 if plan['name_changes'].get(key, {}).get('kind') == 'manual' else row))
            changed['updated'] += 1
        _validate_transfers(store)
        for source, target in plan['mapping'].items():
            store.db.execute(
                'INSERT INTO finanzguru_snapshot_heads('
                'source_account,target_account,exported_at,fingerprint,source_sha256,import_id) '
                'VALUES (?,?,?,?,?,?) ON CONFLICT(source_account) DO UPDATE SET '
                'exported_at=excluded.exported_at,fingerprint=excluded.fingerprint,'
                'source_sha256=excluded.source_sha256,import_id=excluded.import_id',
                (source, target, plan['exported_at'], plan['source_fingerprints'][source],
                 staged['source_sha256'], import_id))
            _event(store, source=source, target=target, exported_at=plan['exported_at'],
                   sha=staged['source_sha256'], action='accepted',
                   current={'fingerprint': plan['source_fingerprints'][source],
                            'import_id': import_id})
        _persist_row_metadata(store, metadata_rows, staged['source_sha256'])
        store.db.commit()
    except Exception:
        store.db.rollback()
        raise
    return {'inserted': changed['inserted'], 'updated': changed['updated'],
            'removed': changed['removed'], 'auto_classified': auto_classified,
            'auto_transfers': {'status': 'skipped', 'confirmed': 0, 'pair_ids': []},
            'model_review': {'status': 'skipped', 'counts': {}, 'reviewed': 0},
            'source_sha256': staged['source_sha256'], 'ledger_written': True}
