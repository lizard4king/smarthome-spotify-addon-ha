"""Read-only financial boundaries for active PayPal transaction-date rows."""

import json


def transaction_date_keys(store, account_ids=None) -> set[tuple[str, str]]:
    """Return active ledger identities carrying the transaction-date marker.

    The marker is sufficient even if other source metadata is damaged. The true
    booking date is unknown, so no temporal overlap can safely be inferred from
    its supplied transaction date. Consumers must keep the financial movement,
    but conservatively block booking-date-dependent derivations for the account.
    """
    parameters = ()
    clause = ''
    if account_ids is not None:
        parameters = tuple(sorted(set(account_ids)))
        if not parameters:
            return set()
        clause = ' WHERE p.account_id IN (' + ','.join('?' for _ in parameters) + ')'
    result = set()
    for row in store.db.execute(
            'SELECT p.account_id,p.external_id,p.normalized_json '
            'FROM paypal_source_transactions p JOIN active_transactions t '
            'ON t.account_id=p.account_id AND t.external_id=p.external_id' + clause,
            parameters):
        try:
            normalized = json.loads(row['normalized_json'])
        except (ValueError, TypeError):
            continue
        if isinstance(normalized, dict) and normalized.get('date_basis') == 'transaction_date':
            result.add((row['account_id'], row['external_id']))
    return result


def transaction_date_accounts(store, account_ids=None) -> set[str]:
    """Return affected accounts in one batch, without filtering ledger movements."""
    return {account_id for account_id, _ in transaction_date_keys(store, account_ids)}
