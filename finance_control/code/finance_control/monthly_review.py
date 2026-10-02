"""Immutable, read-only monthly ledger reviews."""
import calendar
import csv
import hashlib
import io
import json
from datetime import UTC, date, datetime
from decimal import Decimal

from .classification import source_context_complete
from .core import money
from .transfer_corrections import effective_transfer_id, transfer_display


def _period_bounds(period):
    if not isinstance(period, str) or len(period) != 7 or period[4] != '-':
        raise ValueError('Period must have the form YYYY-MM')
    try:
        start = date.fromisoformat(period + '-01')
    except ValueError as exc:
        raise ValueError('Period must have the form YYYY-MM') from exc
    if start.strftime('%Y-%m') != period:
        raise ValueError('Period must have the form YYYY-MM')
    end = date(start.year, start.month, calendar.monthrange(start.year, start.month)[1])
    prior_end = start.fromordinal(start.toordinal() - 1)
    return start, end, prior_end


def _text(value):
    return format(money(value), '.2f')


def _source(store, end):
    accounts = []
    for account in store.db.execute('SELECT * FROM accounts ORDER BY id'):
        item = dict(account)
        item['opening'] = _text(item['opening'])
        item['shares'] = [dict(row) for row in store.db.execute(
            'SELECT person_id, share FROM ownership WHERE account_id=? ORDER BY person_id',
            (item['id'],))]
        accounts.append(item)
    transactions = []
    for transaction in store.db.execute(
            'SELECT account_id, external_id, date, amount, currency, category, transfer_id '
            'FROM transactions WHERE date <= ? ORDER BY account_id, date, external_id',
            (end.isoformat(),)):
        item = dict(transaction)
        item['effective_transfer_id'] = effective_transfer_id(store, item)
        item['amount'] = _text(item['amount'])
        transactions.append(item)
    return accounts, transactions


def _checklist(definitions, transactions, start, end, store):
    """Describe the reviewed ledger scope without asserting source completeness."""
    counts = {entry['id']: 0 for entry in definitions}
    for transaction in transactions:
        if start.isoformat() <= transaction['date'] <= end.isoformat():
            counts[transaction['account_id']] += 1
    imports = [dict(row) for row in store.db.execute(
        'SELECT imports.id, imports.imported_at, COUNT(*) AS transaction_count '
        'FROM imports JOIN transactions ON transactions.import_id=imports.id '
        'WHERE transactions.date BETWEEN ? AND ? '
        'GROUP BY imports.id, imports.imported_at ORDER BY imports.id',
        (start.isoformat(), end.isoformat()))]
    late_imports = [entry for entry in imports if entry['imported_at'][:10] > end.isoformat()]
    accounts = [{'id': entry['id'], 'display_name': entry.get('display_name'), 'kind': entry['kind'],
                 'transaction_count': counts[entry['id']]} for entry in definitions]
    cards = [_account_label(entry) for entry in definitions if entry['kind'] == 'CREDIT_CARD']
    depots = [_account_label(entry) for entry in definitions if entry['kind'] == 'DEPOT']
    return {
        'version': 1,
        'period': {'start': start.isoformat(), 'end': end.isoformat(),
                   'calendar_month': True,
                   'note': 'Der Zeitraum umfasst den vollständigen Kalendermonat; '
                           'die Buchungsvollständigkeit wird nicht automatisch bestätigt.'},
        'scope': {'account_count': len(accounts), 'transaction_count': sum(counts.values()),
                  'accounts': accounts},
        'imports': {
            'source_import_count': len(imports),
            'late_import_count': len(late_imports),
            'late_imports': late_imports,
            'status': 'attention' if late_imports else 'review_required',
            'note': (f'{len(late_imports)} Import(e) mit Buchungen dieses Monats wurden erst nach dem Monatsende '
                     'gespeichert.' if late_imports else
                     'Aus dem Buchungsbestand sind keine nach dem Monatsende gespeicherten '
                     'Importe erkennbar. Fehlende Quellen können daraus nicht ausgeschlossen werden.'),
        },
        'account_treatment': {
            'credit_cards': {'account_ids': cards, 'included_in_liquidity': True,
                             'note': 'Kreditkarten sind Teil der Liquidität; negative Salden mindern sie.'},
            'depots': {'account_ids': depots, 'included_in_liquidity': False,
                       'note': 'Depots bleiben sichtbar, sind aber nicht Teil der Liquidität. '
                               'Die Werte sind Buchungsbestände, keine Marktwerte.'},
        },
    }


def _transaction_rows(store, start, end):
    """Expose the exact bookings that a user must inspect before confirming a month."""
    rows = []
    query = """SELECT t.account_id,
        CASE WHEN NULLIF(TRIM(a.display_name),'') IS NULL THEN t.account_id
             ELSE t.account_id || ' · ' || a.display_name END AS account_label,
        t.external_id,t.date,t.amount,t.currency,t.category AS source_category,t.transfer_id,
        c.counterparty,c.description,o.category_id,o.confirmed,o.revision,cat.label AS category_label
        FROM transactions t
        JOIN accounts a ON a.id=t.account_id
        LEFT JOIN transaction_context c ON (c.account_id=t.account_id AND c.external_id=t.external_id)
        LEFT JOIN classification_overrides o ON (o.account_id=t.account_id AND o.external_id=t.external_id)
        LEFT JOIN category_catalog cat ON cat.id=o.category_id
        WHERE t.date BETWEEN ? AND ? ORDER BY t.date DESC,t.account_id,t.external_id"""
    for row in store.db.execute(query, (start.isoformat(), end.isoformat())):
        item = dict(row)
        transfer = effective_transfer_id(store, item)
        links = [dict(link) for link in store.db.execute(
            'SELECT d.id,d.kind,d.vendor,d.title,l.allocated_amount,l.allocation_type '
            'FROM classification_document_links l JOIN classification_documents d ON d.id=l.document_id '
            'WHERE l.account_id=? AND l.external_id=? ORDER BY d.id',
            (item['account_id'], item['external_id']))]
        category_label = item.pop('category_label')
        category_id = item.pop('category_id')
        item.update({
            'amount': _text(item['amount']),
            'is_transfer': bool(transfer),
            'direction': 'transfer' if transfer else ('expense' if money(item['amount']) < 0 else 'income'),
            'category': 'Umbuchung' if transfer else (category_label if item['confirmed'] else None),
            'category_id': category_id if item['confirmed'] and not transfer else None,
            'confirmed': bool(transfer or item['confirmed']),
            'revision': item['revision'] or 0,
            'links': links,
            'source_context_complete': source_context_complete(
                item['counterparty'], item['description']),
        })
        item['transfer_display'] = transfer_display(store, item) if transfer else None
        rows.append(item)
    return rows


def _review_digest_rows(rows):
    """Keep visible review semantics while ignoring optimistic-lock counters."""
    return [{key: value for key, value in row.items() if key != 'revision'} for row in rows]


def _legacy_review_rows(rows):
    """Reproduce the visible transaction shape persisted before digest version 2."""
    return [{key: value for key, value in row.items()
             if key not in {'revision', 'category_id'}} for row in rows]


def _build_preview(store, period):
    start, end, prior_end = _period_bounds(period)
    definitions, transactions = _source(store, end)
    if not definitions:
        raise ValueError('Monthly review requires at least one account')
    opening_dates = {entry['opening_date'] for entry in definitions}
    if len(opening_dates) != 1:
        raise ValueError('Monthly review requires one common opening date')
    opening_date = date.fromisoformat(next(iter(opening_dates)))
    if opening_date > prior_end:
        raise ValueError('Monthly review requires opening balances before the period')

    values = {entry['id']: money(entry['opening']) for entry in definitions}
    income = {entry['id']: Decimal(0) for entry in definitions}
    expenses = {entry['id']: Decimal(0) for entry in definitions}
    transfers = {entry['id']: Decimal(0) for entry in definitions}
    transaction_count = 0
    for entry in transactions:
        amount = money(entry['amount'])
        booked = date.fromisoformat(entry['date'])
        if booked <= prior_end:
            values[entry['account_id']] += amount
        elif start <= booked <= end:
            transaction_count += 1
            if entry['effective_transfer_id']:
                transfers[entry['account_id']] += amount
            elif amount >= 0:
                income[entry['account_id']] += amount
            else:
                expenses[entry['account_id']] -= amount

    account_rows = []
    liquidity = {key: Decimal(0) for key in ('opening', 'income', 'expenses', 'transfers', 'closing')}
    for entry in definitions:
        account_id = entry['id']
        opening = values[account_id]
        closing = opening + income[account_id] - expenses[account_id] + transfers[account_id]
        row = {'id': account_id, 'display_name': entry.get('display_name'),
               'account_label': _account_label(entry), 'kind': entry['kind'], 'opening': _text(opening),
               'income': _text(income[account_id]), 'expenses': _text(expenses[account_id]),
               'transfers': _text(transfers[account_id]), 'closing': _text(closing)}
        account_rows.append(row)
        if entry['kind'] != 'DEPOT':
            for key in liquidity:
                liquidity[key] += money(row[key])

    checklist = _checklist(definitions, transactions, start, end, store)
    transaction_rows = _transaction_rows(store, start, end)
    legacy_source_payload = {'accounts': definitions, 'transactions': transactions}
    legacy_source_digest = hashlib.sha256(json.dumps(
        legacy_source_payload, sort_keys=True, separators=(',', ':'),
        ensure_ascii=False).encode('utf-8')).hexdigest()
    # Version 2 also binds the review to confirmed categories, document links,
    # and resolved transfer counterparts shown to the user. A later visible
    # review change therefore cannot leave a saved monthly snapshot looking
    # current. Pure optimistic-lock revision increments remain irrelevant.
    source_payload = {
        'version': 2,
        'accounts': definitions,
        'transactions': transactions,
        'review_transactions': _review_digest_rows(transaction_rows),
    }
    source_digest = hashlib.sha256(json.dumps(
        source_payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')).hexdigest()
    review_token = hashlib.sha256(json.dumps(
        {'version': 2, 'period': period, 'source_digest': source_digest},
        sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
    return {'version': 1, 'period': period, 'start': start.isoformat(), 'end': end.isoformat(),
            'accounts': account_rows, 'liquidity': {key: _text(value) for key, value in liquidity.items()},
            'transaction_count': transaction_count, 'transactions': transaction_rows,
            'source_digest_version': 2,
            'legacy_source_digest': legacy_source_digest,
            'source_digest': source_digest,
            'review_token': review_token, 'checklist': checklist}


def _account_label(account):
    name = account.get('display_name')
    return f"{account['id']} · {name}" if name else account['id']


def preview(store, period):
    """Return a consistent read snapshot for one complete calendar month."""
    if store.db.in_transaction:
        return _build_preview(store, period)
    store.db.execute('BEGIN')
    try:
        result = _build_preview(store, period)
    finally:
        store.db.rollback()
    return result


def _stored(row):
    snapshot = json.loads(row['payload'])
    return {'id': row['id'], 'revision': row['revision'], 'created_at': row['created_at'], **snapshot}


def source_matches(row, current):
    """Compare a stored v1/v2 review with the current source without migration noise."""
    if row['source_digest'] == current['source_digest']:
        return True
    try:
        stored = json.loads(row['payload'])
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        return False
    if not isinstance(stored, dict):
        return False
    return (
        stored.get('source_digest_version', 1) == 1
        and row['source_digest'] == current['legacy_source_digest']
        and _legacy_review_rows(stored.get('transactions', []))
        == _legacy_review_rows(current['transactions'])
    )


def save(store, period, review_token, confirmed):
    """Confirm and persist a new immutable revision after rechecking the source."""
    if confirmed is not True:
        raise ValueError('Explicit confirmation is required')
    store.db.execute('BEGIN IMMEDIATE')
    try:
        current = _build_preview(store, period)
        if review_token != current['review_token']:
            raise ValueError('Review token is stale')
        latest = store.db.execute(
            'SELECT * FROM monthly_reviews WHERE period=? ORDER BY revision DESC LIMIT 1',
            (period,)).fetchone()
        if latest and source_matches(latest, current):
            raise ValueError('Unchanged source already reviewed')
        revision = 1 if latest is None else latest['revision'] + 1
        created_at = datetime.now(UTC).isoformat()
        payload = json.dumps(current, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
        review_id = store.db.execute(
            'INSERT INTO monthly_reviews(period,revision,created_at,source_digest,payload) VALUES (?,?,?,?,?)',
            (period, revision, created_at, current['source_digest'], payload)).lastrowid
        store.db.commit()
    except Exception:
        store.db.rollback()
        raise
    return {'id': review_id, 'revision': revision, 'created_at': created_at, **current}


def records(store):
    return [_stored(row) for row in store.db.execute(
        'SELECT * FROM monthly_reviews ORDER BY period, revision')]


def get_review(store, review_id):
    row = store.db.execute('SELECT * FROM monthly_reviews WHERE id=?', (review_id,)).fetchone()
    if row is None:
        raise KeyError(review_id)
    return _stored(row)


def _csv_id(value):
    value = str(value)
    return "'" + value if value.lstrip()[:1] in ('=', '+', '-', '@') else value


def export_csv(snapshot):
    """Export one stored review; it deliberately never recalculates ledger data."""
    output = io.StringIO(newline='')
    writer = csv.writer(output, delimiter=';', lineterminator='\r\n')
    writer.writerow(['Zeitraum', 'Konto', 'Art', 'Anfang_EUR', 'Einnahmen_EUR',
                     'Ausgaben_EUR', 'Umbuchungen_EUR', 'Ende_EUR', 'Revision', 'Erstellt_UTC'])
    for row in snapshot['accounts']:
        writer.writerow([snapshot['period'], _csv_id(row['id']), row['kind']] + [
            row[key].replace('.', ',') for key in
            ('opening', 'income', 'expenses', 'transfers', 'closing')] +
            [snapshot.get('revision', ''), snapshot.get('created_at', '')])
    liquidity = snapshot['liquidity']
    writer.writerow([snapshot['period'], 'LIQUIDITAET', 'SUMME'] + [
        liquidity[key].replace('.', ',') for key in
        ('opening', 'income', 'expenses', 'transfers', 'closing')] +
        [snapshot.get('revision', ''), snapshot.get('created_at', '')])
    return output.getvalue()
