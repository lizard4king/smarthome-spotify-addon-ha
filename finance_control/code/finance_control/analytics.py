"""Household category analysis with monthly and parent-to-booking drill-downs."""
from datetime import UTC, date, datetime
from decimal import Decimal

from .classification import _direction, _filter_date, money
from .person_attribution import account_allocations, person_label, split_cents


def _month_range(date_from, date_to, rows):
    dates = [row['date'] for row in rows]
    today = datetime.now(UTC).date().isoformat()
    first = date.fromisoformat(date_from or (min(dates) if dates else today))
    last = date.fromisoformat(date_to or (max(dates) if dates else first.isoformat()))
    current = first.replace(day=1)
    last = last.replace(day=1)
    result = []
    while current <= last:
        result.append(current.strftime('%Y-%m'))
        current = date(current.year + (current.month == 12), current.month % 12 + 1, 1)
    return result


def _month_amounts():
    return {'amount': Decimal(0), 'count': 0}


def _person_expenses(people, expenses, allocations):
    buckets = {person: {'id': person, 'label': label, 'amount': Decimal(0),
                        'count': 0, 'categories': {}} for person, label in people.items()}
    buckets['JOINT'] = {'id': 'JOINT', 'label': 'Gemeinsam', 'amount': Decimal(0),
                       'count': 0, 'categories': {}}
    buckets[None] = {'id': None, 'label': 'Ungeklärt', 'amount': Decimal(0),
                     'count': 0, 'categories': {}}
    for row, parent, child, value in expenses:
        for person, part in split_cents(value, allocations.get(row['account_id'], {None: Decimal(1)})).items():
            bucket = buckets[person]
            bucket['amount'] += part
            bucket['count'] += 1
            category = bucket['categories'].setdefault(child['id'], {
                'id': child['id'], 'label': child['label'], 'parent_label': parent['label'],
                'amount': Decimal(0), 'count': 0, 'purposes': {}})
            category['amount'] += part
            category['count'] += 1
            purpose = row['description'] or row['counterparty'] or 'Kein Verwendungszweck'
            category['purposes'][purpose] = category['purposes'].get(purpose, Decimal(0)) + part
    result = []
    for bucket in buckets.values():
        categories = sorted(bucket.pop('categories').values(), key=lambda item: (-item['amount'], item['label']))
        for category in categories:
            category['amount'] = format(category['amount'], '.2f')
            category['purposes'] = [{'label': label, 'amount': format(value, '.2f')}
                                   for label, value in sorted(category['purposes'].items(),
                                                              key=lambda item: (-item[1], item[0]))]
        bucket['categories'] = categories
        bucket['amount'] = format(bucket['amount'], '.2f')
        result.append(bucket)
    return {'basis': 'Zuordnung nach Kontoinhaberschaft', 'people': result}


def category_summary(store, data=None, *, people=None):
    data = {} if data is None else data
    if not isinstance(data, dict) or set(data) - {
            'date_from', 'date_to', 'account_id', 'person_breakdown'}:
        raise ValueError('invalid_analytics_request')
    if 'person_breakdown' in data and type(data['person_breakdown']) is not bool:
        raise ValueError('invalid_analytics_person_breakdown')
    include_people = data.get('person_breakdown', False)
    date_from = None if data.get('date_from') is None else _filter_date(data['date_from'], 'date_from')
    date_to = None if data.get('date_to') is None else _filter_date(data['date_to'], 'date_to')
    if date_from and date_to and date_from > date_to:
        raise ValueError('invalid_date_range')
    clauses, params = [], []
    if date_from:
        clauses.append('t.date>=?'); params.append(date_from)
    if date_to:
        clauses.append('t.date<=?'); params.append(date_to)
    if data.get('account_id') is not None:
        account_id = data['account_id']
        if not isinstance(account_id, str) or not account_id.strip() or len(account_id) > 120:
            raise ValueError('invalid_account_id')
        account_id = account_id.strip()
        if store.db.execute('SELECT 1 FROM accounts WHERE id=?', (account_id,)).fetchone() is None:
            raise ValueError('unknown_account')
        clauses.append('t.account_id=?'); params.append(account_id)
    where = '' if not clauses else ' WHERE ' + ' AND '.join(clauses)
    rows = store.db.execute(
        'SELECT t.account_id,t.external_id,t.date,t.amount,t.category,t.transfer_id,c.description,c.counterparty,'
        'o.category_id,o.confirmed FROM transactions t '
        'LEFT JOIN transaction_context c USING(account_id,external_id) '
        'LEFT JOIN classification_overrides o USING(account_id,external_id)' + where,
        params).fetchall()
    catalog = {row['id']: dict(row) for row in store.db.execute(
        'SELECT id,label,transaction_type,parent_id FROM category_catalog')}
    link_counts = {(row['account_id'], row['external_id']): row['count'] for row in store.db.execute(
        'SELECT account_id,external_id,COUNT(*) AS count FROM classification_document_links '
        'GROUP BY account_id,external_id')}
    parents = {}
    labels = ({person['id']: person['label'] for person in people} if people is not None else
              {row['id']: person_label(row['id']) for row in store.db.execute(
                  'SELECT id FROM persons ORDER BY id')}
              ) if include_people else {}
    allocations = account_allocations(store, labels) if include_people else {}
    expenses = []
    totals = {'income': Decimal(0), 'outflow': Decimal(0), 'confirmed_count': 0,
              'unreviewed_count': 0, 'transfer_count': 0}
    month_keys = _month_range(date_from, date_to, rows)
    monthly = {month: {'month': month, 'income': Decimal(0), 'outflow': Decimal(0),
                       'confirmed_count': 0, 'unreviewed_count': 0, 'transfer_count': 0}
               for month in month_keys}
    for row in rows:
        direction = _direction(store, row)
        amount = money(row['amount'])
        month = row['date'][:7]
        month_bucket = monthly[month]
        if direction == 'transfer':
            totals['transfer_count'] += 1
            month_bucket['transfer_count'] += 1
            continue
        if row['confirmed'] != 1 or row['category_id'] not in catalog:
            totals['unreviewed_count'] += 1
            month_bucket['unreviewed_count'] += 1
            continue
        child = catalog[row['category_id']]
        parent = catalog.get(child['parent_id'])
        if parent is None:
            totals['unreviewed_count'] += 1
            month_bucket['unreviewed_count'] += 1
            continue
        # Person expenses use confirmed expense categories; positive refunds reduce them.
        if include_people and child['transaction_type'] == 'expense':
            expenses.append((row, parent, child, -amount))
        value = amount if direction == 'income' else -amount
        totals[direction if direction == 'income' else 'outflow'] += value
        totals['confirmed_count'] += 1
        month_bucket[direction if direction == 'income' else 'outflow'] += value
        month_bucket['confirmed_count'] += 1
        parent_bucket = parents.setdefault(parent['id'], {
            'id': parent['id'], 'label': parent['label'],
            'transaction_type': child['transaction_type'], 'amount': Decimal(0),
            'count': 0, 'document_count': 0, 'monthly': {}, 'children': {}})
        child_bucket = parent_bucket['children'].setdefault(child['id'], {
            'id': child['id'], 'label': child['label'], 'amount': Decimal(0),
            'count': 0, 'document_count': 0, 'monthly': {}})
        document_count = link_counts.get((row['account_id'], row['external_id']), 0)
        for bucket in (parent_bucket, child_bucket):
            bucket['amount'] += value
            bucket['count'] += 1
            bucket['document_count'] += document_count
            category_month = bucket['monthly'].setdefault(month, _month_amounts())
            category_month['amount'] += value
            category_month['count'] += 1
    result = []
    for parent in parents.values():
        children = sorted(parent.pop('children').values(), key=lambda item: (-item['amount'], item['label']))
        parent['amount'] = format(parent['amount'], '.2f')
        parent['monthly'] = _format_category_months(parent['monthly'])
        parent['children'] = [{**child, 'amount': format(child['amount'], '.2f'),
                               'monthly': _format_category_months(child['monthly'])}
                              for child in children]
        result.append(parent)
    result.sort(key=lambda item: (item['transaction_type'] != 'expense', -Decimal(item['amount']), item['label']))
    formatted_totals = {key: (format(value, '.2f') if isinstance(value, Decimal) else value)
                        for key, value in totals.items()}
    formatted_monthly = []
    for bucket in monthly.values():
        income, outflow = bucket['income'], bucket['outflow']
        formatted_monthly.append({**bucket, 'income': format(income, '.2f'),
                                  'outflow': format(outflow, '.2f'),
                                  'net': format(income - outflow, '.2f')})
    formatted_totals['net'] = format(totals['income'] - totals['outflow'], '.2f')
    response = {'categories': result, 'totals': formatted_totals, 'monthly': formatted_monthly,
                'period': {'date_from': date_from, 'date_to': date_to}}
    if include_people:
        response['person_expenses'] = _person_expenses(labels, expenses, allocations)
    return response


def _format_category_months(monthly):
    return [{'month': month, 'amount': format(values['amount'], '.2f'), 'count': values['count']}
            for month, values in sorted(monthly.items())]
