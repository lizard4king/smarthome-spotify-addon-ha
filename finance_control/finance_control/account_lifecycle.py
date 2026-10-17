"""Explicit account closing dates; past transactions and balances remain intact."""

from datetime import UTC, date, datetime

from .audit import actor_for


def visible_on(account, as_of):
    """A closed account remains visible strictly before its closing date."""
    closed_on = account.get('closed_on')
    return closed_on is None or closed_on > as_of


def set_closed_on(store, account_id, closed_on, revision):
    """Record a confirmed closing date or explicitly reopen an account."""
    if (type(account_id) is not str or not account_id.strip()
            or len(account_id) > 120 or account_id != account_id.strip()
            or any(ord(char) < 32 for char in account_id)):
        raise ValueError('invalid_account_id')
    if type(revision) is not int or revision < 0:
        raise ValueError('invalid_lifecycle_revision')
    if closed_on is not None:
        if type(closed_on) is not str:
            raise ValueError('invalid_closing_date')
        try:
            if date.fromisoformat(closed_on).isoformat() != closed_on:
                raise ValueError('invalid_closing_date')
        except ValueError as error:
            raise ValueError('invalid_closing_date') from error
    with store.db:
        current = store.db.execute(
            'SELECT opening_date,closed_on,lifecycle_revision FROM accounts WHERE id=?',
            (account_id,)).fetchone()
        if current is None:
            raise ValueError('unknown_account')
        if current['lifecycle_revision'] != revision:
            raise ValueError('stale_lifecycle_revision')
        if closed_on == current['closed_on']:
            raise ValueError('unchanged_lifecycle')
        if closed_on is not None:
            if closed_on < current['opening_date']:
                raise ValueError('closing_before_opening')
            last_booking = store.db.execute(
                'SELECT MAX(date) FROM active_transactions WHERE account_id=?',
                (account_id,)).fetchone()[0]
            if last_booking is not None and last_booking > closed_on:
                raise ValueError('closing_before_last_booking')
        next_revision = revision + 1
        updated = store.db.execute(
            'UPDATE accounts SET closed_on=?,lifecycle_revision=? '
            'WHERE id=? AND lifecycle_revision=?',
            (closed_on, next_revision, account_id, revision))
        if updated.rowcount != 1:
            raise ValueError('stale_lifecycle_revision')
        store.db.execute(
            'INSERT INTO account_lifecycle_audit '
            '(account_id,revision,previous_closed_on,closed_on,changed_at,actor) '
            'VALUES (?,?,?,?,?,?)',
            (account_id, next_revision, current['closed_on'], closed_on,
             datetime.now(UTC).isoformat(), actor_for(store)))
    return {'id': account_id, 'closed_on': closed_on,
            'lifecycle_revision': next_revision}
