"""Persisted, explicit account-opening intake for a staged FinanzGuru export.

This module deliberately does not create transactions.  A draft is a single,
versioned SQLite record whose source remains bound to the reviewed stage.
"""
import json
from datetime import date
from decimal import Decimal, InvalidOperation

from .core import parse_money_input, valid_identifier, valid_person_id
from .finanzguru_import import load_batch

_KINDS = {'CHECKING', 'SAVINGS', 'CREDIT_CARD', 'DEPOT'}
_MAX_LABEL = 120
_MAX_NOTE = 2_000
_PAGE = 50


def _text(value, field, maximum=_MAX_LABEL, required=True):
    if not isinstance(value, str):
        raise TypeError('invalid_' + field)
    result = value.strip()
    if (required and not result) or len(result) > maximum:
        raise ValueError('invalid_' + field)
    return result


def _date(value, field):
    try:
        return date.fromisoformat(_text(value, field, 10)).isoformat()
    except ValueError as error:
        raise ValueError('invalid_' + field) from error


def _amount(value, field, nullable=True):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value.strip():
        if nullable and isinstance(value, str) and not value.strip():
            return None
        raise ValueError('invalid_' + field)
    try:
        return format(parse_money_input(value), '.2f')
    except ValueError as error:
        raise ValueError('invalid_' + field) from error


def _shares(value):
    if not isinstance(value, dict) or not value:
        raise ValueError('invalid_shares')
    result = {}
    try:
        for person, share in value.items():
            if not valid_person_id(person) or not isinstance(share, str):
                raise ValueError
            amount = Decimal(share)
            if not amount.is_finite() or amount <= 0:
                raise ValueError
            result[person] = format(amount.normalize(), 'f')
        if sum((Decimal(value) for value in result.values()), Decimal(0)) != 1:
            raise ValueError
    except (InvalidOperation, ValueError) as error:
        raise ValueError('invalid_shares') from error
    return result


def _account(value):
    if not isinstance(value, dict):
        raise TypeError('invalid_account')
    required = {'key', 'source_account', 'source_name', 'institution', 'owner', 'shares', 'kind',
                'opening_candidate', 'opening', 'opening_confirmed', 'balance_status',
                'source_day', 'note'}
    if set(value) != required:
        raise ValueError('invalid_account_fields')
    account = {
        'key': _text(value['key'], 'key'),
        'source_account': _text(value['source_account'], 'source_account'),
        'source_name': _text(value['source_name'], 'source_name', required=False),
        'institution': _text(value['institution'], 'institution'),
        'owner': _text(value['owner'], 'owner', 64),
        'shares': _shares(value['shares']),
        'kind': _text(value['kind'], 'kind', 32),
        'opening_candidate': _amount(value['opening_candidate'], 'opening_candidate'),
        'opening': _amount(value['opening'], 'opening'),
        'opening_confirmed': value['opening_confirmed'],
        'balance_status': _text(value['balance_status'], 'balance_status', 64),
        'source_day': None if value['source_day'] is None else _date(value['source_day'], 'source_day'),
        'note': _text(value['note'], 'note', _MAX_NOTE, required=False),
    }
    if type(account['opening_confirmed']) is not bool or account['kind'] not in _KINDS:
        raise ValueError('invalid_account')
    people = set(account['shares'])
    owner = next(iter(people)) if len(people) == 1 else 'JOINT'
    if account['owner'] != owner:
        raise ValueError('invalid_ownership')
    if account['opening_confirmed'] and account['opening'] is None:
        raise ValueError('confirmed_opening_required')
    return account


def _stage(root, batch):
    _, staged = load_batch(root, batch)
    if staged.get('blockers'):
        raise ValueError('staged_batch_blocked')
    return staged


def _validate_payload(root, value):
    if not isinstance(value, dict) or set(value) != {'batch', 'source_label', 'opening_date', 'accounts'}:
        raise ValueError('invalid_payload')
    batch = _text(value['batch'], 'batch', 32)
    staged = _stage(root, batch)
    accounts = [_account(account) for account in value['accounts']] if isinstance(value['accounts'], list) else None
    if accounts is None or not accounts:
        raise ValueError('invalid_accounts')
    if len({account['key'] for account in accounts}) != len(accounts):
        raise ValueError('duplicate_account_key')
    expected = {row['source_account'] for row in staged['rows']}
    actual = {account['source_account'] for account in accounts}
    if actual != expected or len(actual) != len(accounts):
        raise ValueError('source_accounts_must_match_stage')
    return ({'batch': batch, 'source_label': _text(value['source_label'], 'source_label'),
             'opening_date': _date(value['opening_date'], 'opening_date'), 'accounts': accounts}, staged)


def _read(store):
    row = store.db.execute('SELECT batch,source_sha256,payload,revision,activated FROM intake_drafts WHERE id=1').fetchone()
    if row is None:
        raise KeyError('intake_draft')
    return row, json.loads(row['payload'])


def _result(payload, revision, staged, activated=False):
    accounts = payload['accounts']
    unresolved = sum(not account['opening_confirmed'] or account['opening'] is None for account in accounts)
    originals = len(staged['rows'])
    draft = payload | {'revision': revision, 'source_rows': originals + staged.get('excluded_split_children', 0),
                       'original_rows': originals, 'excluded_split_children': staged.get('excluded_split_children', 0),
                       'split_summary': staged.get('split_summary', {}), 'ready': unresolved == 0,
                       'unresolved': unresolved, 'ledger_written': False, 'activated': bool(activated)}
    return {'draft': draft}


def initialize(store, root, payload):
    """Create the one draft.  Metadata is immutable after this call."""
    payload, staged = _validate_payload(root, payload)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    store.db.execute('BEGIN IMMEDIATE')
    try:
        existing = store.db.execute('SELECT batch,source_sha256,payload,revision,activated FROM intake_drafts WHERE id=1').fetchone()
        if existing:
            if (existing['batch'] != payload['batch'] or existing['source_sha256'] != staged['source_sha256']
                    or existing['payload'] != encoded):
                raise ValueError('intake_draft_already_exists')
            revision = existing['revision']
            activated = existing['activated']
        else:
            store.db.execute('INSERT INTO intake_drafts(id,batch,source_sha256,payload,revision,activated) VALUES (1,?,?,?,?,0)',
                             (payload['batch'], staged['source_sha256'], encoded, 1))
            revision, activated = 1, False
        store.db.commit()
    except Exception:
        store.db.rollback()
        raise
    return _result(payload, revision, staged, activated)


def load(store, root):
    try:
        row, payload = _read(store)
    except KeyError:
        return {'draft': None}
    staged = _stage(root, row['batch'])
    if staged['source_sha256'] != row['source_sha256']:
        raise ValueError('source_changed')
    _validate_payload(root, payload)
    return _result(payload, row['revision'], staged, row['activated'])


def save(store, root, data):
    if not isinstance(data, dict) or set(data) != {'revision', 'accounts'} or type(data['revision']) is not int:
        raise ValueError('invalid_save')
    updates = data['accounts']
    if not isinstance(updates, list):
        raise TypeError('invalid_accounts')
    by_key = {}
    for value in updates:
        if not isinstance(value, dict) or set(value) != {'key', 'opening', 'opening_confirmed'}:
            raise ValueError('invalid_account_update')
        key = _text(value['key'], 'key')
        if key in by_key or type(value['opening_confirmed']) is not bool:
            raise ValueError('invalid_account_update')
        opening = _amount(value['opening'], 'opening')
        if value['opening_confirmed'] and opening is None:
            raise ValueError('confirmed_opening_required')
        by_key[key] = (opening, value['opening_confirmed'])
    store.db.execute('BEGIN IMMEDIATE')
    try:
        row, payload = _read(store)
        staged = _stage(root, row['batch'])
        if staged['source_sha256'] != row['source_sha256']:
            raise ValueError('source_changed')
        if row['activated']:
            raise ValueError('activated_draft_is_immutable')
        if set(by_key) != {account['key'] for account in payload['accounts']}:
            raise ValueError('account_keys_must_match')
        for account in payload['accounts']:
            account['opening'], account['opening_confirmed'] = by_key[account['key']]
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        changed = store.db.execute('UPDATE intake_drafts SET payload=?, revision=revision+1, activated=0 '
                                  'WHERE id=1 AND revision=?', (encoded, data['revision'])).rowcount
        if changed != 1:
            raise ValueError('stale_revision')
        revision = store.db.execute('SELECT revision FROM intake_drafts WHERE id=1').fetchone()[0]
        store.db.commit()
    except Exception:
        store.db.rollback()
        raise
    return _result(payload, revision, staged)


def rows(store, root, data):
    if not isinstance(data, dict) or set(data) - {'page', 'account_key'} or type(data.get('page')) is not int or data['page'] < 0:
        raise ValueError('invalid_rows_request')
    loaded = load(store, root)
    if loaded['draft'] is None:
        raise KeyError('intake_draft')
    result = loaded['draft']
    by_source = {account['source_account']: account for account in result['accounts']}
    selected = data.get('account_key')
    if selected is not None and selected not in {account['key'] for account in result['accounts']}:
        raise ValueError('unknown_account_key')
    staged = _stage(root, result['batch'])
    annotated = []
    for source in staged['rows']:
        account = by_source[source['source_account']]
        if selected is None or account['key'] == selected:
            before = source['date'] <= result['opening_date']
            annotated.append(dict(source) | {'account_key': account['key'], 'source_name': account['source_name'],
                                              'before_or_on_opening': before,
                                              'status': 'before_or_on_opening' if before else 'after_opening'})
    start = data['page'] * _PAGE
    total = len(annotated)
    return {'page': data['page'], 'pages': (total + _PAGE - 1) // _PAGE,
            'total': total, 'rows': annotated[start:start + _PAGE]}


def activate(store, root, data, *, household='HOUSEHOLD', people=None):
    if not valid_identifier(household):
        raise ValueError('invalid_household')
    known_people = {'ANDREAS', 'ERLENE'} if people is None else set(people)
    if (not known_people or any(not valid_person_id(person) for person in known_people)):
        raise ValueError('invalid_people')
    if not isinstance(data, dict) or set(data) != {'revision', 'confirmed'} or type(data['revision']) is not int or data['confirmed'] is not True:
        raise ValueError('explicit_activation_confirmation_required')
    store.db.execute('BEGIN IMMEDIATE')
    try:
        row, payload = _read(store)
        staged = _stage(root, row['batch'])
        if staged['source_sha256'] != row['source_sha256']:
            raise ValueError('source_changed')
        current = store.db.execute('SELECT revision,activated FROM intake_drafts WHERE id=1').fetchone()
        if current['activated'] and current['revision'] in {data['revision'], data['revision'] + 1}:
            existing = {item[0] for item in store.db.execute('SELECT id FROM accounts')}
            expected = {account['key'] for account in payload['accounts']}
            if expected <= existing:
                store.db.commit()
                return {'batch': payload['batch'], 'mapping': {a['source_account']: a['key'] for a in payload['accounts']},
                        'activated': True, 'ledger_written': False}
        if any(not set(account['shares']) <= known_people for account in payload['accounts']):
            raise ValueError('unknown_household_person')
        if current['revision'] != data['revision']:
            raise ValueError('stale_revision')
        if any(not account['opening_confirmed'] or account['opening'] is None for account in payload['accounts']):
            raise ValueError('all_openings_must_be_confirmed')
        # Reuse the Store's own account validation against an isolated database.
        from .core import Store
        checker = Store()
        try:
            for account in payload['accounts']:
                checker.add_account(account['key'], account['owner'], account['shares'], account['opening'],
                                    payload['opening_date'], account['institution'], account['kind'])
        finally:
            checker.close()
        keys = [account['key'] for account in payload['accounts']]
        existing_dates = {item[0] for item in store.db.execute('SELECT DISTINCT opening_date FROM accounts')}
        if existing_dates and existing_dates != {payload['opening_date']}:
            raise ValueError('common_opening_date_required')
        placeholders = ','.join('?' * len(keys))
        existing = {item[0] for item in store.db.execute(
            f'SELECT id FROM accounts WHERE id IN ({placeholders})', keys)}
        if existing:
            raise ValueError('target_account_exists')
        store.db.execute('INSERT OR IGNORE INTO households(id) VALUES (?)', (household,))
        for account in payload['accounts']:
            store.db.executemany('INSERT OR IGNORE INTO persons(id) VALUES (?)',
                                 [(person,) for person in account['shares']])
            store.db.execute('INSERT OR IGNORE INTO institutions(id) VALUES (?)', (account['institution'],))
            store.db.execute('INSERT INTO accounts(id,institution,kind,owner,household,currency,opening,opening_date) VALUES (?,?,?,?,?,?,?,?)',
                             (account['key'], account['institution'], account['kind'], account['owner'], household, 'EUR', account['opening'], payload['opening_date']))
            store.db.executemany('INSERT INTO ownership(account_id,person_id,share) VALUES (?,?,?)',
                                [(account['key'], person, share) for person, share in account['shares'].items()])
        store.db.execute('UPDATE intake_drafts SET activated=1, revision=revision+1 WHERE id=1')
        store.db.commit()
    except Exception:
        store.db.rollback()
        raise
    return {'batch': payload['batch'], 'mapping': {a['source_account']: a['key'] for a in payload['accounts']},
            'activated': True, 'ledger_written': False}
