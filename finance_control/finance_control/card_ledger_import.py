"""Repeat-safe C.12 bookings; callers own authorization and the SQL transaction."""
from collections import Counter
from datetime import date
from hashlib import sha256
import csv
import io
import json
import re

from .core import money


def _hash(value):
    return sha256(json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True).encode()).hexdigest()


def _text(value):
    return ' '.join((value or '').casefold().split())


def apply(store, source_key, account_id, result, retrieved_at):
    if (not store.db.in_transaction or not re.fullmatch('[0-9a-f]{64}', source_key)
            or result.get('status') != 'ok'):
        raise ValueError('invalid card import boundary')
    source = store.db.execute('SELECT account_id,card_id FROM card_sources WHERE source_key=?', (source_key,)).fetchone()
    account = store.db.execute('SELECT * FROM accounts WHERE id=?', (account_id,)).fetchone()
    if (source is None or source['account_id'] != account_id or account is None
            or account['kind'] != 'CREDIT_CARD' or account['currency'] != 'EUR'):
        raise ValueError('invalid card import target')
    occurrences, claimed, planned = Counter(), set(), []
    for row in result['transactions']:
        booked, amount = row['booking_date'], format(money(row['amount']), '.2f')
        if (row['currency'] != 'EUR' or date.fromisoformat(booked).isoformat() != booked
                or booked > date.today().isoformat()):
            raise ValueError('invalid card booking')
        if booked <= account['opening_date']:
            continue  # Already represented by the explicitly saved opening balance.
        merchant = row.get('merchant_name') or ''
        description = ' \u00b7 '.join(part for pair in row.get('descriptions', []) for part in pair if part)
        reference = row.get('booking_reference')
        if reference:
            identity = ['reference', reference]
        else:
            identity = ['fields', booked, row.get('receipt_date'), amount, row['currency'],
                        _text(merchant), _text(description), row.get('terminal_id')]
        identity_hash = _hash(identity)
        occurrences[identity_hash] += 1
        # Reference duplication is ambiguous; fields-only duplicates retain their multiplicity.
        if reference and occurrences[identity_hash] > 1:
            raise ValueError('ambiguous card booking reference')
        bank_key = (('ref:' + identity_hash) if reference else
                    f'fields:{identity_hash}:{occurrences[identity_hash]}')
        known = store.db.execute('SELECT k.* FROM card_booking_links k JOIN card_sources s '
                                 'ON s.source_key=k.source_key WHERE s.card_id=? AND k.account_id=? '
                                 'AND k.bank_key=? LIMIT 1',
                                 (source['card_id'], account_id, bank_key)).fetchone()
        if known is not None:
            existing = store.db.execute('SELECT * FROM active_transactions WHERE account_id=? AND external_id=?',
                                        (account_id, known['external_id'])).fetchone()
            if (existing is None or (existing['date'], existing['amount'], existing['currency'])
                    != (booked, amount, 'EUR')):
                raise ValueError('changed or removed imported card booking')
            claimed.add(known['external_id'])
            continue  # Never overwrite categories, transfer flags or receipt links.
        linked_same = store.db.execute(
            'SELECT k.bank_key,s.card_id FROM card_booking_links k JOIN card_sources s ON s.source_key=k.source_key '
            'WHERE k.account_id=? AND k.booking_date=? AND k.amount=? AND k.currency=?',
            (account_id, booked, amount, 'EUR')).fetchall()
        if any(r['card_id'] != source['card_id'] or (reference and not r['bank_key'].startswith('ref:')) or
               (not reference and not r['bank_key'].startswith('fields:' + identity_hash + ':'))
               for r in linked_same):
            raise ValueError('card existing booking review required')
        candidates = store.db.execute(
            'SELECT t.external_id,c.counterparty,c.description FROM active_transactions t '
            'LEFT JOIN transaction_context c ON c.account_id=t.account_id AND c.external_id=t.external_id '
            'WHERE t.account_id=? AND t.date=? AND t.amount=? AND t.currency=? '
            'AND NOT EXISTS (SELECT 1 FROM card_booking_links k WHERE k.account_id=t.account_id '
            'AND k.external_id=t.external_id)', (account_id, booked, amount, 'EUR')).fetchall()
        candidates = [c for c in candidates if c['external_id'] not in claimed]
        def text_matches(candidate):
            pairs = [(merchant, candidate['counterparty']), (description, candidate['description'])]
            comparable = [(left, right) for left, right in pairs if left and right]
            return bool(comparable) and all(_text(left) == _text(right) for left, right in comparable)
        matches = [c for c in candidates if text_matches(c)]
        if candidates and len(matches) != 1:
            raise ValueError('card existing booking review required')
        external = matches[0]['external_id'] if matches else 'c12-' + _hash([source_key, bank_key])
        claimed.add(external)
        planned.append((bank_key, external, booked, amount, merchant, description, bool(matches)))
    # All validation and ambiguity checks precede writes. The caller rolls back on any error.
    output = io.StringIO(); writer = csv.writer(output)
    writer.writerow(['external_id','account_id','date','amount','currency','category','transfer_id'])
    for _, external, booked, amount, _, _, existing in planned:
        if not existing:
            writer.writerow([external, account_id, booked, amount, 'EUR', 'Nicht zugeordnet', ''])
    inserted = store.import_csv(output.getvalue(), manage_transaction=False) if any(not r[-1] for r in planned) else 0
    for bank_key, external, booked, amount, merchant, description, existing in planned:
        store.db.execute('INSERT INTO card_booking_links VALUES (?,?,?,?,?,?,?)',
                         (source_key, bank_key, account_id, external, booked, amount, 'EUR'))
        if not existing:
            store.db.execute('INSERT INTO transaction_context VALUES (?,?,?,?)',
                             (account_id, external, merchant, description))
    balance = result.get('balance')
    if balance is not None:
        if (balance['currency'] != 'EUR' or date.fromisoformat(balance['as_of']).isoformat() != balance['as_of']
                or balance['as_of'] > date.today().isoformat()):
            raise ValueError('invalid card balance')
        store.db.execute('INSERT OR IGNORE INTO card_balance_observations '
                         '(source_key,account_id,amount,currency,booked_on,retrieved_at) VALUES (?,?,?,?,?,?)',
                         (source_key, account_id, format(money(balance['amount']), '.2f'), 'EUR',
                          balance['as_of'], retrieved_at))
    return {'inserted': inserted, 'balance_saved': balance is not None}
