"""Latest recorded bank or reconciled statement balance per account."""

from datetime import date
from hashlib import sha256
import json
from decimal import Decimal

from .core import money
from .paypal_date_basis import transaction_date_accounts
from .server_balance_mappings import target_compatible


def summarize(account_rows, balances):
    """Sum only recorded current balances, keeping unknown accounts explicit.

    ``balances`` maps account ids to the current_balance objects returned by
    :func:`latest_by_account`. Amounts are accumulated as Decimal and never
    converted between currencies.
    """
    totals = {}
    for account in account_rows:
        account_id = account['id']
        currency = account['currency']
        if type(currency) is not str or not currency:
            continue
        group = totals.setdefault(currency, {
            'currency': currency, 'amount': Decimal('0.00'),
            'known_count': 0, 'account_count': 0,
            'oldest_booked_on': None, 'newest_booked_on': None,
        })
        group['account_count'] += 1
        balance = balances.get(account_id)
        if not isinstance(balance, dict) or balance.get('currency') != currency:
            continue
        try:
            amount = money(balance['amount'])
            booked_on = balance['booked_on']
            if (type(booked_on) is not str
                    or date.fromisoformat(booked_on).isoformat() != booked_on):
                continue
        except (KeyError, TypeError, ValueError):
            continue
        group['amount'] += amount
        group['known_count'] += 1
        if group['oldest_booked_on'] is None or booked_on < group['oldest_booked_on']:
            group['oldest_booked_on'] = booked_on
        if group['newest_booked_on'] is None or booked_on > group['newest_booked_on']:
            group['newest_booked_on'] = booked_on
    return [{**group, 'amount': format(group['amount'], '.2f')}
            for _, group in sorted(totals.items())]


def _candidate(account_id, currency, amount, booked_on, retrieved_at, source, reference):
    try:
        if (type(booked_on) is not str or date.fromisoformat(booked_on).isoformat() != booked_on
                or type(currency) is not str or not currency):
            return None
        amount = format(money(amount), '.2f')
    except (TypeError, ValueError):
        return None
    return (account_id, {'amount': amount, 'currency': currency,
                         'booked_on': booked_on, 'retrieved_at': retrieved_at,
                         'source': source, 'control_reference': reference})


def latest_by_account(store, account_rows, *, as_of=None, include_evidence=False,
                      include_confirmed_reference=True):
    """Use recorded controls only, optionally bounded by an inclusive ISO date.

    Without ``as_of`` the latest recorded control is returned regardless of the
    reporting month. The bound applies before selecting the latest candidate,
    so earlier bank observations and statements remain available behind a future
    balance. ``include_evidence`` adds stable control references and all conflicting
    highest-ranked controls. Conflicting amounts are ambiguous even without this
    option; no amount is selected by query order. Exclude confirmed references
    when an independent control is needed for reconciliation.
    """
    if as_of is not None:
        try:
            if type(as_of) is not str or date.fromisoformat(as_of).isoformat() != as_of:
                raise ValueError('invalid_balance_as_of')
        except (TypeError, ValueError) as exc:
            raise ValueError('invalid_balance_as_of') from exc
    wanted = {row['id']: row['currency'] for row in account_rows}
    if not wanted:
        return {}
    placeholders = ','.join('?' for _ in wanted)
    parameters = (*wanted, as_of) if as_of is not None else tuple(wanted)
    candidates = []

    # Card controls are qualified only while their confirmed card target and owner remain active.
    for row in store.db.execute(
            'SELECT e.id,e.account_id,e.amount,e.currency,e.booked_on,e.retrieved_at '
            'FROM card_balance_observations e JOIN card_sources s ON s.source_key=e.source_key '
            'AND s.account_id=e.account_id JOIN server_cards k ON k.card_id=s.card_id '
            'AND k.account_id=s.account_id AND k.owner_id=s.owner_id AND k.connection_id=s.connection_id '
            'AND k.connection_revision=s.connection_revision '
            'JOIN app_bank_connections c ON c.id=s.connection_id AND c.user_id=s.owner_id '
            "AND c.status!='REVOKED' AND c.revision=s.connection_revision AND c.bank_id='POSTBANK' "
            'JOIN app_users u ON u.id=s.owner_id AND u.active=1 '
            "JOIN accounts a ON a.id=e.account_id AND a.kind='CREDIT_CARD' AND a.currency=e.currency "
            f'WHERE e.account_id IN ({placeholders})'
            + (' AND e.booked_on<=?' if as_of is not None else ''), parameters):
        candidates.append(_candidate(row['account_id'], row['currency'], row['amount'], row['booked_on'],
                                     row['retrieved_at'], 'bank', f"card_observation:{row['id']}"))
    # Explicit bank balances are accepted only while their original binding
    # still points at the same account.
    for table, reference in [('bank_balance_observations', "'bank_observation:' || b.id"),
                             ('bank_account_balances', "'bank_current:' || b.account_id")]:
        # Read the current table too for legacy/raw fixtures, except an exact
        # observation already represented by the immutable history.
        legacy_only = (" AND NOT EXISTS (SELECT 1 FROM bank_balance_observations h "
                       "WHERE h.account_id=b.account_id AND h.source_key=b.source_key "
                       "AND h.amount=b.amount AND h.currency=b.currency "
                       "AND h.booked_on=b.booked_on AND h.retrieved_at=b.retrieved_at)"
                       if table == 'bank_account_balances' else '')
        for row in store.db.execute(
                'SELECT b.account_id,b.amount,b.currency,b.booked_on,b.retrieved_at,'
                + reference + ' AS control_reference '
                f'FROM {table} b JOIN bank_source_accounts s '
                'ON s.source_key=b.source_key AND s.account_id=b.account_id '
                f'WHERE b.account_id IN ({placeholders})' + legacy_only
                + (' AND b.booked_on<=?' if as_of is not None else ''), parameters):
            candidates.append(_candidate(row['account_id'], row['currency'], row['amount'],
                                         row['booked_on'], row['retrieved_at'], 'bank',
                                         row['control_reference']))
    # Additional balance-only confirmations never grant a transaction import.
    # Revalidate current ownership, registration, target metadata and existing
    # source bindings before exposing immutable observations as current balances.
    for row in store.db.execute(
            'SELECT e.id,e.account_id,e.amount,e.currency,e.booked_on,e.retrieved_at,'
            'e.bank_id,a.kind,a.institution '
            'FROM server_bank_balance_evidence e '
            'JOIN server_bank_balance_bindings b ON b.connection_id=e.connection_id '
            'AND b.account_fingerprint=e.account_fingerprint AND b.account_id=e.account_id '
            'AND b.owner_id=e.owner_id AND b.bank_id=e.bank_id '
            'AND b.connection_revision=e.connection_revision '
            'JOIN app_bank_connections c ON c.id=b.connection_id AND c.user_id=b.owner_id '
            "AND c.status!='REVOKED' AND c.revision=e.connection_revision AND c.bank_id=e.bank_id "
            'JOIN app_users u ON u.id=b.owner_id AND u.active=1 '
            'JOIN accounts a ON a.id=e.account_id AND a.currency=e.currency '
            f'WHERE e.account_id IN ({placeholders}) '
            'AND NOT EXISTS (SELECT 1 FROM bank_source_accounts s '
            'WHERE s.account_id=e.account_id AND s.provider!=e.bank_id) '
            'AND NOT EXISTS (SELECT 1 FROM server_bank_refresh_bindings r '
            'WHERE r.account_id=e.account_id AND (r.connection_id!=e.connection_id '
            'OR r.account_fingerprint!=e.account_fingerprint OR r.bank_id!=e.bank_id))'
            + (' AND e.booked_on<=?' if as_of is not None else ''), parameters):
        if target_compatible(row, row['bank_id']):
            candidates.append(_candidate(row['account_id'], row['currency'], row['amount'],
                                         row['booked_on'], row['retrieved_at'], 'bank',
                                         f"balance_evidence:{row['id']}"))
    for row in store.db.execute(
            'SELECT p.period_key,p.account_id,p.closing_balance,p.currency,p.as_of '
            'FROM ing_period_imports p JOIN bank_source_accounts s '
            'ON s.source_key=p.source_key AND s.account_id=p.account_id '
            f'WHERE p.account_id IN ({placeholders})'
            + (' AND p.as_of<=?' if as_of is not None else ''), parameters):
        candidates.append(_candidate(row['account_id'], row['currency'],
                                     row['closing_balance'], row['as_of'], None, 'statement',
                                     f"period_import:{row['period_key']}"))
    for row in store.db.execute(
            'SELECT m.period_start,m.account_id,m.closing_balance,a.currency,m.period_end '
            'FROM bank_monthly_adoptions m JOIN bank_source_accounts s '
            'ON s.source_key=m.source_key AND s.account_id=m.account_id '
            'JOIN accounts a ON a.id=m.account_id '
            f'WHERE m.account_id IN ({placeholders})'
            + (' AND m.period_end<=?' if as_of is not None else ''), parameters):
        candidates.append(_candidate(row['account_id'], row['currency'],
                                     row['closing_balance'], row['period_end'], None, 'statement',
                                     f"adoption:{row['account_id']}:{row['period_start']}"))
    for row in store.db.execute(
            'SELECT i.statement_key,i.account_id,i.closing_balance,a.currency,i.period_end '
            'FROM bank_statement_imports i JOIN bank_statement_account_bindings b '
            'ON b.account_id=i.account_id JOIN accounts a ON a.id=i.account_id '
            f'WHERE i.account_id IN ({placeholders})'
            + (' AND i.period_end<=?' if as_of is not None else ''), parameters):
        candidates.append(_candidate(row['account_id'], row['currency'],
                                     row['closing_balance'], row['period_end'], None, 'statement',
                                     f"statement_import:{row['statement_key']}"))

    result = {}
    by_account = {}
    for candidate in candidates:
        if candidate is not None and candidate[1]['currency'] == wanted[candidate[0]]:
            by_account.setdefault(candidate[0], []).append(candidate[1])
    for account_id, values in by_account.items():
        def rank(value):
            return (value['booked_on'], value['source'] == 'bank', value['retrieved_at'] or '')
        highest = max(map(rank, values))
        latest = sorted((value for value in values if rank(value) == highest),
                        key=lambda value: value['control_reference'])
        selected = dict(latest[0])
        if len({value['amount'] for value in latest}) > 1:
            selected.update(amount=None, status='ambiguous', warning='conflicting_controls')
            selected['conflicting_controls'] = latest
            # Bind the public reference to every conflicting fact, not only the
            # arbitrary first candidate, so confirmation tokens detect changes.
            facts = [{key: value[key] for key in (
                'control_reference', 'amount', 'currency', 'booked_on', 'retrieved_at')}
                for value in latest]
            encoded = json.dumps(facts, sort_keys=True, separators=(',', ':')).encode('utf-8')
            selected['control_reference'] = 'conflicting:' + sha256(encoded).hexdigest()
        result[account_id] = selected

    # Explicit owner references outrank file evidence but never qualified controls.
    if include_confirmed_reference:
        from .balance_references import latest as confirmed_references
        uncertain = transaction_date_accounts(store, wanted)
        references = confirmed_references(store, account_rows, as_of=as_of)
        for account_id, reference in references.items():
            if account_id not in result:
                if (account_id in uncertain
                        and reference['booked_on'] > reference['reference_booked_on']):
                    reference = {**reference, 'amount': None, 'status': 'ambiguous',
                                 'warning': 'paypal_transaction_date_not_booking_date'}
                result[account_id] = reference

    # File evidence is a fallback only. Any qualified bank or bound statement
    # candidate above retains the existing source/date priority.
    # Transaction dates cannot prove which booked-day interval was complete.
    # Exclude the whole account from file fallback; independent recorded bank
    # and statement controls above remain usable without ledger derivation.
    file_only = set(wanted) - set(result) - transaction_date_accounts(store, wanted)
    if file_only:
        ids = sorted(file_only)
        placeholders = ','.join('?' for _ in ids)
        file_parameters = (*ids, as_of) if as_of is not None else tuple(ids)
        file_rows = store.db.execute(
            'SELECT m.account_id,m.balance_amount,m.currency,m.booked_on,'
            'm.external_id,m.source_sha256,m.source_account,m.source_reference,'
            't.amount AS transaction_amount '
            'FROM finanzguru_row_metadata m JOIN active_transactions t '
            'ON t.account_id=m.account_id AND t.external_id=m.external_id '
            'AND t.date=m.booked_on AND t.currency=m.currency '
            'WHERE m.balance_amount IS NOT NULL '
            f'AND m.account_id IN ({placeholders})'
            + (' AND m.booked_on<=?' if as_of is not None else ''), file_parameters)
        by_account = {}
        for row in file_rows:
            if row['currency'] != wanted[row['account_id']]:
                continue
            # Bind the source row's compound primary key and all facts used
            # for comparison/day-end proof, including the matched ledger posting.
            facts = {key: row[key] for key in (
                'account_id', 'external_id', 'source_sha256', 'source_account',
                'source_reference', 'balance_amount', 'booked_on', 'currency', 'transaction_amount')}
            encoded = json.dumps(facts, sort_keys=True, separators=(',', ':')).encode('utf-8')
            row_reference = 'finanzguru_row:' + sha256(encoded).hexdigest()
            candidate = _candidate(row['account_id'], row['currency'], row['balance_amount'],
                                   row['booked_on'], None, 'finanzguru_file', row_reference)
            if candidate is not None:
                by_account.setdefault(candidate[0], []).append(candidate[1])
        for account_id, values in by_account.items():
            latest_date = max(value['booked_on'] for value in values)
            latest = [value for value in values if value['booked_on'] == latest_date]
            amounts = {value['amount'] for value in latest}
            from .finanzguru_day_end import accepted_day_end
            day_end = accepted_day_end(store, account_id, wanted[account_id], latest_date)
            if day_end is not None:
                result[account_id] = {**latest[0], 'amount': day_end, 'day_end_proven': True}
            elif len(amounts) > 1:
                result[account_id] = {
                    'amount': None, 'currency': wanted[account_id], 'booked_on': latest_date,
                    'retrieved_at': None, 'source': 'finanzguru_file', 'status': 'ambiguous',
                    'warning': 'conflicting_finanzguru_balances_on_same_date',
                }
            else:
                result[account_id] = dict(latest[0])
            file_facts = {
                'rows': sorted(value['control_reference'] for value in latest),
                'amount': result[account_id]['amount'],
                'day_end_proven': result[account_id].get('day_end_proven', False),
                'status': result[account_id].get('status'),
            }
            encoded = json.dumps(file_facts, sort_keys=True, separators=(',', ':')).encode('utf-8')
            result[account_id]['control_reference'] = 'finanzguru_file:' + sha256(encoded).hexdigest()
    if not include_evidence:
        for value in result.values():
            value.pop('control_reference', None)
            value.pop('conflicting_controls', None)
    return result
