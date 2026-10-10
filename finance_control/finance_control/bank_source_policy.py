"""Explicit bank-source cutover and safe adoption of existing closed months."""

from collections import Counter
from datetime import date
from decimal import Decimal, localcontext
import hashlib
import json
from pathlib import Path

from .core import money, valid_identifier
from .monthly_archive import monthly_source_account_key, read_monthly_archive
from .reconciliation_proof import LedgerControlBalanceMismatch


def bound_finanzguru_targets(store, target_accounts):
    """Return explicitly bound account IDs; institution labels are never consulted."""
    table = store.db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='bank_source_accounts'").fetchone()
    if table is None:
        return set()
    targets = set(target_accounts)
    if not targets:
        return set()
    marks = ','.join('?' for _ in targets)
    return {row[0] for row in store.db.execute(
        f'SELECT account_id FROM bank_source_accounts WHERE account_id IN ({marks})',
        tuple(sorted(targets)))}


def partition_finanzguru_transfer_pairs(rows, pairs, direct_sources, eligible_references,
                                       cross_source_postable_references=None):
    """Keep unrelated pairs; flag a direct/unbound pair without dropping its unbound row."""
    by_reference = {row['reference']: row for row in rows}
    eligible_references = set(eligible_references)
    if cross_source_postable_references is None:
        cross_source_postable_references = eligible_references
    else:
        cross_source_postable_references = set(cross_source_postable_references)
    used_references = set()
    retained = []
    cross_source_references = set()
    skipped_direct_source_pairs = 0
    skipped_out_of_scope_pairs = 0
    for pair in pairs:
        if (not isinstance(pair, list) or len(pair) != 2 or pair[0] == pair[1]
                or any(not isinstance(reference, str) or reference not in by_reference for reference in pair)
                or any(reference in used_references for reference in pair)):
            raise ValueError('invalid_transfer_pair')
        used_references.update(pair)
        first, second = (by_reference[reference] for reference in pair)
        first_direct = first['source_account'] in direct_sources
        second_direct = second['source_account'] in direct_sources
        if first_direct or second_direct:
            skipped_direct_source_pairs += 1
            if first_direct != second_direct:
                unbound_reference = pair[1] if first_direct else pair[0]
                if unbound_reference in cross_source_postable_references:
                    cross_source_references.add(unbound_reference)
            continue
        if all(reference in eligible_references for reference in pair):
            retained.append(pair)
        else:
            # A user-confirmed pair cannot silently degrade into a single-leg
            # import when its other unbound leg is outside the selected range.
            raise ValueError('invalid_transfer_pair')
    return retained, cross_source_references, skipped_direct_source_pairs, skipped_out_of_scope_pairs


def _ledger_fingerprint(store, account_id, period_start, period_end):
    from .finanzguru_snapshot import _dependent_state, _transfer_pair_state

    records = []
    for row in store.db.execute(
            'SELECT * FROM transactions WHERE account_id=? AND date>=? AND date<=? '
            'ORDER BY external_id', (account_id, period_start, period_end)):
        key = (row['account_id'], row['external_id'])
        records.append({'transaction': dict(row), 'dependencies': _dependent_state(store, key),
                        'transfer_pair': _transfer_pair_state(store, key)})
    payload = json.dumps(records, sort_keys=True, ensure_ascii=False, default=str, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _ledger_rows(store, account_id, period_start, period_end):
    return store.db.execute(
        'SELECT date,amount,currency FROM transactions WHERE account_id=? AND date>=? AND date<=?',
        (account_id, period_start, period_end)).fetchall()


def adopt_monthly_archive(store, path, account_id, confirmed_source_account, *, manage_transaction=True):
    """Bind one direct bank source after exact closed-month reconciliation.

    Returns 1 when the immutable adoption record is created and 0 for an exact
    replay. The existing ledger, categories, context, and references are never
    written by this operation.
    """
    if type(manage_transaction) is not bool:
        raise ValueError('invalid_transaction_mode')
    if manage_transaction and store.db.in_transaction:
        raise ValueError('caller_transaction_active')
    if not manage_transaction and not store.db.in_transaction:
        raise ValueError('caller_transaction_required')
    snapshot = read_monthly_archive(path)
    if type(confirmed_source_account) is not str or confirmed_source_account != snapshot.source_account:
        raise ValueError('source_account_confirmation_required')
    if not valid_identifier(account_id):
        raise ValueError('invalid_target_account')
    today = date.today()
    current_month_start = date(today.year, today.month, 1)
    if snapshot.period_end >= current_month_start:
        raise ValueError('closed_month_required')

    source_key = monthly_source_account_key(snapshot)
    start, end = snapshot.period_start.isoformat(), snapshot.period_end.isoformat()
    archive_sha256 = Path(path).stem
    try:
        if manage_transaction:
            store.db.execute('BEGIN IMMEDIATE')
        account = store.db.execute('SELECT * FROM accounts WHERE id=?', (account_id,)).fetchone()
        if account is None or account['currency'] != snapshot.currency:
            raise ValueError('target_account_mismatch')
        if account['opening_date'] >= start:
            raise ValueError('month_precedes_ledger_opening')

        source_binding = store.db.execute(
            'SELECT provider,account_id FROM bank_source_accounts WHERE source_key=?',
            (source_key,)).fetchone()
        account_binding = store.db.execute(
            'SELECT provider,source_key FROM bank_source_accounts WHERE account_id=?',
            (account_id,)).fetchone()
        statement_source_binding = store.db.execute(
            'SELECT account_id FROM bank_statement_account_bindings WHERE source_key=?',
            (source_key,)).fetchone()
        statement_account_binding = store.db.execute(
            'SELECT source_key FROM bank_statement_account_bindings WHERE account_id=?',
            (account_id,)).fetchone()
        if ((source_binding is not None and
             (source_binding['account_id'] != account_id or source_binding['provider'] != snapshot.source_profile))
                or (account_binding is not None and
                    (account_binding['source_key'] != source_key or account_binding['provider'] != snapshot.source_profile))
                or (statement_source_binding is not None and statement_source_binding['account_id'] != account_id)
                or (statement_account_binding is not None and statement_account_binding['source_key'] != source_key)):
            raise ValueError('bank_source_binding_conflict')

        with localcontext() as context:
            context.prec = 40
            opening = money(account['opening'])
            for row in store.db.execute(
                    'SELECT amount FROM transactions WHERE account_id=? AND date<?', (account_id, start)):
                opening += money(row['amount'])
            ledger_rows = _ledger_rows(store, account_id, start, end)
            closing = opening + sum((money(row['amount']) for row in ledger_rows), Decimal('0'))
        if opening != snapshot.opening_balance or closing != snapshot.closing_balance:
            raise LedgerControlBalanceMismatch(reconciliation={
                'period_start': start, 'period_end': end, 'currency': snapshot.currency,
                'ledger_opening_date': account['opening_date'],
                'ledger_initial_balance': format(money(account['opening']), '.2f'),
                'ledger_opening_balance': format(opening, '.2f'),
                'ledger_closing_balance': format(closing, '.2f'),
                'bank_opening_balance': format(snapshot.opening_balance, '.2f'),
                'bank_closing_balance': format(snapshot.closing_balance, '.2f'),
                'ledger_booking_count': len(ledger_rows), 'bank_booking_count': len(snapshot.rows),
            })

        expected = Counter((row.booked_on.isoformat(), money(row.amount), row.currency)
                           for row in snapshot.rows)
        actual = Counter((row['date'], money(row['amount']), row['currency']) for row in ledger_rows)
        if actual != expected:
            raise ValueError('ledger_month_multiset_mismatch')

        fingerprint = _ledger_fingerprint(store, account_id, start, end)
        previous = store.db.execute(
            'SELECT * FROM bank_monthly_adoptions WHERE account_id=? AND period_start=?',
            (account_id, start)).fetchone()
        expected_record = (end, source_key, archive_sha256, fingerprint, len(snapshot.rows),
                           format(snapshot.opening_balance, '.2f'), format(snapshot.closing_balance, '.2f'))
        if previous is not None:
            actual_record = (previous['period_end'], previous['source_key'], previous['archive_sha256'],
                             previous['ledger_sha256'], previous['booking_count'],
                             previous['opening_balance'], previous['closing_balance'])
            if actual_record != expected_record:
                raise ValueError('monthly_adoption_conflict')
            if manage_transaction:
                store.db.commit()
            return 0

        if source_binding is None:
            store.db.execute('INSERT INTO bank_source_accounts(source_key,provider,account_id) VALUES (?,?,?)',
                             (source_key, snapshot.source_profile, account_id))
        store.db.execute(
            'INSERT INTO bank_monthly_adoptions('
            'account_id,period_start,period_end,source_key,archive_sha256,ledger_sha256,'
            'booking_count,opening_balance,closing_balance) VALUES (?,?,?,?,?,?,?,?,?)',
            (account_id, start, end, source_key, archive_sha256, fingerprint, len(snapshot.rows),
             format(snapshot.opening_balance, '.2f'), format(snapshot.closing_balance, '.2f')))
        if manage_transaction:
            store.db.commit()
        return 1
    except Exception as error:
        if manage_transaction:
            store.db.rollback()
        if isinstance(error, ValueError):
            raise
        raise ValueError('monthly_adoption_failed') from None
