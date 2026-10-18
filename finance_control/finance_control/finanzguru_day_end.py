"""Read-only day-end proof from a complete accepted Finanzguru snapshot."""

from collections import Counter, OrderedDict, defaultdict
from hashlib import sha256
import json
import sys
from threading import RLock

from .core import money
from .paypal_date_basis import transaction_date_accounts


def _end_of_trail(edges):
    """Find the unique end of a connected directed Euler trail in linear time."""
    degrees = Counter()
    neighbours = defaultdict(set)
    for before, after in edges:
        degrees[before] += 1
        degrees[after] -= 1
        neighbours[before].add(after)
        neighbours[after].add(before)
    if not neighbours:
        return None
    pending = [next(iter(neighbours))]
    visited = set()
    while pending:
        node = pending.pop()
        if node not in visited:
            visited.add(node)
            pending.extend(neighbours[node] - visited)
    if len(visited) != len(neighbours):
        return None
    if len(neighbours) == 1:
        return next(iter(neighbours))
    starts = [node for node, degree in degrees.items() if degree == 1]
    ends = [node for node, degree in degrees.items() if degree == -1]
    if (len(starts) == len(ends) == 1
            and all(degree in (-1, 0, 1) for degree in degrees.values())):
        return ends[0]
    return None


# Bound retained Python objects, including exact source text keys. Oversized
# receipts still validate normally; they simply do not enter this process cache.
_CACHE_MAX_BYTES = 32 * 1024 * 1024
_CACHE_MAX_ENTRIES = 16
_proof_cache: OrderedDict[tuple, tuple[tuple, int]] = OrderedDict()
_cache_bytes = 0
_cache_lock = RLock()
_HEAD_COLUMNS = ('source_account', 'target_account', 'exported_at', 'fingerprint',
                 'source_sha256', 'import_id')
_METADATA_COLUMNS = ('source_account', 'source_reference', 'account_id', 'external_id',
                     'booked_on', 'currency', 'balance_amount', 'contract_id',
                     'contract_label', 'cadence_raw', 'interval_months')


def _retained_size(value):
    """Conservative object size: shared references are deliberately counted twice."""
    return sys.getsizeof(value) + (sum(map(_retained_size, value))
                                  if isinstance(value, tuple) else 0)


def _prepare_day(source_text, receipt_digest, head_items, currency, day):
    """Derive immutable proof parts only from these exact receipt/head facts."""
    from .finanzguru_snapshot import _accepted_metadata_manifest

    if sha256(source_text.encode()).hexdigest() != receipt_digest:
        return None
    manifest = json.loads(source_text)
    head = dict(zip(_HEAD_COLUMNS, head_items))
    source, target = head['source_account'], head['target_account']
    if not _accepted_metadata_manifest(
            manifest, {'source_sha256': head['source_sha256'], 'rows': manifest['rows']},
            source, target, head):
        return None
    if day > manifest['choices']['date_to']:
        return None
    rows = [row for row in manifest['rows']
            if row['source_account'] == source and row['date'] == day]
    postings = tuple((row['external_id'], row['date'], money(row['amount']), row['currency'])
                     for row in rows)
    if len({row['external_id'] for row in rows}) != len(rows):
        return None
    metadata = [row for row in manifest['row_metadata']
                if row['source_account'] == source and row['booked_on'] == day]
    evidence = tuple(tuple(row[key] for key in _METADATA_COLUMNS) for row in metadata)
    # JSON containers in evidence cannot satisfy the persisted-row Counter
    # comparison. Reject them before retaining only immutable proof values.
    hash(evidence)
    by_id = {row['external_id']: row for row in metadata}
    if len(by_id) != len(metadata) or set(by_id) != {row['external_id'] for row in rows}:
        return None
    edges = []
    for row in rows:
        meta = by_id[row['external_id']]
        if (meta['account_id'] != target or meta['currency'] != currency
                or meta['source_reference'] != row['reference']
                or money(meta['original_amount']) != money(row['amount'])):
            return None
        after = money(meta['balance_amount'])
        edges.append((after - money(row['amount']), after))
    end = _end_of_trail(edges)
    return postings, evidence, format(end, '.2f') if end is not None else None


def _cached_day(source_text, receipt_digest, head, currency, day):
    global _cache_bytes
    # Never key by import id alone: source and digest are freshly read each time,
    # and every head field participates even if an import id was reused.
    key = (source_text, receipt_digest, tuple(head[key] for key in _HEAD_COLUMNS), currency, day)
    with _cache_lock:
        if key in _proof_cache:
            _proof_cache.move_to_end(key)
            return _proof_cache[key][0]
    proof = _prepare_day(*key)
    if proof is None:
        return None
    # Include a conservative allowance for the OrderedDict node and size integer.
    size = _retained_size((key, proof)) + 512
    if size <= _CACHE_MAX_BYTES:
        with _cache_lock:
            previous = _proof_cache.pop(key, None)
            if previous is not None:
                _cache_bytes -= previous[1]
            while _proof_cache and (_cache_bytes + size > _CACHE_MAX_BYTES
                                    or len(_proof_cache) >= _CACHE_MAX_ENTRIES):
                _, (_, removed_size) = _proof_cache.popitem(last=False)
                _cache_bytes -= removed_size
            _proof_cache[key] = (proof, size)
            _cache_bytes += size
    return proof


def accepted_day_end(store, account_id, currency, day):
    """Return a proven end balance, never read original files or combine exports.

    Every ledger posting on the day must occur exactly once in one current,
    digest-bound accepted snapshot, with intact persisted row metadata for that
    day. Older metadata before account opening need not have been persisted.
    Multiple qualifying source heads remain unknown even if amounts coincide.
    Only immutable receipt derivations are cached. Current source text, digest,
    heads, ledger postings and persisted day metadata are checked on every call.
    """
    # Account-wide because the actual booking date and therefore overlap with
    # the requested day are unknown. Check before any cached proof can be used.
    if transaction_date_accounts(store, (account_id,)):
        return None
    ledger = [dict(row) for row in store.db.execute(
        'SELECT external_id,date,amount,currency FROM active_transactions '
        'WHERE account_id=? AND date=?', (account_id, day))]
    if not ledger or any(row['currency'] != currency for row in ledger):
        return None
    proven = []
    for head in store.db.execute(
            'SELECT * FROM finanzguru_snapshot_heads WHERE target_account=?', (account_id,)):
        try:
            imported = store.db.execute('SELECT digest,source FROM imports WHERE id=?',
                                        (head['import_id'],)).fetchone()
            if imported is None:
                continue
            proof = _cached_day(imported['source'], imported['digest'], head, currency, day)
            if proof is None:
                continue
            postings, evidence, end = proof
            current_postings = Counter((row['external_id'], row['date'], money(row['amount']),
                                        row['currency']) for row in ledger)
            if Counter(postings) != current_postings:
                continue
            persisted = store.db.execute(
                'SELECT * FROM finanzguru_row_metadata '
                'WHERE source_sha256=? AND source_account=? AND booked_on=?',
                (head['source_sha256'], head['source_account'], day))
            if Counter(evidence) != Counter(tuple(row[key] for key in _METADATA_COLUMNS)
                                             for row in persisted):
                continue
            proven.append(end)
        except (KeyError, TypeError, ValueError, ArithmeticError):
            # Corrupt/incomplete evidence cannot extend the legacy fallback.
            continue
    return proven[0] if len(proven) == 1 else None
