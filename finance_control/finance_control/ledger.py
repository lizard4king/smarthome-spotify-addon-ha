"""Versioned booking writes; the caller owns the surrounding transaction."""
from datetime import UTC, datetime

from .audit import actor_for

TRANSACTION_COLUMNS = (
    'account_id', 'external_id', 'date', 'amount', 'currency', 'category',
    'transfer_id', 'import_id',
)


def insert_transaction(store, values8):
    """Insert the next active revision, preserving any previous physical rows.

    Values follow TRANSACTION_COLUMNS. This function never commits.
    """
    values = tuple(values8)
    if len(values) != len(TRANSACTION_COLUMNS):
        raise ValueError('Exactly eight transaction values required')
    revision = store.db.execute(
        'SELECT COALESCE(MAX(revision),0)+1 FROM transactions '
        'WHERE account_id=? AND external_id=?', values[:2]).fetchone()[0]
    store.db.execute(
        'INSERT INTO transactions('
        'account_id,external_id,date,amount,currency,category,transfer_id,import_id,revision) '
        'VALUES (?,?,?,?,?,?,?,?,?)', (*values, revision))
    return revision


def void_transaction(store, key, event_id, reason):
    """End an active revision with a matching immutable snapshot event as proof.

    ``key`` is (account_id, external_id); return the ended revision. No commit.
    """
    if reason not in {'superseded', 'voided'}:
        raise ValueError('Invalid transaction void reason')
    if not isinstance(key, (tuple, list)) or len(key) != 2:
        raise ValueError('Transaction key must contain account and external ID')
    current = store.db.execute(
        'SELECT revision FROM active_transaction_versions WHERE account_id=? AND external_id=?',
        tuple(key)).fetchone()
    if current is None:
        raise ValueError('No active transaction to void')
    store.db.execute(
        'UPDATE transactions SET voided_at=?,voided_by_event=?,void_reason=?,voided_by=? '
        'WHERE account_id=? AND external_id=? AND revision=? AND voided_at IS NULL',
        (datetime.now(UTC).isoformat(), event_id, reason, actor_for(store),
         *key, current['revision']))
    return current['revision']
