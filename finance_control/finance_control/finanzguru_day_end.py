"""Read-only day-end proof from a complete accepted Finanzguru snapshot."""

from collections import Counter, defaultdict
import json

from .core import money


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


def accepted_day_end(store, account_id, currency, day):
    """Return a proven end balance, never read original files or combine exports.

    Every ledger posting on the day must occur exactly once in one current,
    digest-bound accepted snapshot, with intact persisted row metadata for that
    day. Older metadata before account opening need not have been persisted.
    Multiple qualifying source heads remain unknown even if amounts coincide.
    """
    # Imported lazily: snapshot intake also uses balance-related modules.
    from .finanzguru_snapshot import _accepted_metadata_source

    ledger = [dict(row) for row in store.db.execute(
        'SELECT external_id,date,amount,currency FROM transactions '
        'WHERE account_id=? AND date=?', (account_id, day))]
    if not ledger or any(row['currency'] != currency for row in ledger):
        return None
    proven = []
    for head in store.db.execute(
            'SELECT * FROM finanzguru_snapshot_heads WHERE target_account=?', (account_id,)):
        try:
            imported = store.db.execute('SELECT source FROM imports WHERE id=?',
                                        (head['import_id'],)).fetchone()
            if imported is None:
                continue
            manifest = json.loads(imported['source'])
            source = head['source_account']
            if not _accepted_metadata_source(
                    store, {'source_sha256': head['source_sha256'], 'rows': manifest['rows']},
                    source, account_id, head):
                continue
            if day > manifest['choices']['date_to']:
                continue
            rows = [row for row in manifest['rows']
                    if row['source_account'] == source and row['date'] == day]
            def posting(row):
                return (row['external_id'], row['date'], money(row['amount']), row['currency'])
            if (len({row['external_id'] for row in rows}) != len(rows)
                    or Counter(map(posting, rows)) != Counter(map(posting, ledger))):
                continue
            metadata = [row for row in manifest['row_metadata']
                        if row['source_account'] == source and row['booked_on'] == day]
            persisted = [dict(row) for row in store.db.execute(
                'SELECT * FROM finanzguru_row_metadata '
                'WHERE source_sha256=? AND source_account=? AND booked_on=?',
                (head['source_sha256'], source, day))]
            columns = ('source_account', 'source_reference', 'account_id', 'external_id',
                       'booked_on', 'currency', 'balance_amount', 'contract_id',
                       'contract_label', 'cadence_raw', 'interval_months')
            def evidence(row):
                return tuple(row[key] for key in columns)
            if Counter(map(evidence, metadata)) != Counter(map(evidence, persisted)):
                continue
            by_id = {row['external_id']: row for row in metadata}
            if len(by_id) != len(metadata) or set(by_id) != {row['external_id'] for row in rows}:
                continue
            edges = []
            for row in rows:
                meta = by_id[row['external_id']]
                if (meta['account_id'] != account_id or meta['currency'] != currency
                        or meta['source_reference'] != row['reference']
                        or money(meta['original_amount']) != money(row['amount'])):
                    break
                after = money(meta['balance_amount'])
                edges.append((after - money(row['amount']), after))
            else:
                end = _end_of_trail(edges)
                proven.append(format(end, '.2f') if end is not None else None)
        except (KeyError, TypeError, ValueError, ArithmeticError):
            # Corrupt/incomplete evidence cannot extend the legacy fallback.
            continue
    return proven[0] if len(proven) == 1 else None
