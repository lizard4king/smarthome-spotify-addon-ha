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
from .finanzguru_import import digest, load_batch


_CHILD_TABLES = (
    'transaction_context', 'classification_overrides',
    'classification_document_links', 'transfer_correction_members',
    'classification_model_reviews', 'transfer_single_legs',
    'payment_mail_events', 'bonsy_cash_allocations',
    'classification_model_rejections',
)
_DELETE_CHILDREN = tuple(name for name in _CHILD_TABLES if name != 'payment_mail_events')


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


def _source_context(store, root, existing):
    """Find the last accepted raw context for each still present booking."""
    baseline = {}
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
            candidates = [(choices, rows, False)]
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
            candidates = [(receipt.get('choices'), staged['rows'], True)
                          for _, receipt, staged in sorted(
                              receipts, key=_receipt_order_key, reverse=True)]
        for prior_choices, rows, append in candidates:
            if not isinstance(prior_choices, dict):
                continue
            mapping = prior_choices.get('mapping')
            cutoff = prior_choices.get('date_to')
            if (not isinstance(mapping, dict)
                    or (cutoff is not None and not isinstance(cutoff, str))):
                continue
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
                                 'description': row.get('description', '')}
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


def _plan(store, staged, choices, root):
    if not isinstance(choices, dict) or choices.get('mode') != 'snapshot':
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
    rows = staged['rows']
    sources = {row['source_account'] for row in rows}
    if set(mapping) != sources or any(target not in accounts for target in mapping.values()):
        raise ValueError('complete_account_mapping_required')
    if len(set(mapping.values())) != len(mapping):
        raise ValueError('source_accounts_must_map_to_distinct_targets')
    blockers = list(staged['blockers'])
    if staged.get('split_row_count', 0) and choices.get('split_policy') != 'originals_only':
        blockers.append('split_policy_required')
    eligible = [row for row in rows if row['date'] <= cutoff]
    references = {row['reference']: row for row in eligible}
    transfer_ids = {}
    for pair in pairs:
        if (not isinstance(pair, list) or len(pair) != 2 or pair[0] == pair[1]
                or any(ref not in references or ref in transfer_ids for ref in pair)):
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
    source_context = _source_context(store, root, existing)
    accounts_by_id, ids_by_booking = {}, {}
    for old in ledger:
        accounts_by_id.setdefault(old['external_id'], set()).add(old['account_id'])
        ids_by_booking.setdefault(
            (old['account_id'], old['date'], old['amount']), []).append(old['external_id'])
    context_by_key = {(row['account_id'], row['external_id']): row for row in contexts}
    desired, detail, counts = {}, [], Counter()
    for row in rows:
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
        if key in desired:
            status = 'duplicate_in_file' if desired[key] == values else 'source_id_conflict'
            if status == 'source_id_conflict':
                blockers.append(status)
        else:
            desired[key] = values
            old = existing.get(key)
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
                       'status': status, 'transfer': bool(values['transfer_id'])})
    # The hash namespace binds a source account independently of the old import ID.
    for source, target in mapping.items():
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
    for source, target in mapping.items():
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
    fingerprint = digest({'source': staged['source_sha256'], 'choices': choices,
                          'rows': staged['rows']})
    replay = bool(mapping) and all(
        heads.get(source) and heads[source]['exported_at'] == exported_at
        and heads[source]['fingerprint'] == fingerprint for source in mapping)
    for source in mapping:
        head = heads.get(source)
        if head and exported_at < head['exported_at']:
            blockers.append('stale_export')
        elif head and exported_at == head['exported_at'] and head['fingerprint'] != fingerprint:
            blockers.append('same_export_timestamp_conflict')
    new_total = sum((money(row['amount']) for key, row in desired.items() if key not in existing), money('0'))
    updated_delta = sum((money(row['amount']) - money(existing[key]['amount'])
                         for key, row in desired.items() if key in existing), money('0'))
    removed_total = sum((money(row['amount']) for row in removed.values()), money('0'))
    net_change = new_total + updated_delta - removed_total
    if replay and any(counts[name] for name in ('new', 'updated', 'removed')):
        blockers.append('snapshot_state_diverged')
    token = digest({'fingerprint': fingerprint, 'heads': heads,
                    'accounts': list(accounts.values()), 'ledger': ledger,
                    'contexts': contexts,
                    'source_context': {key[0] + '\0' + key[1]: source_context[key]
                                       for key in sorted(source_context)},
                    'dependencies': {key[0] + '\0' + key[1]: _dependent_state(store, key)
                                     for key in sorted(set(desired) | set(removed))},
                    'transfer_pairs': {key[0] + '\0' + key[1]: _transfer_pair_state(store, key)
                                       for key in sorted(set(desired) | set(removed))}})
    for name in ('new', 'updated', 'removed', 'already_imported'):
        counts[name] += 0
    report = {'counts': dict(counts), 'blockers': sorted(set(blockers)), 'rows': detail,
              'date_to': cutoff, 'new_net_amount': format(new_total, '.2f'),
              'net_change': format(net_change, '.2f'), 'ready': not blockers,
              'ledger_written': False, 'review_token': token,
              'auto_classification_count': 0}
    return report, {'mapping': mapping, 'desired': desired, 'existing': existing,
                    'removed': removed, 'heads': heads, 'fingerprint': fingerprint,
                    'exported_at': exported_at, 'replay': replay}


def preview_snapshot(store, root, batch, choices):
    _, staged = load_batch(root, batch)
    return _plan(store, staged, choices, root)[0]


def _clear_dependents(store, key, *, remove):
    state = _dependent_state(store, key)
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
        if plan['replay']:
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
                   'counts': report['counts']}
        receipt['manifest_sha256'] = hashlib.sha256(_json(receipt).encode()).hexdigest()
        with (folder / ('decision-' + secrets.token_hex(8) + '.json')).open(
                'x', encoding='utf-8') as stream:
            json.dump(receipt, stream, ensure_ascii=False, indent=2)
        source_text = _json({'kind': 'finanzguru_full_snapshot',
                             'source_sha256': staged['source_sha256'],
                             'exported_at': plan['exported_at'],
                             'choices': choices, 'rows': staged['rows']})
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
                               for name in ('counterparty', 'description'))
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
                             'dependencies': dependencies}, current=row)
            changed['updated'] += 1
        _validate_transfers(store)
        for source, target in plan['mapping'].items():
            store.db.execute(
                'INSERT INTO finanzguru_snapshot_heads('
                'source_account,target_account,exported_at,fingerprint,source_sha256,import_id) '
                'VALUES (?,?,?,?,?,?) ON CONFLICT(source_account) DO UPDATE SET '
                'exported_at=excluded.exported_at,fingerprint=excluded.fingerprint,'
                'source_sha256=excluded.source_sha256,import_id=excluded.import_id',
                (source, target, plan['exported_at'], plan['fingerprint'],
                 staged['source_sha256'], import_id))
            _event(store, source=source, target=target, exported_at=plan['exported_at'],
                   sha=staged['source_sha256'], action='accepted',
                   current={'fingerprint': plan['fingerprint'], 'import_id': import_id})
        store.db.commit()
    except Exception:
        store.db.rollback()
        raise
    return {'inserted': changed['inserted'], 'updated': changed['updated'],
            'removed': changed['removed'], 'auto_classified': auto_classified,
            'auto_transfers': {'status': 'skipped', 'confirmed': 0, 'pair_ids': []},
            'model_review': {'status': 'skipped', 'counts': {}, 'reviewed': 0},
            'source_sha256': staged['source_sha256'], 'ledger_written': True}
