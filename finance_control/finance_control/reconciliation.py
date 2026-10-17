"""One evidence-bounded comparison of a recorded control and the active ledger."""

from datetime import date, datetime

from .booking_balances import LedgerBalanceUnavailable, ledger_balance_at
from .core import money
from .current_balances import latest_by_account
from .reconciliation_proof import validated_balance_comparison


def reconciliation_difference(store, account_id, as_of):
    """Return control minus ledger at the actual control date, never a guess.

    ``as_of`` bounds eligible controls, not the time they became known. A bank
    retrieval within a day is not proof of that day's final balance. Equal
    amounts therefore retain the timing and completeness warnings.
    """
    return reconciliation_differences(store, [account_id], as_of)[account_id]


def reconciliation_differences(store, account_ids, as_of):
    """Compare requested accounts using one bounded control lookup and read snapshot."""
    try:
        if type(as_of) is not str or date.fromisoformat(as_of).isoformat() != as_of:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise ValueError('invalid_reconciliation_as_of') from exc
    if (not isinstance(account_ids, (list, tuple, set, frozenset))
            or any(type(account_id) is not str for account_id in account_ids)):
        raise ValueError('unknown_reconciliation_account')
    identifiers = sorted(set(account_ids))
    if not identifiers:
        return {}
    own_transaction = not store.db.in_transaction
    if own_transaction:
        store.db.execute('BEGIN')
    try:
        placeholders = ','.join('?' for _ in identifiers)
        accounts = {
            row['id']: dict(row) for row in store.db.execute(
                f'SELECT * FROM accounts WHERE id IN ({placeholders})', identifiers)
        }
        if set(accounts) != set(identifiers):
            raise ValueError('unknown_reconciliation_account')
        controls = latest_by_account(store, list(accounts.values()), as_of=as_of,
                                     include_evidence=True)
        return {account_id: _difference(store, accounts[account_id],
                                        controls.get(account_id), as_of)
                for account_id in identifiers}
    finally:
        if own_transaction:
            store.db.rollback()


def _difference(store, account, control, as_of):
    account_id = account['id']
    result = {
        'version': 1, 'account_id': account_id, 'requested_as_of': as_of,
        'comparison_date': None, 'currency': account['currency'],
        'control_amount': None, 'ledger_amount': None, 'difference': None,
        'status': 'missing', 'source': None, 'control_reference': None,
        'retrieved_at': None, 'comparison_basis': None, 'reason': 'no_control',
        'warnings': ['ledger_completeness_unverified'],
    }

    def finish(reason=None):
        if reason is not None:
            result.update(status='missing', reason=reason, ledger_amount=None, difference=None)
        result['warnings'] = sorted(set(result['warnings']))
        validated = validated_balance_comparison(result)
        if validated is None:
            raise ValueError('invalid_reconciliation_result')
        return validated

    if control is None:
        return finish()
    try:
        control_day = control['booked_on']
        if date.fromisoformat(control_day).isoformat() != control_day or control_day > as_of:
            raise ValueError
        source = control['source']
        if source not in {'bank', 'statement', 'finanzguru_file'}:
            raise ValueError
        reference = control['control_reference']
        if type(reference) is not str or not reference or len(reference) > 256:
            raise ValueError
        retrieved_at = control.get('retrieved_at')
        if retrieved_at is not None:
            timestamp = datetime.fromisoformat(retrieved_at)
            if timestamp.utcoffset() is None:
                raise ValueError
        result.update(comparison_date=control_day, source=source,
                      control_reference=reference, retrieved_at=retrieved_at)
        if control_day < as_of:
            result['warnings'].append('older_control')
        if control.get('status') == 'ambiguous':
            return finish('conflicting_controls')
        if control['currency'] != account['currency']:
            return finish('currency_conflict')
        amount = money(control['amount'])
        result['control_amount'] = _amount(amount)
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return finish('invalid_control')

    basis = ('day_end' if source == 'statement' or (
        source == 'finanzguru_file' and control.get('day_end_proven') is True) else 'intraday')
    result['comparison_basis'] = basis
    if basis == 'intraday':
        result['warnings'].append('bank_balance_not_day_end' if source == 'bank'
                                  else 'file_balance_not_day_end')
    try:
        ledger = ledger_balance_at(store, account, control_day)
    except LedgerBalanceUnavailable as exc:
        return finish(str(exc))
    difference = amount - ledger
    result.update(ledger_amount=_amount(ledger), difference=_amount(difference),
                  status='matched' if difference == 0 else 'different', reason=None)
    return finish()


def _amount(value):
    return '0.00' if value == 0 else format(value, '.2f')


def view(store, data):
    """Strict read-only endpoint for one explicitly selected account and date."""
    if type(data) is not dict or set(data) != {'account_id', 'as_of'}:
        raise ValueError('invalid_reconciliation_request')
    if type(data['account_id']) is not str:
        raise ValueError('invalid_reconciliation_request')
    return reconciliation_difference(store, data['account_id'], data['as_of'])
