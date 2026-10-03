"""Read-only, centrally shaped pending-approval inbox.

Every direct action deliberately points to an existing revision-checked
operation.  This module never performs a write itself.
"""
from datetime import date, datetime, timedelta

from .classification import (
    _document_match_eligible,
    document_recipient_matches,
    normalize_counterparty,
    source_context_complete,
)
from .core import money
from .transfer_corrections import pending_suggestions, sql_transfer_predicate


def _item(item_type, item_id, title, detail, confidence, target, source_revision,
          direct_action=None):
    """Return the stable queue-item contract used by future frontend clients."""
    return {
        'type': item_type,
        'id': item_id,
        'title': title,
        'detail': detail,
        'confidence': confidence,
        'direct_action': direct_action,
        'target': target,
        'source_revision': source_revision,
    }


def _action(route, payload):
    return {'route': route, 'payload': payload}


def _model_decision_note(category, confidence):
    """Explain why a persisted model proposal still needs a person."""
    if any(marker in category for marker in ('UNKLAR', 'SONSTIGE')):
        return ('Manuelle Entscheidung nötig: Der Vorschlag ist ausdrücklich '
                'unklar oder eine Sammelkategorie.')
    if confidence != 'high':
        return ('Manuelle Entscheidung nötig: Das Modell meldet keine hohe '
                'Sicherheit.')
    return ('Manuelle Entscheidung nötig: Es fehlen zwei gleichartige, bereits '
            'von Dir bestätigte Vergleichsbuchungen.')


def _category_items(store):
    non_transfer = 'NOT ' + sql_transfer_predicate(context_alias='x')
    rows = store.db.execute(
        f"""SELECT t.account_id,t.external_id,t.date,o.revision,
                  COALESCE(x.counterparty,'') AS counterparty,
                  COALESCE(x.description,'') AS description,t.amount,t.currency,
                  mr.category_id AS model_category,mr.confidence AS model_confidence,
                  mr.rationale AS model_rationale,mr.model AS model_name,
                  CASE WHEN substr(t.amount, 1, 1)='-' THEN 'expense' ELSE 'income' END AS direction
           FROM transactions t
           LEFT JOIN transaction_context x ON (x.account_id=t.account_id AND x.external_id=t.external_id)
           LEFT JOIN classification_overrides o ON (o.account_id=t.account_id AND o.external_id=t.external_id)
           LEFT JOIN classification_model_reviews mr
             ON (mr.account_id=t.account_id AND mr.external_id=t.external_id AND mr.status='proposed')
           LEFT JOIN transfer_correction_members m
             ON (m.account_id=t.account_id AND m.external_id=t.external_id)
           WHERE {non_transfer}
             AND COALESCE(o.confirmed, 0)=0
           ORDER BY t.date DESC,t.account_id,t.external_id""").fetchall()
    rules = {(rule['counterparty_normalized'], rule['direction']): (rule['id'], rule['category_id'])
             for rule in store.db.execute(
                 'SELECT id,counterparty_normalized,direction,category_id FROM classification_rules')}
    suggestions = []
    for row in rows:
        normalized = normalize_counterparty(row['counterparty']) if row['counterparty'] else ''
        rule = rules.get((normalized, row['direction']))
        if rule is not None:
            suggestions.append((row, *rule))
    groups = {}
    for row, rule_id, category in suggestions:
        group = groups.setdefault((rule_id, category), {
            'count': 0, 'start': row['date'], 'end': row['date'],
            'absolute_amount': 0, 'members': [], 'counterparty': row['counterparty'],
        })
        group['count'] += 1
        group['start'] = min(group['start'], row['date'])
        group['end'] = max(group['end'], row['date'])
        group['absolute_amount'] += abs(money(row['amount']))
        group['members'].append({
            'account_id': row['account_id'], 'external_id': row['external_id'],
            'category': category, 'revision': row['revision'] or 0,
        })
    suggested = {(row['account_id'], row['external_id']): (rule_id, category)
                 for row, rule_id, category in suggestions}
    open_groups = {}
    for row in rows:
        key = (row['account_id'], row['external_id'])
        if (key in suggested or not row['counterparty']
                or not source_context_complete(row['counterparty'], row['description'])):
            continue
        normalized = normalize_counterparty(row['counterparty'])
        group = open_groups.setdefault((normalized, row['direction']), {
            'count': 0, 'start': row['date'], 'end': row['date'],
            'absolute_amount': 0, 'counterparty': row['counterparty'],
        })
        group['count'] += 1
        group['start'] = min(group['start'], row['date'])
        group['end'] = max(group['end'], row['date'])
        group['absolute_amount'] += abs(money(row['amount']))
    items = []
    for row in rows:
        match = suggested.get((row['account_id'], row['external_id']))
        if match is None:
            if row['model_category'] is not None:
                item = _item(
                    'classification', f"classification:{row['account_id']}:{row['external_id']}",
                    'Modellvorschlag prüfen',
                    f"Ollama schlägt {row['model_category']} für {row['counterparty']} am "
                    f"{row['date']} über {format(abs(money(row['amount'])), '.2f')} EUR vor. "
                    f"{row['model_rationale']}",
                    row['model_confidence'], 'classification', row['revision'] or 0,
                    _action('/api/classification-save', {
                        'account_id': row['account_id'], 'external_id': row['external_id'],
                        'category': row['model_category'], 'revision': row['revision'] or 0,
                    }))
                item.update({
                    'transaction_account': row['account_id'],
                    'transaction_external_id': row['external_id'],
                    'transaction_date': row['date'],
                    'transaction_amount': row['amount'],
                    'transaction_currency': row['currency'],
                    'transaction_counterparty': row['counterparty'],
                    'transaction_description': row['description'],
                    'transaction_provisional': (row['counterparty'] or '').strip().casefold().startswith(
                        'vorgemerkt'),
                    'model_name': row['model_name'],
                    'decision_note': _model_decision_note(
                        row['model_category'], row['model_confidence']),
                    'reject_action': _action('/api/classification-model-reject', {
                        'account_id': row['account_id'], 'external_id': row['external_id'],
                        'category_id': row['model_category'],
                        'revision': row['revision'] or 0,
                    }),
                })
                items.append(item)
                continue
            party = row['counterparty'] or 'Gegenpartei noch nicht erfasst'
            context_complete = source_context_complete(
                row['counterparty'], row['description'])
            item = _item(
                'classification', f"classification:{row['account_id']}:{row['external_id']}",
                'Quelldetails prüfen' if not context_complete else 'Kategorie prüfen',
                (f'{party} · {row["date"]} · {format(abs(money(row["amount"])), ".2f")} EUR. '
                 + ('Empfänger und Verwendungszweck fehlen; Betrag allein erlaubt keine Zuordnung.'
                    if not context_complete else 'Noch keine passende lokale Regel vorhanden.')),
                'unknown', 'classification', row['revision'] or 0)
            item.update({
                'transaction_account': row['account_id'],
                'transaction_external_id': row['external_id'],
                'transaction_date': row['date'],
                'transaction_amount': row['amount'],
                'transaction_currency': row['currency'],
                'transaction_counterparty': row['counterparty'],
                'transaction_description': row['description'],
            })
            if context_complete and row['counterparty']:
                normalized = normalize_counterparty(row['counterparty'])
                group = open_groups[(normalized, row['direction'])]
                item.update({
                    'group_key': f'classification-open:{row["direction"]}:{normalized}',
                    'group_count': group['count'],
                    'group_period_start': group['start'],
                    'group_period_end': group['end'],
                    'group_absolute_amount': format(group['absolute_amount'], '.2f'),
                    'group_counterparty': group['counterparty'],
                })
            items.append(item)
            continue
        rule_id, category = match
        group = groups[(rule_id, category)]
        item = _item(
            'classification', f"classification:{row['account_id']}:{row['external_id']}",
            'Kategorie bestätigen',
            f'Lokale Regel schlägt {category} für {row["counterparty"]} am {row["date"]} '
            f'über {format(abs(money(row["amount"])), ".2f")} EUR vor.',
            'high', 'classification', row['revision'] or 0,
            _action('/api/classification-save', {
                'account_id': row['account_id'], 'external_id': row['external_id'],
                'category': category, 'revision': row['revision'] or 0,
            }))
        item.update({
            'transaction_account': row['account_id'],
            'transaction_external_id': row['external_id'],
            'transaction_date': row['date'],
            'transaction_amount': row['amount'],
            'transaction_currency': row['currency'],
            'transaction_counterparty': row['counterparty'],
            'transaction_description': row['description'],
            'group_key': f'classification-rule:{rule_id}:{category}',
            'group_count': group['count'],
            'group_period_start': group['start'],
            'group_period_end': group['end'],
            'group_absolute_amount': format(group['absolute_amount'], '.2f'),
            'group_counterparty': group['counterparty'],
            'group_direct_action': _action('/api/classification-save-batch', {
                'confirmed': True, 'classifications': group['members'],
            }),
        })
        items.append(item)
    return items


def _transfer_items(store):
    items = []
    for pair in pending_suggestions(store):
        first, second = pair['first'], pair['second']
        amount = format(abs(money(first['amount'])), '.2f')
        pair_id = ':'.join((first['account_id'], first['external_id'],
                            second['account_id'], second['external_id']))
        detail = (f"{first['account_id']} · {first['date']} → {second['account_id']} · "
                  f"{second['date']} · {amount} EUR")
        parties = ' → '.join(value for value in (
            first['counterparty'], second['counterparty']) if value)
        if parties:
            detail += f' · {parties}'
        items.append(_item(
            'transfer_correction', f'transfer:{pair_id}', 'Umbuchung bestätigen',
            detail, pair['confidence'], 'classification', '0:0',
            _action('/api/transfer-correction-save', {
                'first': {key: first[key] for key in ('account_id', 'external_id', 'revision')},
                'second': {key: second[key] for key in ('account_id', 'external_id', 'revision')},
                'confirmed': True,
            })))
    return items


def document_link_items(store):
    items = []
    rows = store.db.execute(
        """WITH transaction_allocations AS (
               SELECT account_id,external_id,
                      SUM(CAST(REPLACE(allocated_amount,'.','') AS INTEGER)) AS allocated_cents
               FROM classification_document_links GROUP BY account_id,external_id
           ), document_allocations AS (
               SELECT document_id,
                      SUM(CAST(REPLACE(allocated_amount,'.','') AS INTEGER)) AS allocated_cents
               FROM classification_document_links WHERE allocation_type='payment' GROUP BY document_id
           )
           SELECT t.account_id,t.external_id,t.date AS transaction_date,t.amount AS transaction_amount,
                  COALESCE(c.counterparty,'') AS counterparty,COALESCE(c.description,'') AS description,
                  d.id AS document_id,d.id AS id,d.kind,d.vendor,d.title,d.document_date,d.amount AS document_amount,
                  d.currency,d.source_reference,d.warnings,d.status,d.revision
           FROM transactions t
           LEFT JOIN transaction_context c ON c.account_id=t.account_id AND c.external_id=t.external_id
           LEFT JOIN transfer_correction_members m ON m.account_id=t.account_id AND m.external_id=t.external_id
           LEFT JOIN transaction_allocations ta
             ON ta.account_id=t.account_id AND ta.external_id=t.external_id
           JOIN classification_documents d
             ON d.kind='invoice' AND d.amount IS NOT NULL AND d.currency='EUR'
            AND d.document_date IS NOT NULL AND d.document_date<=t.date
            AND d.document_date>=date(t.date,'-45 days')
           LEFT JOIN document_allocations da ON da.document_id=d.id
           WHERE t.date >= '2026-01-01' AND t.currency='EUR' AND substr(t.amount,1,1)='-'
             AND t.transfer_id='' AND m.pair_id IS NULL
             AND CAST(REPLACE(REPLACE(t.amount,'-',''),'.','') AS INTEGER)
                   - COALESCE(ta.allocated_cents,0)
                 = CAST(REPLACE(d.amount,'.','') AS INTEGER)
                   - COALESCE(da.allocated_cents,0)
             AND NOT EXISTS (
                SELECT 1 FROM classification_document_links l
                JOIN classification_documents d ON d.id=l.document_id
                WHERE l.account_id=t.account_id AND l.external_id=t.external_id AND d.kind='invoice')
             AND NOT EXISTS (
                SELECT 1 FROM classification_audit a
                WHERE a.action='document_link_rejected' AND a.account_id=t.account_id
                  AND a.external_id=t.external_id AND a.document_id=d.id)
           ORDER BY t.date,t.account_id,t.external_id,d.document_date,d.id"""
    ).fetchall()
    for row in rows:
        if not document_recipient_matches(
                row['vendor'], row['counterparty'], row['description'], row['title']):
            continue
        if not _document_match_eligible(store, row):
            continue
        if row['status'] == 'confirmed':
                action = _action('/api/classification-link', {
                    'account_id': row['account_id'], 'external_id': row['external_id'],
                    'document_id': row['document_id'], 'confirmed': True,
                })
        else:
            action = _action('/api/classification-confirm-link', {
                'id': row['document_id'], 'revision': row['revision'], 'vendor': row['vendor'],
                'title': row['title'], 'document_date': row['document_date'],
                'amount': row['document_amount'], 'currency': row['currency'],
                'source_reference': row['source_reference'], 'status': 'confirmed',
                'account_id': row['account_id'], 'external_id': row['external_id'],
                'confirmed': True,
            })
        day_gap = (date.fromisoformat(row['transaction_date'])
                   - date.fromisoformat(row['document_date'])).days
        confidence = 'high' if day_gap <= 7 else 'medium'
        items.append(_item(
            'document', f"document-link:{row['account_id']}:{row['external_id']}:{row['document_id']}",
            'Rechnung zuordnen',
            f"{row['transaction_date']} · {row['account_id']} · "
            f"{format(abs(money(row['transaction_amount'])), '.2f')} EUR · "
            f"{row['vendor']} · {row['title']} · Rechnung {row['document_date']}.",
            confidence, 'documents', row['revision'], action) | {
                'document_id': row['document_id'], 'transaction_account': row['account_id'],
                'transaction_external_id': row['external_id'],
                'transaction_date': row['transaction_date'],
                'transaction_amount': row['transaction_amount'], 'document_status': row['status'],
            })
    return items


def _budget_items(store):
    from . import budget
    snapshot = budget.load(store)
    if snapshot['plan'] is None:
        return []
    revision = snapshot['revision']
    latest_revision = snapshot['latest_revision']
    items = []
    for item in snapshot['plan']['items']:
        # Estimates remain part of the forecast but are not actionable yes/no decisions.
        if item['confirmed'] or item['label'].casefold().startswith('schätzung ·'):
            continue
        confirmed_plan = snapshot['plan'] | {
            'items': [candidate | {'confirmed': True} if candidate['id'] == item['id']
                      else candidate.copy() for candidate in snapshot['plan']['items']],
        }
        items.append(_item(
            'budget_item', f"budget:{revision}:{item['id']}", 'Budgetposition bestätigen',
            f"{item['label']} · {item['amount']} EUR · Quelle: {item['source']} ist noch nicht bestätigt.",
            'unknown', 'planning', revision,
            _action('/api/budget-save', {
                'revision': latest_revision, 'base_revision': revision,
                'active_revision': revision, 'plan': confirmed_plan, 'activate': True,
            })))
    return items


def _intake_items(store, root):
    from . import intake_draft
    draft = intake_draft.load(store, root)['draft']
    if draft is None or draft['activated']:
        return []
    return [_item(
        'intake_mapping', f"intake:{draft['batch']}:{account['key']}", 'Konto-Mapping prüfen',
        f"{account['source_name'] or account['source_account']}: Eröffnungswert ist noch nicht bestätigt.",
        'unknown', 'intake', draft['revision'])
        for account in draft['accounts']
        if not account['opening_confirmed'] or account['opening'] is None]


def list_items(store, root, data=None):
    """List pending items without changing records, audit logs, or the ledger."""
    data = {} if data is None else data
    if not isinstance(data, dict) or set(data) - {'as_of'}:
        raise ValueError('invalid_approvals_request')
    as_of = data.get('as_of')
    if as_of is None:
        as_of = (datetime.now().astimezone().date().replace(day=1) - timedelta(days=1)).isoformat()
    else:
        as_of = date.fromisoformat(as_of).isoformat()
    items = (_transfer_items(store) + _category_items(store) + document_link_items(store)
             + _budget_items(store) + _intake_items(store, root))
    return {'items': sorted(items, key=lambda item: (item['type'], item['id']))}
