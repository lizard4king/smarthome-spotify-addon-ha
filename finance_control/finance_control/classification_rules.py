"""Rule policy and explicit, revision-checked management of automatic effects."""
import hashlib
import json
import re
from datetime import date


DECISION_ACTIONS = (
    'classification_saved', 'classification_reset', 'classification_auto_applied',
    'classification_model_auto_confirmed', 'snapshot_category_reset',
    'classification_rule_effect_revoked', 'model_suggestion_rejected',
)


def is_intermediary(counterparty, description=''):
    """Payment intermediaries and marketplace families only support proposals."""
    text = ' '.join((counterparty or '', description or '')).casefold()
    return bool(re.search(
        r'paypal|klarna|amazon|\bamzn|\bgoogle\b|\bapple\b|sammel(?:abbuchung|lastschrift|zahlung)|'
        r'payment\s*(?:service|collection)|stripe|adyen|mollie|sumup|worldline', text))


def canonical_rule_date(value, name):
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value):
        raise ValueError(f'invalid_{name}')
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f'invalid_{name}') from error
    if parsed.isoformat() != value:
        raise ValueError(f'invalid_{name}')
    return value


def rule_is_eligible(rule, booked_on=None):
    if not rule['enabled']:
        return False
    if booked_on is None:
        return rule['valid_from'] is None and rule['valid_to'] is None
    booked_on = canonical_rule_date(booked_on, 'booked_on')
    return ((rule['valid_from'] is None or rule['valid_from'] <= booked_on)
            and (rule['valid_to'] is None or booked_on <= rule['valid_to']))


def eligible_rules(store, normalized, direction, booked_on=None):
    """Missing booking dates admit only unbounded rules, never today's validity."""
    if booked_on is not None:
        canonical_rule_date(booked_on, 'booked_on')
    return [dict(row) for row in store.db.execute(
        'SELECT r.*,c.parent_id,c.label FROM classification_rules r '
        'JOIN category_catalog c ON c.id=r.category_id '
        'WHERE r.counterparty_normalized=? AND r.direction=? ORDER BY r.id',
        (normalized, direction)) if rule_is_eligible(row, booked_on)]


def latest_decisions(store):
    """Select the last decision FIRST; only callers then filter its origin."""
    placeholders = ','.join('?' for _ in DECISION_ACTIONS)
    return {(row['account_id'], row['external_id']): dict(row) for row in store.db.execute(
        'SELECT a.* FROM classification_audit a JOIN ('
        'SELECT account_id,external_id,MAX(id) AS id FROM classification_audit '
        f'WHERE action IN ({placeholders}) AND account_id IS NOT NULL '
        'GROUP BY account_id,external_id) last ON last.id=a.id', DECISION_ACTIONS)}


def latest_decision(store, account_id, external_id):
    """Read one identity's latest relevant decision using the identity index."""
    placeholders = ','.join('?' for _ in DECISION_ACTIONS)
    row = store.db.execute(
        'SELECT * FROM classification_audit WHERE account_id=? AND external_id=? '
        f'AND action IN ({placeholders}) ORDER BY id DESC LIMIT 1',
        (account_id, external_id, *DECISION_ACTIONS)).fetchone()
    return None if row is None else dict(row)


def _effect_index(store):
    """One audit scan for all rule counts; no inferred legacy provenance."""
    decisions = latest_decisions(store)
    active = {(row['account_id'], row['external_id']): dict(row) for row in store.db.execute(
        'SELECT t.account_id,t.external_id,t.revision AS transaction_revision,'
        'o.category_id,o.confirmed,o.revision FROM active_transaction_versions t '
        'JOIN classification_overrides o USING(account_id,external_id)')}
    effects, legacy = {}, {}
    for key, event in decisions.items():
        if event['action'] != 'classification_auto_applied' or key not in active:
            continue
        # Corrupt latest evidence blocks older origins; never fall back to them.
        try:
            payload = json.loads(event['current'])
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        rule_id = payload.get('rule_id')
        if type(rule_id) is not int:
            continue
        row = active[key]
        if (row['confirmed'] != 1 or payload.get('confirmed') is not True
                or row['category_id'] != payload.get('category')
                or row['revision'] != payload.get('revision')):
            continue
        if (type(payload.get('transaction_revision')) is not int
                or type(payload.get('rule_revision')) is not int):
            legacy[rule_id] = legacy.get(rule_id, 0) + 1
            continue
        if row['transaction_revision'] != payload['transaction_revision']:
            continue
        effects.setdefault(rule_id, []).append({
            **row, 'auto_event_id': event['id'], 'rule_revision': payload['rule_revision']})
    return effects, legacy


def _record(row):
    return {'id': row['id'], 'counterparty': row['counterparty_normalized'],
            'direction': row['direction'], 'category': row['category_id'],
            'parent_category': row['parent_id'], 'category_label': row['label'],
            'revision': row['revision'], 'enabled': bool(row['enabled']),
            'valid_from': row['valid_from'], 'valid_to': row['valid_to']}


def _rule(store, rule_id, revision=None):
    if type(rule_id) is not int or rule_id < 1:
        raise ValueError('invalid_rule_id')
    row = store.db.execute(
        'SELECT r.*,c.label,c.parent_id FROM classification_rules r '
        'JOIN category_catalog c ON c.id=r.category_id WHERE r.id=?', (rule_id,)).fetchone()
    if row is None:
        raise ValueError('unknown_rule')
    if revision is not None and (type(revision) is not int or revision != row['revision']):
        raise ValueError('stale_revision')
    return _record(row)


def validate_rule_fields(data):
    from .classification import normalize_counterparty
    if data['direction'] not in ('income', 'expense'):
        raise ValueError('invalid_rule_direction')
    enabled = data.get('enabled', True)
    if type(enabled) is not bool:
        raise ValueError('invalid_rule_enabled')
    start = canonical_rule_date(data.get('valid_from'), 'valid_from')
    end = canonical_rule_date(data.get('valid_to'), 'valid_to')
    if start is not None and end is not None and start > end:
        raise ValueError('invalid_rule_date_range')
    return normalize_counterparty(data['counterparty']), data['direction'], int(enabled), start, end


def list_rules(store, data):
    from .classification import _catalog
    if data != {}:
        raise ValueError('invalid_rule_list_request')
    effects, legacy = _effect_index(store)
    rows = [_record(row) for row in store.db.execute(
        'SELECT r.*,c.label,c.parent_id FROM classification_rules r '
        'JOIN category_catalog c ON c.id=r.category_id ORDER BY r.id')]
    for row in rows:
        row.update(auto_count=len(effects.get(row['id'], [])),
                   legacy_ambiguous_count=legacy.get(row['id'], 0))
    return {'rules': rows, 'categories': _catalog(store)}


def update_rule(store, data):
    from .classification import _audit, _category
    required = {'id', 'revision', 'counterparty', 'direction', 'category', 'enabled',
                'valid_from', 'valid_to', 'confirmed'}
    if not isinstance(data, dict) or set(data) != required or data['confirmed'] is not True:
        raise ValueError('explicit_rule_update_confirmation_required')
    normalized, direction, enabled, start, end = validate_rule_fields(data)
    store.db.execute('BEGIN IMMEDIATE')
    try:
        previous = _rule(store, data['id'], data['revision'])
        category = _category(store, data['category'], direction)
        store.db.execute('UPDATE classification_rules SET counterparty_normalized=?,direction=?,'
                         'category_id=?,enabled=?,valid_from=?,valid_to=?,revision=revision+1 WHERE id=?',
                         (normalized, direction, category['id'], enabled, start, end, data['id']))
        current = _rule(store, data['id'])
        _audit(store, 'rule_updated', current, previous=previous)
        store.db.commit()
    except Exception:
        store.db.rollback()
        raise
    return {'rule': current}


def _preview(store, rule_id, revision):
    rule = _rule(store, rule_id, revision)
    effects, legacy = _effect_index(store)
    rows = effects.get(rule_id, [])
    ambiguous = legacy.get(rule_id, 0)
    facts = {'rule': rule, 'effects': rows, 'legacy_ambiguous_count': ambiguous}
    token = hashlib.sha256(json.dumps(facts, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {'rule_id': rule_id, 'revision': revision, 'auto_count': len(rows),
            'legacy_ambiguous_count': ambiguous, 'review_token': token}, rows


def effects_preview(store, data):
    if (not isinstance(data, dict) or set(data) != {'id', 'revision'}
            or type(data['revision']) is not int):
        raise ValueError('invalid_rule_effects_request')
    return _preview(store, data['id'], data['revision'])[0]


def revoke_effects(store, data):
    from .classification import _audit
    if (not isinstance(data, dict)
            or set(data) != {'id', 'revision', 'review_token', 'confirmed'}
            or data['confirmed'] is not True or type(data['revision']) is not int
            or not isinstance(data['review_token'], str)):
        raise ValueError('explicit_rule_revoke_confirmation_required')
    store.db.execute('BEGIN IMMEDIATE')
    try:
        preview, rows = _preview(store, data['id'], data['revision'])
        if preview['review_token'] != data['review_token']:
            raise ValueError('stale_review_token')
        for row in rows:
            key = row['account_id'], row['external_id']
            store.db.execute('UPDATE classification_overrides SET category_id=NULL,confirmed=0,'
                             'revision=revision+1 WHERE account_id=? AND external_id=?', key)
            current = {'category': None, 'confirmed': False, 'revision': row['revision'] + 1,
                       'rule_id': data['id'], 'rule_revision': row['rule_revision'],
                       'transaction_revision': row['transaction_revision'],
                       'auto_event_id': row['auto_event_id']}
            _audit(store, 'classification_rule_effect_revoked', current, *key, previous=row)
        store.db.commit()
    except Exception:
        store.db.rollback()
        raise
    return {'revoked': len(rows), 'skipped_legacy': preview['legacy_ambiguous_count']}
