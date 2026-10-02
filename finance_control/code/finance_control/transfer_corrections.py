"""Additive, confirmed corrections for imported internal transfers."""
import json
from datetime import UTC, date, datetime

from .core import money


_SOURCE_TRANSFER_CATEGORIES = frozenset({
    'umbuchung', 'umbuchungen', 'kontoumbuchung', 'kontoumbuchungen', 'transfer',
})


def source_declares_transfer(transaction):
    """Recognise an explicit source declaration, never a merchant-name guess."""
    try:
        category = transaction['category']
    except (KeyError, IndexError):
        try:
            category = transaction['source_category']
        except (KeyError, IndexError):
            category = None
    normalized = ' '.join(str(category or '').casefold().split())
    if normalized in _SOURCE_TRANSFER_CATEGORIES:
        return True
    leaf = normalized.rsplit('/', 1)[-1].strip()
    if leaf not in _SOURCE_TRANSFER_CATEGORIES or '/' not in normalized:
        return False
    try:
        description = transaction['description']
    except (KeyError, IndexError):
        description = None
    purpose = ' '.join(str(description or '').casefold().split())
    return purpose in {'umbuchung', 'kontoumbuchung'} or purpose.startswith('haushaltsgeld ')


def sql_transfer_predicate(alias='t', context_alias=None):
    """Return the shared SQL predicate for effective household transfers."""
    categories = ','.join("'" + value + "'" for value in sorted(_SOURCE_TRANSFER_CATEGORIES))
    active_correction = (
        'EXISTS (SELECT 1 FROM transfer_correction_members transfer_member '
        'JOIN transfer_correction_pairs transfer_pair '
        'ON transfer_pair.id=transfer_member.pair_id AND transfer_pair.revoked_at IS NULL '
        f'WHERE transfer_member.account_id={alias}.account_id '
        f'AND transfer_member.external_id={alias}.external_id)')
    single_leg = (
        'EXISTS (SELECT 1 FROM transfer_single_legs transfer_single '
        f'WHERE transfer_single.account_id={alias}.account_id '
        f'AND transfer_single.external_id={alias}.external_id)')
    hierarchy = ''
    if context_alias:
        source = f"replace(lower(trim({alias}.category)), ' ', '')"
        suffixes = ' OR '.join(
            f"{source} LIKE '%/{value}'" for value in sorted(_SOURCE_TRANSFER_CATEGORIES))
        purpose = f"lower(trim(coalesce({context_alias}.description,'')))"
        hierarchy = (f" OR (({suffixes}) AND ({purpose} IN ('umbuchung','kontoumbuchung') "
                     f"OR {purpose} LIKE 'haushaltsgeld %'))")
    return (f"({alias}.transfer_id != '' OR {active_correction} OR {single_leg} "
            f"OR lower(trim({alias}.category)) IN ({categories}){hierarchy})")


def effective_transfer_id(store, transaction):
    """Keep the imported source value intact while exposing an effective transfer ID."""
    if transaction['transfer_id']:
        return transaction['transfer_id']
    if source_declares_transfer(transaction):
        return f"source-category:{transaction['account_id']}:{transaction['external_id']}"
    single = store.db.execute(
        'SELECT 1 FROM transfer_single_legs WHERE account_id=? AND external_id=?',
        (transaction['account_id'], transaction['external_id'])).fetchone()
    if single is not None:
        return f"single:{transaction['account_id']}:{transaction['external_id']}"
    row = store.db.execute(
        'SELECT m.pair_id FROM transfer_correction_members m '
        'JOIN transfer_correction_pairs p ON p.id=m.pair_id AND p.revoked_at IS NULL '
        'WHERE m.account_id=? AND m.external_id=?',
        (transaction['account_id'], transaction['external_id'])).fetchone()
    return None if row is None else f"correction:{row['pair_id']}"


def save_single_leg(store, data):
    """Mark a proven one-sided ledger movement as a transfer without inventing a partner."""
    if (not isinstance(data, dict) or set(data) != {'account_id', 'external_id', 'reason', 'confirmed'}
            or data['confirmed'] is not True or not isinstance(data['reason'], str)
            or not data['reason'].strip() or len(data['reason'].strip()) > 120):
        raise ValueError('invalid_single_leg_transfer')
    if (not isinstance(data['account_id'], str) or not isinstance(data['external_id'], str)
            or len(data['account_id'].strip()) > 120
            or len(data['external_id'].strip()) > 240):
        raise ValueError('invalid_single_leg_transfer')
    key = (data['account_id'].strip(), data['external_id'].strip())
    if not all(key):
        raise ValueError('invalid_single_leg_transfer')
    transaction = store.db.execute(
        'SELECT * FROM transactions WHERE account_id=? AND external_id=?', key).fetchone()
    if transaction is None:
        raise ValueError('unknown_transaction')
    if effective_transfer_id(store, transaction):
        raise ValueError('effective_transfer_already_set')
    created_at = datetime.now(UTC).isoformat()
    current = {'account_id': key[0], 'external_id': key[1],
               'reason': data['reason'].strip(), 'revision': 1,
               'effective_transfer_id': f'single:{key[0]}:{key[1]}'}
    with store.db:
        store.db.execute(
            'INSERT INTO transfer_single_legs(account_id,external_id,reason,created_at,revision) '
            'VALUES (?,?,?,?,1)', (*key, current['reason'], created_at))
        store.db.execute(
            'INSERT INTO transfer_single_leg_audit(occurred_at,action,account_id,external_id,current) '
            'VALUES (?,?,?,?,?)',
            (created_at, 'single_leg_transfer_confirmed', *key,
             json.dumps(current, sort_keys=True)))
    return {'transfer': current}


def transfer_partner(store, transaction):
    """Return the other ledger leg of a proven two-sided transfer, if present."""
    partner = None
    if transaction['transfer_id']:
        partner = store.db.execute(
            'SELECT t.account_id,t.external_id,t.date,t.amount,a.display_name '
            'FROM transactions t JOIN accounts a ON a.id=t.account_id '
            'WHERE t.transfer_id=? AND (t.account_id!=? OR t.external_id!=?) '
            'ORDER BY t.date,t.account_id,t.external_id LIMIT 1',
            (transaction['transfer_id'], transaction['account_id'],
             transaction['external_id'])).fetchone()
    if partner is None:
        partner = store.db.execute(
            'SELECT t.account_id,t.external_id,t.date,t.amount,a.display_name '
            'FROM transfer_correction_members current '
            'JOIN transfer_correction_pairs pair '
            'ON pair.id=current.pair_id AND pair.revoked_at IS NULL '
            'JOIN transfer_correction_members other ON other.pair_id=current.pair_id '
            'AND (other.account_id!=current.account_id '
            'OR other.external_id!=current.external_id) '
            'JOIN transactions t ON t.account_id=other.account_id '
            'AND t.external_id=other.external_id '
            'JOIN accounts a ON a.id=t.account_id '
            'WHERE current.account_id=? AND current.external_id=? '
            'ORDER BY t.date,t.account_id,t.external_id LIMIT 1',
            (transaction['account_id'], transaction['external_id'])).fetchone()
    return None if partner is None else dict(partner)


def transfer_display(store, transaction):
    """Describe the money path with the source and target household accounts."""
    if not effective_transfer_id(store, transaction):
        return None
    own = store.db.execute(
        'SELECT id,display_name FROM accounts WHERE id=?',
        (transaction['account_id'],)).fetchone()
    partner = transfer_partner(store, transaction)

    def label(account):
        display_name = (account['display_name'] or '').strip()
        return (f"{account['account_id']} · {display_name}" if 'account_id' in account.keys()
                and display_name else
                (account['account_id'] if 'account_id' in account.keys() else account['id']))

    own_label = (f"{own['id']} · {own['display_name'].strip()}"
                 if own and (own['display_name'] or '').strip() else transaction['account_id'])
    if partner is None:
        return f'Umbuchung {own_label} · Gegenbuchung fehlt'
    partner_label = label(partner)
    source, target = ((own_label, partner_label) if money(transaction['amount']) < 0
                      else (partner_label, own_label))
    return f'Umbuchung {source} → {target}'


def _key(value):
    if (not isinstance(value, dict) or set(value) != {'account_id', 'external_id', 'revision'}
            or not isinstance(value['account_id'], str) or not value['account_id'].strip()
            or not isinstance(value['external_id'], str) or not value['external_id'].strip()
            or type(value['revision']) is not int or value['revision'] != 0):
        raise ValueError('invalid_transfer_correction_member')
    return value['account_id'].strip(), value['external_id'].strip()


def _transaction(store, key):
    row = store.db.execute('SELECT * FROM transactions WHERE account_id=? AND external_id=?', key).fetchone()
    if row is None:
        raise ValueError('unknown_transaction')
    if row['transfer_id'] or effective_transfer_id(store, row):
        raise ValueError('effective_transfer_already_set')
    return row


def _valid_pair(first, second):
    if (first['account_id'] == second['account_id'] or first['currency'] != 'EUR'
            or second['currency'] != 'EUR' or money(first['amount']) + money(second['amount']) != 0
            or abs((date.fromisoformat(first['date']) - date.fromisoformat(second['date'])).days) > 7):
        raise ValueError('invalid_transfer_correction_pair')


def suggestions(store, data):
    """Suggest only exact EUR counter-postings; callers still confirm a selected pair."""
    if not isinstance(data, dict) or set(data) != {'account_id', 'external_id'}:
        raise ValueError('invalid_transfer_correction_suggestions')
    first = _transaction(store, (data['account_id'], data['external_id']))
    candidates = []
    for second in store.db.execute(
            'SELECT * FROM transactions WHERE account_id != ? AND currency=? AND amount=? '
            'AND transfer_id=\'\' ORDER BY date,account_id,external_id',
            (first['account_id'], first['currency'], format(-money(first['amount']), '.2f'))):
        try:
            _transaction(store, (second['account_id'], second['external_id']))
            _valid_pair(first, second)
        except ValueError:
            continue
        candidates.append({'account_id': second['account_id'], 'external_id': second['external_id'],
                           'revision': 0, 'date': second['date'], 'amount': second['amount'],
                           'confidence': 'high', 'reason': 'exact_eur_opposite_amount_within_7_days'})
    return {'source': {'account_id': first['account_id'], 'external_id': first['external_id'],
                       'revision': 0}, 'suggestions': candidates if len(candidates) == 1 else [],
            'status': 'unique' if len(candidates) == 1 else ('none' if not candidates else 'ambiguous')}


def pending_suggestions(store):
    """List reciprocal unique pairs for the central approval inbox.

    Equal non-empty counterparties usually describe two merchant postings of
    the same amount, not a movement between household accounts.  Those cases
    remain outside the one-click queue.
    """
    rows = [dict(row) for row in store.db.execute(
        "SELECT t.*,coalesce(c.counterparty,'') AS counterparty,"
        "coalesce(c.description,'') AS description "
        "FROM transactions t "
        "LEFT JOIN transaction_context c USING(account_id,external_id) "
        "LEFT JOIN transfer_correction_members m USING(account_id,external_id) "
        "WHERE t.transfer_id='' AND m.pair_id IS NULL AND t.currency='EUR' "
        "ORDER BY t.date,t.account_id,t.external_id"
    )]
    matches = {}
    for first in rows:
        key = (first['account_id'], first['external_id'])
        matches[key] = [second for second in rows
                        if second['account_id'] != first['account_id']
                        and money(first['amount']) + money(second['amount']) == 0
                        and abs((date.fromisoformat(first['date'])
                                 - date.fromisoformat(second['date'])).days) <= 7]
    def unique_nearest(row, candidates):
        if not candidates:
            return None
        distances = [(abs((date.fromisoformat(row['date'])
                           - date.fromisoformat(candidate['date'])).days), candidate)
                     for candidate in candidates]
        minimum = min(distance for distance, _ in distances)
        nearest = [candidate for distance, candidate in distances if distance == minimum]
        return nearest[0] if len(nearest) == 1 else None

    result, seen = [], set()
    for first in rows:
        first_key = (first['account_id'], first['external_id'])
        second = unique_nearest(first, matches[first_key])
        if second is None:
            continue
        second_key = (second['account_id'], second['external_id'])
        if unique_nearest(second, matches[second_key]) is not first:
            continue
        pair_key = tuple(sorted((first_key, second_key)))
        if pair_key in seen:
            continue
        seen.add(pair_key)
        first_party = ' '.join(first['counterparty'].casefold().split())
        second_party = ' '.join(second['counterparty'].casefold().split())
        automatic_topup = any(
            money(row['amount']) > 0
            and 'automatische aufladung' in row['description'].casefold()
            for row in (first, second))
        if first_party and first_party == second_party and not automatic_topup:
            continue
        debit, credit = (first, second) if money(first['amount']) < 0 else (second, first)
        result.append({
            'first': {'account_id': debit['account_id'], 'external_id': debit['external_id'],
                      'revision': 0, 'date': debit['date'], 'amount': debit['amount'],
                      'counterparty': debit['counterparty']},
            'second': {'account_id': credit['account_id'], 'external_id': credit['external_id'],
                       'revision': 0, 'date': credit['date'], 'amount': credit['amount'],
                       'counterparty': credit['counterparty']},
            'confidence': 'high' if automatic_topup else 'medium',
            'reason': ('wallet_automatic_topup_with_exact_counterposting'
                       if automatic_topup
                       else 'reciprocal_nearest_eur_opposite_amount_within_7_days'),
        })
    return result


def auto_confirm_wallet_topups(store):
    """Confirm only unique wallet top-ups that have an exact external counter-posting."""
    confirmed = []
    for pair in pending_suggestions(store):
        if pair['reason'] != 'wallet_automatic_topup_with_exact_counterposting':
            continue
        result = save(store, {
            'first': {key: pair['first'][key]
                      for key in ('account_id', 'external_id', 'revision')},
            'second': {key: pair['second'][key]
                       for key in ('account_id', 'external_id', 'revision')},
            'confirmed': True,
        })
        confirmed.append(result['pair']['id'])
    return {'status': 'ok', 'confirmed': len(confirmed), 'pair_ids': confirmed}


def save(store, data):
    if not isinstance(data, dict) or set(data) != {'first', 'second', 'confirmed'} or data['confirmed'] is not True:
        raise ValueError('explicit_transfer_correction_confirmation_required')
    first_key, second_key = _key(data['first']), _key(data['second'])
    if first_key == second_key:
        raise ValueError('invalid_transfer_correction_pair')
    store.db.execute('BEGIN IMMEDIATE')
    try:
        first, second = _transaction(store, first_key), _transaction(store, second_key)
        _valid_pair(first, second)
        created_at = datetime.now(UTC).isoformat()
        pair_id = store.db.execute(
            'INSERT INTO transfer_correction_pairs(created_at,revision) VALUES (?,1)', (created_at,)).lastrowid
        store.db.executemany(
            'INSERT INTO transfer_correction_members(pair_id,account_id,external_id,revision) VALUES (?,?,?,1)',
            [(pair_id, *first_key), (pair_id, *second_key)])
        current = {'id': pair_id, 'revision': 1, 'members': [
            {'account_id': first_key[0], 'external_id': first_key[1], 'revision': 1},
            {'account_id': second_key[0], 'external_id': second_key[1], 'revision': 1}],
            'effective_transfer_id': f'correction:{pair_id}'}
        store.db.execute('INSERT INTO transfer_correction_audit(occurred_at,action,pair_id,current) VALUES (?,?,?,?)',
                         (created_at, 'transfer_correction_confirmed', pair_id,
                          json.dumps(current, sort_keys=True)))
        store.db.commit()
    except Exception:
        store.db.rollback()
        raise
    return {'pair': current}


def get(store, data):
    if (not isinstance(data, dict) or set(data) != {'account_id', 'external_id'}
            or not isinstance(data['account_id'], str) or not isinstance(data['external_id'], str)):
        raise ValueError('invalid_transfer_correction_lookup')
    row = store.db.execute(
        'SELECT p.id,p.revision,p.created_at FROM transfer_correction_members m '
        'JOIN transfer_correction_pairs p ON p.id=m.pair_id AND p.revoked_at IS NULL '
        'WHERE m.account_id=? AND m.external_id=?',
        (data['account_id'].strip(), data['external_id'].strip())).fetchone()
    if row is None:
        return {'pair': None}
    members = [dict(member) for member in store.db.execute(
        'SELECT account_id,external_id,revision FROM transfer_correction_members '
        'WHERE pair_id=? ORDER BY account_id,external_id', (row['id'],))]
    return {'pair': {'id': row['id'], 'revision': row['revision'], 'created_at': row['created_at'],
                     'members': members, 'effective_transfer_id': f"correction:{row['id']}"}}


def revoke(store, data):
    """Neutralize only an active additive pair; source transactions are never written."""
    if (not isinstance(data, dict) or set(data) != {'id', 'revision', 'confirmed'}
            or type(data['id']) is not int or data['id'] < 1 or type(data['revision']) is not int
            or data['confirmed'] is not True):
        raise ValueError('explicit_transfer_correction_revocation_required')
    store.db.execute('BEGIN IMMEDIATE')
    try:
        pair = store.db.execute(
            'SELECT id,revision FROM transfer_correction_pairs WHERE id=? AND revoked_at IS NULL',
            (data['id'],)).fetchone()
        if pair is None or pair['revision'] != data['revision']:
            raise ValueError('stale_transfer_correction_revision')
        members = [dict(member) for member in store.db.execute(
            'SELECT account_id,external_id,revision FROM transfer_correction_members WHERE pair_id=? '
            'ORDER BY account_id,external_id', (pair['id'],))]
        if len(members) != 2:
            raise ValueError('invalid_transfer_correction_members')
        revoked_at = datetime.now(UTC).isoformat()
        if store.db.execute('DELETE FROM transfer_correction_members WHERE pair_id=?',
                            (pair['id'],)).rowcount != 2:
            raise ValueError('stale_transfer_correction_revision')
        store.db.execute('UPDATE transfer_correction_pairs SET revoked_at=? WHERE id=? AND revoked_at IS NULL',
                         (revoked_at, pair['id']))
        current = {'id': pair['id'], 'revision': pair['revision'], 'revoked_at': revoked_at,
                   'members': members}
        store.db.execute('INSERT INTO transfer_correction_audit(occurred_at,action,pair_id,current) VALUES (?,?,?,?)',
                         (revoked_at, 'transfer_correction_revoked', pair['id'],
                          json.dumps(current, sort_keys=True)))
        store.db.commit()
    except Exception:
        store.db.rollback()
        raise
    return {'revoked': current}
