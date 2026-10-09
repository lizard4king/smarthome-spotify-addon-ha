"""Additive, local transaction classifications and document links.

The ledger remains the source of amounts, booking dates and source categories.
"""
import hashlib
import json
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from itertools import combinations

from .budget import canonical_category_id
from .core import money, parse_money_input
from .payment_status import statuses_for_transactions
from .transfer_corrections import (
    effective_transfer_id,
    sql_transfer_predicate,
    transfer_display,
)

_PAGE = 50
_DOCUMENT_MATCH_PAGE = 25
_MATCH_GROUP_CANDIDATES = 18
_MATCH_GROUP_LIMIT = 25
DOCUMENT_REVIEW_START_DATE = '2026-01-01'
_KINDS = {'invoice', 'contract'}
_TRANSACTION_FILTERS = {'query', 'category', 'reviewed', 'account_id', 'date_from', 'date_to'}
_DATE_RE = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}\Z')
_MATCH_IGNORED = {
    'andreas', 'pistelok', 'rechnung', 'invoice', 'bestellung', 'zahlung',
    'payment', 'service', 'services', 'gmbh', 'deutschland', 'deutsche', 'online',
    'pos', 'atm', 'eur', 'usd', 'gbp', 'chf', 'bic', 'ltd', 'inc', 'llc', 'plc',
    'vat', 'ust', 'ref',
}
_REFUND_MARKERS = ('refund', 'gutschrift', 'erstattung', 'rueckzahlung', 'rückzahlung')
_AMBIGUOUS_RECIPIENTS = {'amazon', 'paypal', 'google', 'apple', 'klarna'}
_GENERIC_COUNTERPARTIES = {
    'abbuchung', 'belastung', 'buchung', 'kartenzahlung', 'kreditkartenumsatz',
    'lastschrift', 'sepa-lastschrift', 'sonstige',
}
_AUTO_REVIEW_GENERIC = {
    'unbekannt', 'unbekannter anbieter', 'unbenanntes dokument', 'unknown',
    'unknown vendor', 'n/a', 'na', 'test', 'anbieter', 'vendor', 'adresse', 'menge',
}
_AUTO_REVIEW_SALUTATION = re.compile(
    r'(^|\s)(herr|frau|mr|mrs|ms|dear|hallo|liebe?r)(\s|$)', re.IGNORECASE)
_AUTO_REVIEW_URL = re.compile(
    r'(?:https?://|www\.|\.(?:com|de|net|org|io)(?:/|$))', re.IGNORECASE)
_AUTO_REVIEW_IMAGE = re.compile(
    r'\.(?:png|jpe?g|gif|webp|svg|bmp)(?:\s|$)', re.IGNORECASE)
_AUTO_REVIEW_PLACEHOLDER = re.compile(
    r'(?:guten tag|vielen dank|nehmen sie|code\s*/\s*produkt|ihre zahlung|bestellung anzeigen)',
    re.IGNORECASE)
_AUTO_REVIEW_BANK_VENDOR = re.compile(
    r'\b(?:bank|sparkasse|raiffeisenbank|postbank)\b', re.IGNORECASE)
_CASH_WITHDRAWAL_MARKER = re.compile(
    r'\b(?:bargeld(?:abhebung|auszahlung)?|barabhebung|cash\s+withdrawal|geldautomat|atm)\b',
    re.IGNORECASE)


def _text(value, name, maximum=240, empty=False):
    if not isinstance(value, str):
        raise TypeError(f'invalid_{name}')
    value = value.strip()
    if (not empty and not value) or len(value) > maximum:
        raise ValueError(f'invalid_{name}')
    return value


def _optional_date(value, name):
    if value is None:
        return None
    try:
        return date.fromisoformat(_text(value, name, 10)).isoformat()
    except ValueError as error:
        raise ValueError(f'invalid_{name}') from error


def _filter_date(value, name):
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise ValueError(f'invalid_{name}')
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f'invalid_{name}') from error
    if parsed.isoformat() != value:
        raise ValueError(f'invalid_{name}')
    return value


def _optional_amount(value):
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError('invalid_amount')
    return format(money(value.strip()), '.2f')


def _allocated_amount(value):
    amount = (format(parse_money_input(value), '.2f')
              if isinstance(value, str) else None)
    if amount is None or money(amount) <= 0:
        raise ValueError('invalid_allocated_amount')
    return amount


def _evidence_type(kind, title, warnings):
    return ('order_confirmation'
            if ('amazon_order_confirmation_not_tax_invoice' in warnings
                or 'amazon_data_portability_not_tax_invoice' in warnings
                or title.startswith('Amazon-Bestellbestätigung '))
            else kind)


def _links_for_transaction(store, account_id, external_id):
    result = [dict(link) for link in store.db.execute(
        'SELECT d.id,d.kind,d.vendor,d.title,d.status,d.warnings,l.allocated_amount,l.allocation_type '
        'FROM classification_document_links l JOIN classification_documents d ON d.id=l.document_id '
        'WHERE l.account_id=? AND l.external_id=? ORDER BY d.id', (account_id, external_id))]
    for link in result:
        link['evidence_type'] = _evidence_type(
            link['kind'], link['title'], link.pop('warnings'))
    return result


def _warnings(value):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 50:
        raise ValueError('invalid_warnings')
    return [_text(item, 'warning', 240) for item in value]


def normalize_counterparty(value):
    """Return the canonical counterparty key shared by rules and approval views."""
    return ' '.join(_text(value, 'counterparty').casefold().split())


def source_context_complete(counterparty, description):
    """Return whether source labels identify more than a generic booking type."""
    party = ' '.join((counterparty or '').casefold().split())
    purpose = ' '.join((description or '').casefold().split())
    return bool((purpose and purpose not in _GENERIC_COUNTERPARTIES)
                or (party and party not in _GENERIC_COUNTERPARTIES))


def _merchant_family_match(store, counterparty, description, direction):
    """Apply explicit household policy to recognizable marketplace bookings."""
    text = ' '.join((counterparty or '', description or '')).casefold()
    if 'amazon' not in text and 'amzn' not in text:
        return None
    if direction == 'income':
        category_id = 'EINKOMMEN_ERSTATTUNG'
    elif direction != 'expense':
        return None
    elif ('amazon instant video' in text or 'prime video' in text
          or 'amazon digital' in text or 'amzn digital' in text):
        category_id = 'FREIZEIT_STREAMING_DIGITAL'
    elif 'amazon prim' in text or 'amznprime' in text:
        category_id = 'AUSGABEN_ABONNEMENTS'
    else:
        category_id = 'ALLTAG_ONLINE_EINKAUF'
    row = store.db.execute(
        'SELECT id,label,parent_id FROM category_catalog WHERE id=? '
        'AND transaction_type=?', (category_id, direction)).fetchone()
    if row is None:
        return None
    return {'rule_id': None, 'category': row['id'], 'parent_category': row['parent_id'],
            'label': row['label'], 'provenance': 'local_merchant_family_rule'}


def _match_tokens(value):
    return {
        token for token in re.findall(r'[a-z0-9äöüß]+', (value or '').casefold())
        if len(token) >= 4 and token not in _MATCH_IGNORED and not token.isdigit()
    }


def document_recipient_matches(vendor, counterparty, description='', title=''):
    """Require recipient evidence; broad marketplaces also need item evidence."""
    if not isinstance(vendor, str):
        return False
    vendor = vendor.strip()
    if not vendor or len(vendor) > 240:
        return False
    if counterparty is not None:
        if not isinstance(counterparty, str) or len(counterparty.strip()) > 240:
            return False
        counterparty = counterparty.strip()
    if description is None:
        description = ''
    if title is None:
        title = ''
    if (not isinstance(description, str) or len(description) > 1000
            or not isinstance(title, str) or len(title) > 240):
        return False
    vendor_key = normalize_counterparty(vendor)
    counterparty_key = normalize_counterparty(counterparty) if counterparty else ''
    distinctive_vendor_tokens = _match_tokens(vendor) - _GENERIC_COUNTERPARTIES
    if vendor_key in _GENERIC_COUNTERPARTIES:
        return False
    if distinctive_vendor_tokens & _AMBIGUOUS_RECIPIENTS:
        booking_tokens = _match_tokens(' '.join((counterparty or '', description or '')))
        return bool((distinctive_vendor_tokens & booking_tokens)
                    and (_match_tokens(title) & booking_tokens))
    # Short, distinctive merchant names such as OBI are deliberately absent
    # from the generic token set. Require a whole word in either booking field;
    # payment processors often put the merchant only in the description.
    short_distinctive_match = (
        ' ' not in vendor_key and '-' not in vendor_key and len(vendor_key) >= 3
        and re.search(rf'(?<!\w){re.escape(vendor_key)}(?!\w)',
                      f'{counterparty_key} {description.casefold()}')
    )
    if ((vendor_key == counterparty_key or short_distinctive_match)
            and len(vendor_key) >= 3
            and any(character.isalpha() for character in vendor_key)
            and (distinctive_vendor_tokens
                 or (' ' not in vendor_key and '-' not in vendor_key))
            and vendor_key not in _AMBIGUOUS_RECIPIENTS
            and vendor_key not in _MATCH_IGNORED):
        return True
    if len(vendor_key) < 3:
        return False
    booking_tokens = _match_tokens(' '.join((counterparty or '', description or '')))
    vendor_matches = distinctive_vendor_tokens & booking_tokens
    return bool(vendor_matches)


def _supports_refund_suggestion(store, document):
    text = f"{document['title']} {document['vendor']}".casefold()
    return (any(marker in text for marker in _REFUND_MARKERS)
            or store.db.execute(
                'SELECT 1 FROM amazon_portability_documents WHERE document_id=? '
                'AND related_external_id IS NOT NULL LIMIT 1',
                (document['id'],)).fetchone() is not None)


def _api_refund_total(store, document_id):
    amounts = [Decimal(row['amount']) for row in store.db.execute(
        'SELECT amount FROM amazon_portability_documents WHERE document_id=? '
        'AND related_external_id IS NOT NULL', (document_id,))]
    return sum((-amount for amount in amounts if amount < 0), Decimal(0))


def _document_match_eligible(store, document):
    """Allow reviewed documents and narrow, auditable unreviewed candidates."""
    try:
        warnings = json.loads(document['warnings'])
    except (TypeError, ValueError):
        return False
    if not isinstance(warnings, list) or set(warnings) & {
            'source_excluded_bonsy', 'not_invoice_like', 'duplicate_source_document'}:
        return False
    if document['status'] == 'confirmed':
        return True
    if document['status'] != 'unreviewed':
        return False
    if set(warnings) <= {'ocr_output_unreviewed'}:
        return True
    return (warnings in (['amazon_order_confirmation_not_tax_invoice'],
                         ['amazon_data_portability_not_tax_invoice'])
            and store.db.execute(
                'SELECT 1 FROM amazon_order_documents WHERE document_id=?',
                (document['id'],)).fetchone() is not None)


def _exact_sum_groups(candidates, target, amount_key):
    """Return bounded, deterministic 2..4 member groups with an exact EUR sum."""
    usable = [candidate for candidate in candidates
              if Decimal(0) < candidate[amount_key] < target][:_MATCH_GROUP_CANDIDATES]
    groups = []
    for size in range(2, min(4, len(usable)) + 1):
        for members in combinations(usable, size):
            if sum((member[amount_key] for member in members), Decimal(0)) == target:
                groups.append(members)
                if len(groups) >= _MATCH_GROUP_LIMIT:
                    return groups
    return groups


def _source_reference(value):
    value = _text(value, 'source_reference', 500)
    if '/' in value or '\\' in value or not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}:[A-Za-z0-9._:-]+', value):
        raise ValueError('invalid_source_reference')
    return value


def _now():
    return datetime.now(UTC).isoformat()


def _audit(store, action, current, account_id=None, external_id=None, document_id=None, previous=None):
    store.db.execute('INSERT INTO classification_audit(occurred_at,action,account_id,external_id,document_id,previous,current) VALUES (?,?,?,?,?,?,?)',
                     (_now(), action, account_id, external_id, document_id,
                      None if previous is None else json.dumps(previous, sort_keys=True),
                      json.dumps(current, sort_keys=True)))


def _document_link_rejected(store, account_id, external_id, document_id):
    return store.db.execute(
        "SELECT 1 FROM classification_audit WHERE action='document_link_rejected' "
        'AND account_id=? AND external_id=? AND document_id=? LIMIT 1',
        (account_id, external_id, document_id)).fetchone() is not None


def _transaction(store, account_id, external_id):
    row = store.db.execute(
        'SELECT t.*,c.counterparty,c.description FROM transactions t '
        'LEFT JOIN transaction_context c USING(account_id,external_id) '
        'WHERE t.account_id=? AND t.external_id=?', (account_id, external_id)).fetchone()
    if row is None:
        raise ValueError('unknown_transaction')
    return row


def _direction(store, row):
    if effective_transfer_id(store, row):
        return 'transfer'
    return 'income' if money(row['amount']) > 0 else 'expense'


def _cash_withdrawal_evidence(store, account_id, external_id):
    """Return explicit cash-withdrawal evidence without classifying neutral payments."""
    row = store.db.execute(
        """SELECT t.category AS source_category,c.counterparty,c.description,o.category_id
           FROM transactions t
           LEFT JOIN transaction_context c USING(account_id,external_id)
           LEFT JOIN classification_overrides o USING(account_id,external_id)
           WHERE t.account_id=? AND t.external_id=?""",
        (account_id, external_id)).fetchone()
    if row is None:
        return False
    if canonical_category_id(store, row['category_id']) == 'AUSGABEN_BARGELD':
        return True
    for audit in store.db.execute(
            """SELECT action,previous,current FROM classification_audit
               WHERE account_id=? AND external_id=?
                 AND action IN ('classification_saved','classification_auto_applied',
                                'classification_reset')
               ORDER BY id DESC""", (account_id, external_id)):
        payloads = (audit['current'],) if audit['action'] != 'classification_reset' else (audit['previous'],)
        for payload in payloads:
            try:
                classification = json.loads(payload)
            except (TypeError, ValueError):
                continue
            if (classification.get('confirmed') is True
                    and canonical_category_id(
                        store, classification.get('category', classification.get('category_id')))
                    == 'AUSGABEN_BARGELD'):
                return True
    return any(_CASH_WITHDRAWAL_MARKER.search(value or '') is not None
               for value in (row['source_category'], row['counterparty'], row['description']))


def _catalog(store):
    return [dict(row) for row in store.db.execute(
        'SELECT id,label,transaction_type,parent_id FROM category_catalog '
        'WHERE parent_id IS NOT NULL ORDER BY transaction_type,parent_id,id')]


def category_catalog(store, data=None):
    """Return the stable, locally owned two-level category catalogue.

    The imported transaction category is deliberately absent from this
    contract: it is provenance only and cannot influence suggestions.
    """
    if data not in (None, {}):
        raise ValueError('invalid_category_catalog_request')
    parents = [dict(row) for row in store.db.execute(
        'SELECT id,label,transaction_type,parent_id FROM category_catalog '
        'WHERE parent_id IS NULL ORDER BY transaction_type,id')]
    return {'parents': parents, 'categories': _catalog(store)}


def create_category(store, data):
    """Create or reuse one local two-level category for income or expense."""
    if (not isinstance(data, dict)
            or set(data) != {'parent_label', 'label', 'transaction_type'}):
        raise ValueError('invalid_category')
    transaction_type = data['transaction_type']
    if transaction_type not in {'income', 'expense'}:
        raise ValueError('invalid_category_type')
    parent_label = _text(data['parent_label'], 'parent_label', 80)
    category_label = _text(data['label'], 'label', 80)
    if parent_label.casefold() == category_label.casefold():
        raise ValueError('category_must_differ_from_parent')

    def custom_id(kind, *parts):
        value = '\x1f'.join((kind, transaction_type, *parts)).encode('utf-8')
        return 'CUSTOM_' + hashlib.sha256(value).hexdigest()[:20].upper()

    store.db.execute('BEGIN IMMEDIATE')
    try:
        parent = next((row for row in store.db.execute(
            "SELECT id,label,transaction_type,parent_id FROM category_catalog "
            "WHERE parent_id IS NULL AND transaction_type=?", (transaction_type,))
                       if row['label'].casefold() == parent_label.casefold()), None)
        parent_created = parent is None
        if parent is None:
            parent_id = custom_id('parent', parent_label.casefold())
            store.db.execute(
                'INSERT INTO category_catalog(id,label,transaction_type,parent_id) VALUES (?,?,?,NULL)',
                (parent_id, parent_label, transaction_type))
            parent = store.db.execute(
                'SELECT id,label,transaction_type,parent_id FROM category_catalog WHERE id=?',
                (parent_id,)).fetchone()
        category = next((row for row in store.db.execute(
            "SELECT id,label,transaction_type,parent_id FROM category_catalog "
            "WHERE parent_id=? AND transaction_type=?", (parent['id'], transaction_type))
                         if row['label'].casefold() == category_label.casefold()), None)
        category_created = category is None
        if category is None:
            category_id = custom_id('category', parent['id'], category_label.casefold())
            store.db.execute(
                'INSERT INTO category_catalog(id,label,transaction_type,parent_id) VALUES (?,?,?,?)',
                (category_id, category_label, transaction_type, parent['id']))
            category = store.db.execute(
                'SELECT id,label,transaction_type,parent_id FROM category_catalog WHERE id=?',
                (category_id,)).fetchone()
        result = {'parent': dict(parent), 'category': dict(category),
                  'parent_created': parent_created, 'category_created': category_created}
        _audit(store, 'category_created_or_reused', result)
        store.db.commit()
    except Exception:
        store.db.rollback()
        raise
    return result | category_catalog(store)


def _category(store, category_id, expected_type):
    category_id = _text(category_id, 'category', 80)
    category_id = canonical_category_id(store, category_id)
    row = store.db.execute('SELECT * FROM category_catalog WHERE id=?', (category_id,)).fetchone()
    if (row is None or row['transaction_type'] != expected_type
            or row['parent_id'] is None):
        raise ValueError('incompatible_category')
    return row


def set_transaction_context(store, data):
    """Register caller-provided local labels; no source transaction column changes."""
    if not isinstance(data, dict) or set(data) != {'account_id', 'external_id', 'counterparty', 'description'}:
        raise ValueError('invalid_context')
    account_id, external_id = _text(data['account_id'], 'account_id', 120), _text(data['external_id'], 'external_id', 240)
    counterparty, description = _text(data['counterparty'], 'counterparty'), _text(data['description'], 'description', 1000, empty=True)
    store.db.execute('BEGIN IMMEDIATE')
    try:
        _transaction(store, account_id, external_id)
        previous = store.db.execute('SELECT counterparty,description FROM transaction_context WHERE account_id=? AND external_id=?',
                                    (account_id, external_id)).fetchone()
        store.db.execute('INSERT INTO transaction_context VALUES (?,?,?,?) ON CONFLICT(account_id,external_id) DO UPDATE SET counterparty=excluded.counterparty,description=excluded.description',
                         (account_id, external_id, counterparty, description))
        _audit(store, 'context_saved', {'counterparty': counterparty, 'description': description}, account_id, external_id,
               previous=None if previous is None else dict(previous))
        store.db.commit()
    except Exception:
        store.db.rollback()
        raise
    return {'context': {'account_id': account_id, 'external_id': external_id, 'counterparty': counterparty, 'description': description}}


def _transaction_scope(data, allowed, error_name):
    data = {} if data is None else data
    if not isinstance(data, dict) or set(data) - allowed:
        raise ValueError(error_name)
    reviewed = data.get('reviewed')
    if reviewed is not None and type(reviewed) is not bool:
        raise ValueError('invalid_reviewed')
    clauses, params = [], []
    if data.get('account_id') is not None:
        clauses.append('t.account_id=?'); params.append(_text(data['account_id'], 'account_id', 120))
    transfer_sql = sql_transfer_predicate(context_alias='c')
    if data.get('category') is not None:
        clauses.append(f"(CASE WHEN {transfer_sql} THEN 'TRANSFER' "
                       "WHEN o.confirmed=1 THEN o.category_id "
                       "WHEN substr(t.amount,1,1)='-' OR t.amount='0.00' "
                       "THEN 'AUSGABEN_UNKLAR' "
                       "ELSE 'EINNAHMEN_UNKLAR' END)=?")
        params.append(_text(data['category'], 'category', 80))
    if reviewed is True:
        clauses.append(f"({transfer_sql} OR o.confirmed=1)")
    elif reviewed is False:
        clauses.append(f"(NOT {transfer_sql} AND (o.account_id IS NULL OR o.confirmed=0))")
    if data.get('query') is not None:
        query = '%' + _text(data['query'], 'query', 240, empty=True).casefold() + '%'
        clauses.append('(lower(coalesce(c.counterparty,\'\')) LIKE ? OR lower(coalesce(c.description,\'\')) LIKE ?)')
        params.extend([query, query])
    date_from = None if data.get('date_from') is None else _filter_date(data['date_from'], 'date_from')
    date_to = None if data.get('date_to') is None else _filter_date(data['date_to'], 'date_to')
    if date_from is not None and date_to is not None and date_from > date_to:
        raise ValueError('invalid_date_range')
    if date_from is not None:
        clauses.append('t.date>=?'); params.append(date_from)
    if date_to is not None:
        clauses.append('t.date<=?'); params.append(date_to)
    where = '' if not clauses else ' WHERE ' + ' AND '.join(clauses)
    base = (' FROM transactions t JOIN accounts a ON a.id=t.account_id '
            'LEFT JOIN transaction_context c ON (c.account_id=t.account_id AND c.external_id=t.external_id) '
            'LEFT JOIN classification_overrides o ON (o.account_id=t.account_id AND o.external_id=t.external_id) '
            'LEFT JOIN transfer_correction_members m ON (m.account_id=t.account_id AND m.external_id=t.external_id)')
    return base, where, params, {'date_from': date_from, 'date_to': date_to}


def list_transactions(store, data):
    data = {} if data is None else data
    allowed = _TRANSACTION_FILTERS | {'page', 'order', 'order_column', 'order_direction', 'column_filters'}
    if (not isinstance(data, dict) or set(data) - allowed
            or type(data.get('page', 0)) is not int or data.get('page', 0) < 0):
        raise ValueError('invalid_list_request')
    order = data.get('order', 'date')
    if order not in {'date', 'amount_desc'}:
        raise ValueError('invalid_order')
    order_column = data.get('order_column')
    order_direction = data.get('order_direction', 'asc')
    columns = {'date', 'account', 'detail', 'amount', 'detected', 'category', 'status'}
    if order_column is not None and (not isinstance(order_column, str) or order_column not in columns):
        raise ValueError('invalid_order_column')
    if order_direction not in {'asc', 'desc'}:
        raise ValueError('invalid_order_direction')
    column_filters = data.get('column_filters', {})
    if not isinstance(column_filters, dict) or set(column_filters) - columns:
        raise ValueError('invalid_column_filters')
    column_filters = {column: _text(value, 'column_filter', 240, empty=True).strip()
                      for column, value in column_filters.items()}
    column_filters = {column: value for column, value in column_filters.items() if value}
    page = data.get('page', 0)
    base, where, params, _ = _transaction_scope(data, allowed, 'invalid_list_request')
    total = store.db.execute('SELECT COUNT(*)' + base + where, params).fetchone()[0]
    rows = []
    selected = list(store.db.execute(
        "SELECT t.account_id,CASE WHEN NULLIF(TRIM(a.display_name),'') IS NULL THEN t.account_id "
        "ELSE t.account_id || ' · ' || a.display_name END AS account_label,t.external_id,t.date,t.amount,"
        't.category AS source_category,t.transfer_id,c.counterparty,c.description,'
        f"o.category_id,o.confirmed,o.revision,CASE WHEN {sql_transfer_predicate(context_alias='c')} "
        'THEN 1 ELSE 0 END AS is_transfer_row,'
        f"CASE WHEN {sql_transfer_predicate(context_alias='c')} "
        'OR o.confirmed=1 THEN 1 ELSE 0 END AS reviewed' + base + where, params))
    selected.sort(key=lambda row: (row['account_id'], row['external_id']))
    selected.sort(key=lambda row: row['date'], reverse=True)
    if order == 'amount_desc':
        selected.sort(key=lambda row: abs(money(row['amount'])), reverse=True)
    selected.sort(key=lambda row: row['reviewed'])
    table_columns = set(column_filters) | ({order_column} if order_column else set())
    basic_columns = {'date', 'account', 'detail', 'amount'}
    if table_columns and table_columns <= basic_columns:
        filtered = [dict(row) for row in selected]
        for row in filtered:
            row['table_values'] = _transaction_basic_table_values(row)
        filtered = _filter_and_sort_table_rows(
            filtered, column_filters, order_column, order_direction)
        total = len(filtered)
        rows = _transaction_list_rows(
            store, filtered[page * _PAGE:(page + 1) * _PAGE])
    elif column_filters or order_column:
        rows = _filter_and_sort_table_rows(
            _transaction_list_rows(store, selected), column_filters,
            order_column, order_direction)
        total = len(rows)
        rows = rows[page * _PAGE:(page + 1) * _PAGE]
    else:
        rows = _transaction_list_rows(store, selected[page * _PAGE:(page + 1) * _PAGE])
    return {'rows': rows, 'categories': _catalog(store), 'total': total,
            'page': page, 'pages': (total + _PAGE - 1) // _PAGE}


def _filter_and_sort_table_rows(rows, filters, order_column, order_direction):
    """Apply identical display-value semantics to basic and enriched rows."""
    rows = [row for row in rows if all(
        needle.casefold() in row['table_values'][column].casefold()
        for column, needle in filters.items())]
    if order_column:
        # Explicit column sorting spans reviewed and unreviewed rows alike.
        rows.sort(key=lambda row: (row['account_id'], row['external_id']))
        rows.sort(key=lambda row: row['date'], reverse=True)
        rows.sort(key=lambda row: money(row['amount']) if order_column == 'amount'
                  else row['table_values'][order_column].casefold(),
                  reverse=order_direction == 'desc')
    return rows


def _transaction_list_rows(store, selected):
    """Enrich bounded batches to avoid SQLite expression/parameter limits."""
    catalog_by_id = {row['id']: dict(row) for row in store.db.execute(
        'SELECT id,label,transaction_type,parent_id FROM category_catalog')}
    rules_by_key = {}
    for rule in store.db.execute(
            'SELECT r.id,r.counterparty_normalized,r.direction,r.category_id,c.parent_id '
            'FROM classification_rules r JOIN category_catalog c ON c.id=r.category_id '
            'ORDER BY r.id'):
        rules_by_key.setdefault(
            (rule['counterparty_normalized'], rule['direction']), dict(rule))
    rows = []
    batch_size = 400  # 800 pair parameters stay below SQLite's legacy 999 limit.
    for offset in range(0, len(selected), batch_size):
        rows.extend(_transaction_list_batch(
            store, selected[offset:offset + batch_size], catalog_by_id, rules_by_key))
    return rows


def _transaction_list_batch(store, selected, catalog_by_id, rules_by_key):
    rows = []
    selected_keys = {(row['account_id'], row['external_id']) for row in selected}
    payment_statuses = statuses_for_transactions(store, selected_keys)
    pair_clauses = ' OR '.join('(r.account_id=? AND r.external_id=?)' for _ in selected_keys)
    pair_params = [value for key in sorted(selected_keys) for value in key]
    model_rows = [] if not selected_keys else store.db.execute(
            "SELECT r.account_id,r.external_id,r.category_id,r.confidence,r.model,c.parent_id "
            "FROM classification_model_reviews r JOIN category_catalog c ON c.id=r.category_id "
            "WHERE r.status='proposed' AND (" + pair_clauses + ')', pair_params)
    models_by_key = {
        (row['account_id'], row['external_id']): dict(row) for row in model_rows
    }
    links_by_key = {key: [] for key in selected_keys}
    if selected_keys:
        clauses = ' OR '.join('(l.account_id=? AND l.external_id=?)' for _ in selected_keys)
        link_params = [value for key in sorted(selected_keys) for value in key]
        for link in store.db.execute(
                'SELECT l.account_id,l.external_id,d.id,d.kind,d.vendor,d.title,d.status,d.warnings,'
                'l.allocated_amount,l.allocation_type FROM classification_document_links l '
                'JOIN classification_documents d ON d.id=l.document_id WHERE ' + clauses +
                ' ORDER BY d.id', link_params):
            value = {
                key: link[key] for key in (
                    'id', 'kind', 'vendor', 'title', 'status',
                    'allocated_amount', 'allocation_type')}
            value['evidence_type'] = _evidence_type(
                link['kind'], link['title'], link['warnings'])
            links_by_key[(link['account_id'], link['external_id'])].append(value)
    for row in selected:
        item = dict(row)
        item.pop('reviewed')
        override_category = item.pop('category_id')
        item['revision'] = item['revision'] or 0
        item['direction'] = ('transfer' if item.pop('is_transfer_row') else
                             'income' if money(item['amount']) > 0 else 'expense')
        item['is_transfer'] = item['direction'] == 'transfer'
        item['category'] = 'TRANSFER' if item['is_transfer'] else (override_category if item['confirmed'] else None)
        item['confirmed'] = item['is_transfer'] or bool(item['confirmed'])
        item['category_proposal'] = None
        item['source_context_complete'] = source_context_complete(
            item['counterparty'], item['description'])
        if item['is_transfer']:
            item['transfer_display'] = transfer_display(store, item)
            item['category_proposal'] = {
                'category': 'TRANSFER', 'parent_category': None,
                'confidence': 'high', 'provenance': 'confirmed',
            }
        elif item['confirmed']:
            category = catalog_by_id.get(item['category'])
            item['category_proposal'] = {
                'category': item['category'],
                'parent_category': None if category is None else category['parent_id'],
                'confidence': 'high', 'provenance': 'confirmed',
            }
        else:
            item['transfer_display'] = None
            normalized_counterparty = (
                normalize_counterparty(item['counterparty'])
                if item['counterparty'] and item['counterparty'].strip() else None)
            rule = rules_by_key.get((normalized_counterparty, item['direction']))
            model = models_by_key.get((item['account_id'], item['external_id']))
            if (rule is not None and item['source_context_complete']
                    and _safe_exact_rule(store, rule['category_id'],
                                         item['counterparty'], item['description'])):
                item['category_proposal'] = {
                    'category': rule['category_id'], 'parent_category': rule['parent_id'],
                    'confidence': 'high',
                    'provenance': 'local_rule_exact_counterparty_and_direction',
                }
            elif model is not None:
                item['category_proposal'] = {
                    'category': model['category_id'], 'parent_category': model['parent_id'],
                    'confidence': model['confidence'],
                    'provenance': f"ollama:{model['model']}",
                }
            else:
                merchant = _merchant_family_match(
                    store, item['counterparty'], item['description'], item['direction'])
                if merchant is not None:
                    item['category_proposal'] = {
                        'category': merchant['category'],
                        'parent_category': merchant['parent_category'],
                        'confidence': 'high',
                        'provenance': merchant['provenance'],
                    }
        item['links'] = links_by_key[(item['account_id'], item['external_id'])]
        item['payment_statuses'] = payment_statuses[(item['account_id'], item['external_id'])]
        item['table_values'] = _transaction_table_values(item, catalog_by_id)
        rows.append(item)
    return rows


def _transaction_basic_table_values(item):
    """Display values available without category, document, or payment enrichment."""
    complete = source_context_complete(item['counterparty'], item['description'])
    return {
        'date': item['date'], 'account': item['account_label'],
        'detail': ((item['counterparty'] or 'Keine Gegenpartei') + ' · ' +
                   (item['description'] or '' if complete else
                    'Quelldetails fehlen: Empfänger und Verwendungszweck sind nicht enthalten.')),
        'amount': f"{money(item['amount']):.2f}".replace('.', ','),
    }


def _transaction_table_values(item, catalog):
    """Searchable German display values for all seven transaction data columns."""
    def hierarchy(category, parent=None):
        entry = catalog.get(category, {})
        parent = parent or entry.get('parent_id')
        label = entry.get('label', category or '')
        return ((catalog.get(parent, {}).get('label', parent) + ' > ') if parent else '') + label

    proposal = item['category_proposal']
    exact = proposal and proposal['provenance'] == 'local_rule_exact_counterparty_and_direction'
    confidence = {'high': 'hoch', 'medium': 'mittel', 'low': 'niedrig'}
    if proposal:
        source = ('Vorschlag aus Deiner Regel' if exact else
                  'Vorschlag aus Händlerregel' if proposal['provenance'] == 'local_merchant_family_rule' else
                  'Vorschlag des lokalen Modells' if proposal['provenance'].startswith('ollama:') else 'Bestätigt')
        meta = ('Bestätigt' if item['confirmed'] else
                source + ' · Sicherheit ' + confidence.get(proposal['confidence'], proposal['confidence']))
        detected = hierarchy(proposal['category'], proposal['parent_category']) + ' · ' + meta
        if item.get('transfer_display'):
            detected += ' · ' + item['transfer_display']
    else:
        detected = 'Noch kein eigener Vorschlag'
    status = ('Bestätigte Umbuchung' if item['is_transfer'] else 'Geprüft' if item['confirmed']
              else 'Regelvorschlag kann bestätigt werden' if exact else 'Kategorie auswählen, dann bestätigen')
    for link in item['links']:
        kind = ('Bestellbestätigung' if link['evidence_type'] == 'order_confirmation' else
                'Rechnung' if link['kind'] == 'invoice' else 'Vertrag')
        allocation = {'evidence': 'Zahlungsnachweis', 'refund': 'Erstattung'}.get(link['allocation_type'], 'Zahlung')
        value = link['allocated_amount']
        status += f" · {kind}: {link['title']} · {allocation} · " + (
            'Vollbetrag' if value is None else f"{money(value):.2f}".replace('.', ',') + ' €')
    payment_labels = {'authorization': 'autorisiert', 'processing': 'in Bearbeitung',
                      'paid': 'erfolgreich bezahlt', 'payment_plan': 'Zahlungsplan'}
    for event in item['payment_statuses']:
        warning = {'conflict': 'Zuordnung inzwischen mehrdeutig',
                   'stale': 'passende Buchung nicht mehr im Abgleich'}.get(event['match_status'], '')
        status += (' · ' + ('PayPal' if event['provider'] == 'paypal' else 'Klarna') + ' · '
                   + payment_labels.get(event['event_status'], event['event_status'] or 'offen')
                   + ' · ' + event['event_date'] + (' · ' + warning if warning else ''))
    return {
        'date': item['date'], 'account': item['account_label'],
        'detail': ((item['counterparty'] or 'Keine Gegenpartei') + ' · ' +
                   (item['description'] or '' if item['source_context_complete'] else
                    'Quelldetails fehlen: Empfänger und Verwendungszweck sind nicht enthalten.')),
        'amount': f"{money(item['amount']):.2f}".replace('.', ','),
        'detected': detected,
        'category': '' if item['is_transfer'] else ('Regelvorschlag prüfen' if exact else 'Kategorie wählen'),
        'status': status,
    }


def get_transaction(store, data):
    """Return one current transaction including local category and document links."""
    if (not isinstance(data, dict) or set(data) != {'account_id', 'external_id'}):
        raise ValueError('invalid_transaction_lookup')
    account_id = _text(data['account_id'], 'account_id', 120)
    external_id = _text(data['external_id'], 'external_id', 240)
    row = store.db.execute(
        """SELECT t.account_id,
                  CASE WHEN NULLIF(TRIM(a.display_name),'') IS NULL THEN t.account_id
                       ELSE t.account_id || ' · ' || a.display_name END AS account_label,
                  t.external_id,t.date,t.amount,t.currency,
                  t.category AS source_category,t.transfer_id,c.counterparty,c.description,
                  o.category_id,o.confirmed,o.revision
           FROM transactions t
           JOIN accounts a ON a.id=t.account_id
           LEFT JOIN transaction_context c USING(account_id,external_id)
           LEFT JOIN classification_overrides o USING(account_id,external_id)
           WHERE t.account_id=? AND t.external_id=?""", (account_id, external_id)).fetchone()
    if row is None:
        raise ValueError('unknown_transaction')
    item = dict(row)
    override_category = item.pop('category_id')
    item['revision'] = item['revision'] or 0
    item['direction'] = _direction(store, item)
    item['is_transfer'] = item['direction'] == 'transfer'
    item['category'] = 'TRANSFER' if item['is_transfer'] else (override_category if item['confirmed'] else None)
    item['confirmed'] = item['is_transfer'] or bool(item['confirmed'])
    item['transfer_display'] = transfer_display(store, item) if item['is_transfer'] else None
    item['source_context_complete'] = source_context_complete(
        item['counterparty'], item['description'])
    item['links'] = _links_for_transaction(store, account_id, external_id)
    item['payment_statuses'] = statuses_for_transactions(
        store, {(account_id, external_id)})[(account_id, external_id)]
    return {'transaction': item}


def save_classification(store, data):
    if not isinstance(data, dict) or set(data) != {'account_id', 'external_id', 'category', 'revision'} or type(data['revision']) is not int or data['revision'] < 0:
        raise ValueError('invalid_classification')
    account_id, external_id = _text(data['account_id'], 'account_id', 120), _text(data['external_id'], 'external_id', 240)
    store.db.execute('BEGIN IMMEDIATE')
    try:
        transaction = _transaction(store, account_id, external_id)
        direction = _direction(store, transaction)
        if direction == 'transfer':
            raise ValueError('transfer_classification_immutable')
        category = _category(store, data['category'], direction)
        current = store.db.execute('SELECT category_id,confirmed,revision FROM classification_overrides WHERE account_id=? AND external_id=?', (account_id, external_id)).fetchone()
        current_revision = 0 if current is None else current['revision']
        if current_revision != data['revision']:
            raise ValueError('stale_revision')
        revision = current_revision + 1
        store.db.execute('INSERT INTO classification_overrides VALUES (?,?,?,?,?) ON CONFLICT(account_id,external_id) DO UPDATE SET category_id=excluded.category_id,confirmed=excluded.confirmed,revision=excluded.revision',
                         (account_id, external_id, category['id'], 1, revision))
        store.db.execute(
            'DELETE FROM classification_model_reviews WHERE account_id=? AND external_id=?',
            (account_id, external_id))
        result = {'account_id': account_id, 'external_id': external_id,
                  'category': category['id'], 'parent_category': category['parent_id'],
                  'confirmed': True, 'revision': revision}
        _audit(store, 'classification_saved', result, account_id, external_id, previous=None if current is None else dict(current))
        store.db.commit()
    except Exception:
        store.db.rollback()
        raise
    return {'classification': result}


def reject_model_suggestion(store, data):
    """Dismiss exactly one still-current local model proposal without classifying it."""
    if (not isinstance(data, dict)
            or set(data) != {'account_id', 'external_id', 'category_id', 'revision'}
            or type(data['revision']) is not int or data['revision'] < 0):
        raise ValueError('invalid_model_suggestion_rejection')
    account_id = _text(data['account_id'], 'account_id', 120)
    external_id = _text(data['external_id'], 'external_id', 240)
    category_id = _text(data['category_id'], 'category_id', 120)
    store.db.execute('BEGIN IMMEDIATE')
    try:
        _transaction(store, account_id, external_id)
        current = store.db.execute(
            'SELECT confirmed,revision FROM classification_overrides WHERE account_id=? AND external_id=?',
            (account_id, external_id)).fetchone()
        current_revision = 0 if current is None else current['revision']
        if current_revision != data['revision']:
            raise ValueError('stale_revision')
        if current is not None and current['confirmed']:
            raise ValueError('no_current_model_suggestion')
        proposal = store.db.execute(
            "SELECT category_id,confidence,rationale,model,status,reviewed_at "
            "FROM classification_model_reviews WHERE account_id=? AND external_id=? "
            "AND status='proposed'", (account_id, external_id)).fetchone()
        if proposal is None:
            raise ValueError('no_current_model_suggestion')
        if proposal['category_id'] != category_id:
            raise ValueError('stale_revision')
        store.db.execute(
            'INSERT OR IGNORE INTO classification_model_rejections VALUES (?,?,?)',
            (account_id, external_id, category_id))
        # Reuse the classification revision so stale saves and batch confirmations
        # conflict with a rejection too. confirmed=0 keeps this booking unclassified.
        revision = current_revision + 1
        store.db.execute(
            'INSERT INTO classification_overrides VALUES (?,?,?,?,?) '
            'ON CONFLICT(account_id,external_id) DO UPDATE SET category_id=NULL,revision=excluded.revision',
            (account_id, external_id, None, 0, revision))
        store.db.execute(
            'DELETE FROM classification_model_reviews WHERE account_id=? AND external_id=?',
            (account_id, external_id))
        result = {'account_id': account_id, 'external_id': external_id,
                  'category_id': category_id, 'revision': revision, 'rejected': True}
        _audit(store, 'model_suggestion_rejected', result, account_id, external_id,
               previous=dict(proposal))
        store.db.commit()
    except Exception:
        store.db.rollback()
        raise
    return {'rejection': result}


def save_classifications(store, data):
    """Confirm a complete category batch atomically using the normal revision contract."""
    if (not isinstance(data, dict) or set(data) != {'confirmed', 'classifications'}
            or data['confirmed'] is not True or not isinstance(data['classifications'], list)
            or not 1 <= len(data['classifications']) <= 500):
        raise ValueError('invalid_classification_batch')
    received = data['classifications']
    keys = set()
    for item in received:
        if (not isinstance(item, dict)
                or set(item) != {'account_id', 'external_id', 'category', 'revision'}
                or type(item['revision']) is not int or item['revision'] < 0):
            raise ValueError('invalid_classification_batch')
        key = (_text(item['account_id'], 'account_id', 120),
               _text(item['external_id'], 'external_id', 240))
        if key in keys:
            raise ValueError('duplicate_classification_batch_member')
        keys.add(key)
    store.db.execute('BEGIN IMMEDIATE')
    try:
        prepared = []
        for item in received:
            account_id = _text(item['account_id'], 'account_id', 120)
            external_id = _text(item['external_id'], 'external_id', 240)
            transaction = _transaction(store, account_id, external_id)
            direction = _direction(store, transaction)
            if direction == 'transfer':
                raise ValueError('transfer_classification_immutable')
            category = _category(store, item['category'], direction)
            current = store.db.execute(
                'SELECT category_id,confirmed,revision FROM classification_overrides '
                'WHERE account_id=? AND external_id=?', (account_id, external_id)).fetchone()
            current_revision = 0 if current is None else current['revision']
            if current_revision != item['revision']:
                raise ValueError('stale_revision')
            prepared.append((account_id, external_id, category, current, current_revision + 1))
        results = []
        for account_id, external_id, category, current, revision in prepared:
            store.db.execute(
                'INSERT INTO classification_overrides VALUES (?,?,?,?,?) '
                'ON CONFLICT(account_id,external_id) DO UPDATE SET '
                'category_id=excluded.category_id,confirmed=excluded.confirmed,revision=excluded.revision',
                (account_id, external_id, category['id'], 1, revision))
            store.db.execute(
                'DELETE FROM classification_model_reviews WHERE account_id=? AND external_id=?',
                (account_id, external_id))
            result = {'account_id': account_id, 'external_id': external_id,
                      'category': category['id'], 'parent_category': category['parent_id'],
                      'confirmed': True, 'revision': revision}
            _audit(store, 'classification_saved', result, account_id, external_id,
                   previous=None if current is None else dict(current))
            results.append(result)
        store.db.commit()
    except Exception:
        store.db.rollback()
        raise
    return {'classifications': results}


def reset_classification(store, data):
    """Remove an explicit override; the immutable source category remains visible."""
    if not isinstance(data, dict) or set(data) != {'account_id', 'external_id', 'revision', 'confirmed'} or type(data['revision']) is not int or data['confirmed'] is not True:
        raise ValueError('explicit_reset_confirmation_required')
    account_id, external_id = _text(data['account_id'], 'account_id', 120), _text(data['external_id'], 'external_id', 240)
    store.db.execute('BEGIN IMMEDIATE')
    try:
        current = store.db.execute('SELECT category_id,confirmed,revision FROM classification_overrides WHERE account_id=? AND external_id=?', (account_id, external_id)).fetchone()
        if current is None:
            raise ValueError('no_classification_override')
        if current['revision'] != data['revision']:
            raise ValueError('stale_revision')
        revision = current['revision'] + 1
        store.db.execute('UPDATE classification_overrides SET category_id=NULL, confirmed=0, revision=? WHERE account_id=? AND external_id=?',
                         (revision, account_id, external_id))
        _audit(store, 'classification_reset', {'revision': revision, 'confirmed': False}, account_id, external_id, previous=dict(current))
        store.db.commit()
    except Exception:
        store.db.rollback()
        raise
    return {'reset': True, 'revision': revision}


def create_rule(store, data):
    if not isinstance(data, dict) or set(data) != {'counterparty', 'direction', 'category'}:
        raise ValueError('invalid_rule')
    direction = data['direction']
    if direction not in {'income', 'expense'}:
        raise ValueError('invalid_rule_direction')
    normalized = normalize_counterparty(data['counterparty'])
    store.db.execute('BEGIN IMMEDIATE')
    try:
        category = _category(store, data['category'], direction)
        cursor = store.db.execute('INSERT INTO classification_rules(counterparty_normalized,direction,category_id,revision) VALUES (?,?,?,1)',
                                  (normalized, direction, category['id']))
        result = {'id': cursor.lastrowid, 'counterparty': normalized, 'direction': direction,
                  'category': category['id'], 'parent_category': category['parent_id'], 'revision': 1}
        _audit(store, 'rule_created', result)
        store.db.commit()
    except Exception:
        store.db.rollback()
        raise
    return {'rule': result}


def suggestions(store, data):
    if not isinstance(data, dict) or set(data) != {'account_id', 'external_id'}:
        raise ValueError('invalid_suggestion_request')
    transaction = _transaction(store, _text(data['account_id'], 'account_id', 120), _text(data['external_id'], 'external_id', 240))
    if _direction(store, transaction) == 'transfer':
        return {'suggestions': []}
    result = []
    model = store.db.execute(
        """SELECT r.category_id,r.confidence,r.rationale,r.model,c.parent_id
           FROM classification_model_reviews r
           JOIN category_catalog c ON c.id=r.category_id
           WHERE r.account_id=? AND r.external_id=? AND r.status='proposed'""",
        (transaction['account_id'], transaction['external_id'])).fetchone()
    if model is not None:
        result.append({'category': model['category_id'],
                       'parent_category': model['parent_id'],
                       'reason': 'local_model', 'rationale': model['rationale'],
                       'provenance': f"ollama:{model['model']}",
                       'confidence': model['confidence']})
    context = store.db.execute('SELECT counterparty,description FROM transaction_context WHERE account_id=? AND external_id=?',
                               (transaction['account_id'], transaction['external_id'])).fetchone()
    if context is None or not context['counterparty'].strip():
        return {'suggestions': result}
    normalized, direction = normalize_counterparty(context['counterparty']), _direction(store, transaction)
    rows = store.db.execute('SELECT r.id,r.category_id,c.parent_id FROM classification_rules r '
                            'JOIN category_catalog c ON c.id=r.category_id '
                            'WHERE r.counterparty_normalized=? AND r.direction=? ORDER BY r.id',
                            (normalized, direction)).fetchall()
    result.extend(
        {'rule_id': row['id'], 'category': row['category_id'],
         'parent_category': row['parent_id'],
         'reason': 'exact_counterparty_and_direction',
         'provenance': 'local_rule_exact_counterparty_and_direction',
         'confidence': 'high'}
        for row in rows if _safe_exact_rule(
            store, row['category_id'], context['counterparty'], context['description']))
    if not rows:
        merchant = _merchant_family_match(
            store, context['counterparty'], context['description'], direction)
        if merchant is not None:
            result.append({
                'category': merchant['category'],
                'parent_category': merchant['parent_category'],
                'reason': 'merchant_family_policy',
                'provenance': merchant['provenance'],
                'confidence': 'high',
            })
    return {'suggestions': result}


def _safe_exact_rule(store, category_id, counterparty, description):
    """A learned cash rule needs cash evidence; a bank name also describes loans."""
    if canonical_category_id(store, category_id) != 'AUSGABEN_BARGELD':
        return True
    from .cash_components import is_cash_withdrawal
    return is_cash_withdrawal({'counterparty': counterparty, 'description': description})


def exact_rule_match(store, counterparty, description, direction):
    """Return one safe local rule match without consulting source categories."""
    if direction not in {'income', 'expense'}:
        return None
    if not source_context_complete(counterparty, description):
        return None
    row = store.db.execute(
        'SELECT r.id,r.category_id,c.parent_id,c.label FROM classification_rules r '
        'JOIN category_catalog c ON c.id=r.category_id '
        'WHERE r.counterparty_normalized=? AND r.direction=?',
        (normalize_counterparty(counterparty), direction)).fetchone()
    if row is None:
        return _merchant_family_match(store, counterparty, description, direction)
    if not _safe_exact_rule(store, row['category_id'], counterparty, description):
        return None
    return {'rule_id': row['id'], 'category': row['category_id'],
            'parent_category': row['parent_id'], 'label': row['label'],
            'provenance': 'local_rule_exact_counterparty_and_direction'}


def apply_exact_rule_if_safe(store, account_id, external_id, *, allow_empty_override=False):
    """Apply a deterministic local rule inside the caller's open transaction.

    Snapshot reconciliation may replace its empty reset anchor, preserving the
    revision chain. All other existing classification decisions remain intact.
    """
    transaction = _transaction(store, account_id, external_id)
    direction = _direction(store, transaction)
    match = exact_rule_match(store, transaction['counterparty'],
                             transaction['description'], direction)
    if match is None:
        return None
    current = store.db.execute(
        'SELECT * FROM classification_overrides WHERE account_id=? AND external_id=?',
        (account_id, external_id)).fetchone()
    if current is not None and (not allow_empty_override or current['category_id'] is not None
                                or current['confirmed'] != 0):
        return None
    revision = 1 if current is None else current['revision'] + 1
    if current is None:
        store.db.execute(
            'INSERT INTO classification_overrides VALUES (?,?,?,?,?)',
            (account_id, external_id, match['category'], 1, revision))
    else:
        store.db.execute(
            'UPDATE classification_overrides SET category_id=?,confirmed=1,revision=? '
            'WHERE account_id=? AND external_id=?',
            (match['category'], revision, account_id, external_id))
    result = {'account_id': account_id, 'external_id': external_id,
              'category': match['category'], 'parent_category': match['parent_category'],
              'confirmed': True, 'revision': revision, 'rule_id': match['rule_id'],
              'provenance': match['provenance']}
    _audit(store, 'classification_auto_applied', result, account_id, external_id,
           previous=None if current is None else dict(current))
    return result


def apply_pending_exact_rules(store, data):
    """Apply already confirmed local rules to older open bookings in one transaction."""
    if data != {'confirmed': True}:
        raise ValueError('explicit_rule_backfill_confirmation_required')
    rows = store.db.execute(
        'SELECT t.account_id,t.external_id FROM transactions t '
        'LEFT JOIN classification_overrides o USING(account_id,external_id) '
        'WHERE o.account_id IS NULL OR o.confirmed=0 '
        'ORDER BY t.date,t.account_id,t.external_id').fetchall()
    applied = []
    store.db.execute('BEGIN IMMEDIATE')
    try:
        for row in rows:
            result = apply_exact_rule_if_safe(
                store, row['account_id'], row['external_id'], allow_empty_override=True)
            if result is not None:
                applied.append(result)
        store.db.commit()
    except Exception:
        store.db.rollback()
        raise
    return {'applied': applied, 'checked': len(rows)}


def match_suggestions(store, data):
    """Return individual invoices and exact-sum invoice groups for one payment."""
    if not isinstance(data, dict) or set(data) != {'account_id', 'external_id', 'max_days'} or type(data['max_days']) is not int or not 0 <= data['max_days'] <= 45:
        raise ValueError('invalid_match_request')
    account_id, external_id = _text(data['account_id'], 'account_id', 120), _text(data['external_id'], 'external_id', 240)
    transaction = _transaction(store, account_id, external_id)
    if effective_transfer_id(store, transaction) or money(transaction['amount']) >= 0 or transaction['currency'] != 'EUR':
        return {'suggestions': [], 'suggestion_groups': []}
    payment = date.fromisoformat(transaction['date'])
    used_transaction = sum((money(link['allocated_amount']) for link in store.db.execute(
        'SELECT allocated_amount FROM classification_document_links WHERE account_id=? AND external_id=?',
        (account_id, external_id))), Decimal(0))
    transaction_remaining = abs(money(transaction['amount'])) - used_transaction
    if transaction_remaining <= 0:
        return {'suggestions': [], 'suggestion_groups': []}
    days = timedelta(days=data['max_days'])
    date_from = date.min if payment < date.min + days else payment - days
    rows = store.db.execute(
        "SELECT d.* FROM classification_documents d WHERE d.kind='invoice' AND d.amount IS NOT NULL "
        "AND d.currency='EUR' AND d.document_date IS NOT NULL "
        "AND d.document_date>=? AND d.document_date<=? ORDER BY d.document_date,d.id",
        (date_from.isoformat(), payment.isoformat())).fetchall()
    suggestions = []
    group_candidates = []
    for document in rows:
        if _document_link_rejected(store, account_id, external_id, document['id']):
            continue
        if not _document_match_eligible(store, document):
            continue
        if store.db.execute(
                'SELECT 1 FROM classification_document_links WHERE account_id=? AND external_id=? AND document_id=?',
                (account_id, external_id, document['id'])).fetchone():
            continue
        if not document_recipient_matches(
                document['vendor'], transaction['counterparty'], transaction['description'],
                document['title']):
            continue
        allocated = sum((money(link['allocated_amount']) for link in store.db.execute(
            "SELECT allocated_amount FROM classification_document_links WHERE document_id=? AND allocation_type='payment'",
            (document['id'],))), Decimal(0))
        from .bonsy_vouchers import voucher_total
        document_remaining = money(document['amount']) - allocated - voucher_total(store, document['id'])
        if document_remaining <= 0:
            continue
        candidate = {
            'document_id': document['id'], 'kind': document['kind'],
            'vendor': document['vendor'], 'title': document['title'],
            'document_date': document['document_date'], 'status': document['status'],
            'remaining_amount': document_remaining,
        }
        group_candidates.append(candidate)
        if document_remaining == transaction_remaining:
            suggestions.append({key: value for key, value in candidate.items()
                                if key != 'remaining_amount'} | {
                                    'allocated_amount': format(document_remaining, '.2f'),
                                    'reason': 'exact_remaining_amount_date_and_recipient',
                                    'require_confirmation': True,
                                })
    suggestion_groups = []
    for members in _exact_sum_groups(group_candidates, transaction_remaining, 'remaining_amount'):
        suggestion_groups.append({
            'group_type': 'multiple_documents_one_transaction',
            'allocation_type': 'payment',
            'allocated_amount': format(transaction_remaining, '.2f'),
            'reason': 'combined_exact_remaining_amount_date_and_recipient',
            'require_confirmation': True,
            'documents': [
                {key: (format(value, '.2f') if key == 'remaining_amount' else value)
                 for key, value in member.items()}
                for member in members
            ],
        })
    return {'suggestions': suggestions, 'suggestion_groups': suggestion_groups}


def document_match_suggestions(store, data):
    """Return eligible payment/refund candidates for one invoice.

    Suggestions are informational only. Individual candidates cover the open
    rest alone; bounded groups cover it through an exact sum.
    """
    required = {'document_id', 'max_days', 'page'}
    if (not isinstance(data, dict) or set(data) != required or type(data['document_id']) is not int
            or data['document_id'] < 1 or type(data['max_days']) is not int
            or not 0 <= data['max_days'] <= 45 or type(data['page']) is not int or data['page'] < 0):
        raise ValueError('invalid_document_match_request')
    document = store.db.execute('SELECT * FROM classification_documents WHERE id=?',
                                (data['document_id'],)).fetchone()
    if document is None:
        raise ValueError('unknown_document')
    linked_transactions = []
    linked_query = """SELECT t.account_id,t.external_id,t.date,t.amount,t.currency,t.category AS source_category,
        t.transfer_id,c.counterparty,c.description,o.category_id,o.confirmed,o.revision
        FROM classification_document_links l JOIN transactions t ON (t.account_id=l.account_id AND t.external_id=l.external_id)
        LEFT JOIN transaction_context c ON (c.account_id=t.account_id AND c.external_id=t.external_id)
        LEFT JOIN classification_overrides o ON (o.account_id=t.account_id AND o.external_id=t.external_id)
        WHERE l.document_id=? ORDER BY t.date,t.account_id,t.external_id"""
    for row in store.db.execute(linked_query, (document['id'],)):
        item = dict(row)
        override_category = item.pop('category_id')
        item.update({'revision': item['revision'] or 0, 'direction': _direction(store, item),
                     'is_transfer': _direction(store, item) == 'transfer',
                     'category': override_category if item['confirmed'] else None,
                     'confirmed': bool(item['confirmed']),
                     'links': [{'id': document['id'], 'kind': document['kind'],
                                'vendor': document['vendor'], 'title': document['title'],
                                'status': document['status']}],
                     'reason': 'already_linked', 'require_confirmation': False})
        item['links'] = _links_for_transaction(store, item['account_id'], item['external_id'])
        linked_transactions.append(item)
    document_amount = (money(document['amount']) if document['amount'] is not None else Decimal(0))
    allocations = store.db.execute(
        'SELECT allocation_type,allocated_amount FROM classification_document_links WHERE document_id=?',
        (document['id'],)).fetchall()
    used_by_type = {
        allocation_type: sum((money(row['allocated_amount']) for row in allocations
                              if row['allocation_type'] == allocation_type), Decimal(0))
        for allocation_type in ('payment', 'refund')
    }
    refund_supported = _supports_refund_suggestion(store, document) or used_by_type['refund'] > 0
    api_refund_total = _api_refund_total(store, document['id'])
    refund_limit = min(document_amount, api_refund_total) if api_refund_total else document_amount
    from .bonsy_vouchers import voucher_total
    voucher_amount = voucher_total(store, document['id'])
    remaining = {
        'payment': max(document_amount - used_by_type['payment'] - voucher_amount, Decimal(0)),
        'refund': max(refund_limit - used_by_type['refund'], Decimal(0))
        if refund_supported else Decimal(0),
    }
    remaining_result = {f'remaining_{key}': format(value, '.2f') for key, value in remaining.items()}

    if (document['kind'] != 'invoice' or document['amount'] is None or document_amount <= 0
            or document['currency'] != 'EUR' or document['document_date'] is None
            or not _document_match_eligible(store, document)):
        return {'suggestions': [], 'suggestion_groups': [],
                'linked_transactions': linked_transactions,
                'match_status': 'document_not_eligible',
                'total': 0, 'page': data['page'], 'pages': 0, **remaining_result}
    issued = date.fromisoformat(document['document_date'])
    days = timedelta(days=data['max_days'])
    date_to = date.max if issued > date.max - days else issued + days
    params = (issued.isoformat(), date_to.isoformat())
    eligible = f""" FROM transactions t
        LEFT JOIN transaction_context c ON (c.account_id=t.account_id AND c.external_id=t.external_id)
        LEFT JOIN classification_overrides o ON (o.account_id=t.account_id AND o.external_id=t.external_id)
        WHERE NOT COALESCE({sql_transfer_predicate(context_alias='c')},0)
        AND t.currency='EUR' AND t.amount != '0.00'
        AND t.date>=? AND t.date<=?"""
    candidates = []
    group_candidates = {'payment': [], 'refund': []}
    query = ('SELECT t.account_id,t.external_id,t.date,t.amount,t.currency,t.category AS source_category,'
             't.transfer_id,c.counterparty,c.description,o.category_id,o.confirmed,o.revision' + eligible
             + ' ORDER BY t.date ASC,t.account_id,t.external_id')
    for row in store.db.execute(query, params):
        amount = money(row['amount'])
        allocation_type = 'payment' if amount < 0 else 'refund'
        document_remaining = remaining[allocation_type]
        if document_remaining <= 0:
            continue
        if store.db.execute(
                'SELECT 1 FROM classification_document_links WHERE account_id=? AND external_id=? AND document_id=?',
                (row['account_id'], row['external_id'], document['id'])).fetchone():
            continue
        if _document_link_rejected(store, row['account_id'], row['external_id'], document['id']):
            continue
        used_transaction = sum((money(link['allocated_amount']) for link in store.db.execute(
            'SELECT allocated_amount FROM classification_document_links WHERE account_id=? AND external_id=?',
            (row['account_id'], row['external_id']))), Decimal(0))
        transaction_remaining = abs(amount) - used_transaction
        if transaction_remaining <= 0:
            continue
        recipient_matches = document_recipient_matches(
            document['vendor'], row['counterparty'], row['description'], document['title'])
        if not recipient_matches:
            continue
        group_candidates[allocation_type].append({
            'row': row, 'remaining_amount': transaction_remaining,
        })
        if transaction_remaining == document_remaining:
            reason = 'exact_remaining_amount_date_and_recipient'
        elif transaction_remaining > document_remaining and document['status'] == 'confirmed':
            reason = 'vendor_and_date_cover_remaining_amount'
        else:
            continue
        candidates.append((row, allocation_type, document_remaining, reason))
    total = len(candidates)
    suggestions = []
    start = data['page'] * _DOCUMENT_MATCH_PAGE
    for row, allocation_type, document_remaining, reason in candidates[start:start + _DOCUMENT_MATCH_PAGE]:
        item = dict(row)
        item.update({'is_transfer': False, 'direction': 'expense' if allocation_type == 'payment' else 'income',
                     'category': item.pop('category_id') if item['confirmed'] else None,
                     'confirmed': bool(item['confirmed']), 'revision': item['revision'] or 0, 'links': [],
                     'allocation_type': allocation_type,
                     'allocated_amount': format(document_remaining, '.2f'),
                     'reason': reason, 'require_confirmation': True})
        item['links'] = _links_for_transaction(store, item['account_id'], item['external_id'])
        suggestions.append(item)
    suggestion_groups = []
    for allocation_type in ('payment', 'refund'):
        document_remaining = remaining[allocation_type]
        if document_remaining <= 0:
            continue
        for members in _exact_sum_groups(
                group_candidates[allocation_type], document_remaining, 'remaining_amount'):
            transactions = []
            for member in members:
                item = dict(member['row'])
                item.update({
                    'is_transfer': False,
                    'direction': 'expense' if allocation_type == 'payment' else 'income',
                    'category': item.pop('category_id') if item['confirmed'] else None,
                    'confirmed': bool(item['confirmed']), 'revision': item['revision'] or 0,
                    'links': _links_for_transaction(
                        store, item['account_id'], item['external_id']),
                    'allocated_amount': format(member['remaining_amount'], '.2f'),
                })
                transactions.append(item)
            suggestion_groups.append({
                'group_type': 'one_document_multiple_transactions',
                'allocation_type': allocation_type,
                'allocated_amount': format(document_remaining, '.2f'),
                'reason': 'combined_exact_remaining_amount_date_and_recipient',
                'require_confirmation': True,
                'transactions': transactions,
            })
    visible_groups = suggestion_groups if data['page'] == 0 else []
    return {'suggestions': suggestions, 'suggestion_groups': visible_groups,
            'linked_transactions': linked_transactions,
            'match_status': 'already_linked' if linked_transactions else 'ok',
            'total': total, 'group_total': len(suggestion_groups), 'page': data['page'],
            'pages': (total + _DOCUMENT_MATCH_PAGE - 1) // _DOCUMENT_MATCH_PAGE,
            **remaining_result}


def _document(row):
    value = dict(row)
    value['warnings'] = json.loads(value['warnings'])
    value['evidence_type'] = _evidence_type(
        value['kind'], value['title'], value['warnings'])
    value['links'] = []
    return value


def register_document(store, data, *, before_commit=None, on_failure=None):
    required = {'kind', 'vendor', 'title', 'document_date', 'amount', 'currency', 'source_reference', 'warnings', 'status'}
    if not isinstance(data, dict) or set(data) != required:
        raise ValueError('invalid_document')
    kind, status = data['kind'], data['status']
    if kind not in _KINDS or status != 'unreviewed':
        raise ValueError('invalid_document')
    amount = _optional_amount(data['amount'])
    currency = None if data['currency'] is None else _text(data['currency'], 'currency', 3)
    if (amount is None) != (currency is None) or (currency is not None and currency != 'EUR'):
        raise ValueError('invalid_document_amount')
    value = (kind, _text(data['vendor'], 'vendor'), _text(data['title'], 'title'), _optional_date(data['document_date'], 'document_date'),
             amount, currency, _source_reference(data['source_reference']), json.dumps(_warnings(data['warnings'])), status)
    store.db.execute('BEGIN IMMEDIATE')
    try:
        if store.db.execute('SELECT 1 FROM classification_documents WHERE source_reference=?', (value[6],)).fetchone():
            raise ValueError('duplicate_source_reference')
        cursor = store.db.execute('INSERT INTO classification_documents(kind,vendor,title,document_date,amount,currency,source_reference,warnings,status,revision) VALUES (?,?,?,?,?,?,?,?,?,1)', value)
        result = _document(store.db.execute('SELECT * FROM classification_documents WHERE id=?', (cursor.lastrowid,)).fetchone())
        _audit(store, 'document_registered', result, document_id=result['id'])
        if before_commit is not None:
            before_commit(result['id'])
        store.db.commit()
    except Exception:
        try:
            if on_failure is not None and 'cursor' in locals():
                on_failure(cursor.lastrowid)
        finally:
            store.db.rollback()
        raise
    return {'document': result}


def refresh_document_candidate(store, data):
    """Replace extracted fields of an unreviewed, unlinked document candidate."""
    editable = {'kind', 'vendor', 'title', 'document_date', 'amount', 'currency',
                'source_reference', 'warnings', 'status'}
    required = {'id', 'revision', 'confirmed', *editable}
    if (not isinstance(data, dict) or set(data) != required
            or type(data['id']) is not int or type(data['revision']) is not int
            or data['confirmed'] is not True):
        raise ValueError('invalid_document_refresh')
    kind = data['kind']
    if kind not in _KINDS or data['status'] != 'unreviewed':
        raise ValueError('invalid_document_refresh')
    amount = _optional_amount(data['amount'])
    currency = None if data['currency'] is None else _text(data['currency'], 'currency', 3)
    if (amount is None) != (currency is None) or (currency is not None and currency != 'EUR'):
        raise ValueError('invalid_document_amount')
    document_date = _optional_date(data['document_date'], 'document_date')
    vendor = _text(data['vendor'], 'vendor')
    title = _text(data['title'], 'title')
    source_reference = _source_reference(data['source_reference'])
    warnings = json.dumps(_warnings(data['warnings']))

    store.db.execute('BEGIN IMMEDIATE')
    try:
        previous = store.db.execute('SELECT * FROM classification_documents WHERE id=?', (data['id'],)).fetchone()
        if previous is None:
            raise ValueError('unknown_document')
        if previous['revision'] != data['revision']:
            raise ValueError('stale_revision')
        if previous['kind'] != kind:
            raise ValueError('document_kind_is_immutable')
        if source_reference != previous['source_reference']:
            raise ValueError('source_reference_is_immutable')
        if previous['status'] != 'unreviewed':
            raise ValueError('document_candidate_not_unreviewed')
        if 'duplicate_source_document' in json.loads(previous['warnings']):
            raise ValueError('restore_duplicate_before_refresh')
        if store.db.execute('SELECT 1 FROM classification_document_links WHERE document_id=?',
                            (data['id'],)).fetchone():
            raise ValueError('document_candidate_linked')
        values = (vendor, title, document_date, amount, currency, warnings,
                  previous['revision'] + 1, data['id'], previous['revision'])
        updated = store.db.execute(
            'UPDATE classification_documents SET vendor=?,title=?,document_date=?,amount=?,currency=?,warnings=?,revision=? '
            'WHERE id=? AND revision=? AND status=\'unreviewed\'', values)
        if updated.rowcount != 1:
            raise ValueError('stale_revision')
        result = _document(store.db.execute('SELECT * FROM classification_documents WHERE id=?', (data['id'],)).fetchone())
        _audit(store, 'document_candidate_refreshed', result, document_id=data['id'], previous=dict(previous))
        store.db.commit()
    except Exception:
        store.db.rollback()
        raise
    return {'document': result}


def list_documents(store, data=None):
    data = {} if data is None else data
    allowed = {'query', 'page', 'year', 'status', 'linked', 'kind'}
    if (not isinstance(data, dict) or set(data) - allowed
            or type(data.get('page', 0)) is not int or data.get('page', 0) < 0):
        raise ValueError('invalid_document_list')
    # Rows explicitly identified as non-documents remain in the audit trail and
    # external source cache, but no longer belong in the active review queue.
    clauses = ['warnings NOT LIKE ?', 'warnings NOT LIKE ?', 'warnings NOT LIKE ?']
    params = ['%"not_invoice_like"%', '%"source_excluded_bonsy"%', '%"duplicate_source_document"%']
    if data.get('query') is not None:
        like = '%' + _text(data['query'], 'query', 240, empty=True).casefold() + '%'
        clauses.append('(lower(vendor) LIKE ? OR lower(title) LIKE ?)')
        params.extend([like, like])
    year = data.get('year')
    if year is not None:
        if year == 'unknown':
            clauses.append('document_date IS NULL')
        elif type(year) is int and 1 <= year <= 9999:
            clauses.append('substr(document_date,1,4)=?')
            params.append(f'{year:04d}')
        else:
            raise ValueError('invalid_document_year')
    status = data.get('status')
    if status is not None:
        if not isinstance(status, str) or status not in {'unreviewed', 'confirmed'}:
            raise ValueError('invalid_document_status')
        clauses.append('status=?')
        params.append(status)
    linked = data.get('linked')
    if linked is not None:
        if type(linked) is not bool:
            raise ValueError('invalid_document_linked')
        exists = ('EXISTS' if linked else 'NOT EXISTS')
        clauses.append(f'{exists} (SELECT 1 FROM classification_document_links l '
                       'WHERE l.document_id=classification_documents.id)')
    kind = data.get('kind')
    if kind is not None:
        if not isinstance(kind, str) or kind not in _KINDS:
            raise ValueError('invalid_document_kind')
        clauses.append('kind=?')
        params.append(kind)
    where = '' if not clauses else ' WHERE ' + ' AND '.join(clauses)
    page = data.get('page', 0)
    total = store.db.execute('SELECT COUNT(*) FROM classification_documents' + where, params).fetchone()[0]
    documents = []
    for row in store.db.execute(
            'SELECT * FROM classification_documents' + where
            + ' ORDER BY document_date IS NULL,document_date DESC,id DESC LIMIT ? OFFSET ?',
            params + [_PAGE, page * _PAGE]):
        document = _document(row)
        from .bonsy_vouchers import voucher_total
        document['voucher_amount'] = format(voucher_total(store, document['id']), '.2f')
        document['links'] = [dict(link) for link in store.db.execute(
            'SELECT account_id,external_id,allocated_amount,allocation_type FROM classification_document_links '
            'WHERE document_id=? ORDER BY account_id,external_id', (document['id'],))]
        documents.append(document)
    return {'documents': documents, 'total': total, 'page': page, 'pages': (total + _PAGE - 1) // _PAGE}


def get_document(store, data):
    """Return exactly one local document for a contextual comparison view."""
    if not isinstance(data, dict) or set(data) != {'id'} or type(data['id']) is not int or data['id'] < 1:
        raise ValueError('invalid_document_lookup')
    row = store.db.execute('SELECT * FROM classification_documents WHERE id=?', (data['id'],)).fetchone()
    if row is None:
        raise ValueError('unknown_document')
    document = _document(row)
    from .bonsy_vouchers import voucher_total
    document['voucher_amount'] = format(voucher_total(store, document['id']), '.2f')
    document['links'] = [dict(link) for link in store.db.execute(
        'SELECT account_id,external_id,allocated_amount,allocation_type FROM classification_document_links '
        'WHERE document_id=? ORDER BY account_id,external_id', (document['id'],))]
    return {'document': document}


def document_coverage(store, data=None):
    """Summarize document review and linking without reading external sources."""
    if data not in (None, {}):
        raise ValueError('invalid_document_coverage_request')
    rows = store.db.execute(
        'SELECT document_date,status,EXISTS('
        'SELECT 1 FROM classification_document_links l WHERE l.document_id=d.id) AS linked '
        'FROM classification_documents d WHERE warnings NOT LIKE ? '
        'AND warnings NOT LIKE ? AND warnings NOT LIKE ?',
        ('%"not_invoice_like"%', '%"source_excluded_bonsy"%', '%"duplicate_source_document"%')).fetchall()
    result = {
        'total': len(rows),
        'confirmed': sum(row['status'] == 'confirmed' for row in rows),
        'unreviewed': sum(row['status'] == 'unreviewed' for row in rows),
        'linked': sum(bool(row['linked']) for row in rows),
        'unlinked': sum(not row['linked'] for row in rows),
        'undated': sum(row['document_date'] is None for row in rows),
    }
    grouped = {}
    for row in rows:
        year = None if row['document_date'] is None else int(row['document_date'][:4])
        bucket = grouped.setdefault(year, {'year': year, 'count': 0, 'confirmed': 0, 'linked': 0})
        bucket['count'] += 1
        bucket['confirmed'] += row['status'] == 'confirmed'
        bucket['linked'] += bool(row['linked'])
    result['years'] = [grouped[year] for year in sorted(
        grouped, key=lambda value: (value is None, 0 if value is None else -value))]
    return result


def dismiss_document(store, data):
    """Keep a source and audit trail while removing a non-document from review."""
    if (not isinstance(data, dict) or set(data) != {'id', 'revision', 'not_invoice'}
            or type(data['id']) is not int or data['id'] < 1
            or type(data['revision']) is not int or data['revision'] < 1
            or data['not_invoice'] is not True):
        raise ValueError('invalid_document_dismissal')
    store.db.execute('BEGIN IMMEDIATE')
    try:
        previous = store.db.execute(
            'SELECT * FROM classification_documents WHERE id=?', (data['id'],)).fetchone()
        if previous is None:
            raise ValueError('unknown_document')
        if previous['revision'] != data['revision']:
            raise ValueError('stale_revision')
        if store.db.execute(
                'SELECT 1 FROM classification_document_links WHERE document_id=?',
                (data['id'],)).fetchone():
            raise ValueError('linked_document_cannot_be_dismissed')
        from .bonsy_vouchers import voucher_total
        if voucher_total(store, data['id']) > 0:
            raise ValueError('voucher_payment_document_cannot_be_dismissed')
        warnings = _warnings(json.loads(previous['warnings']))
        if 'not_invoice_like' not in warnings:
            warnings.append('not_invoice_like')
        revision = previous['revision'] + 1
        updated = store.db.execute(
            'UPDATE classification_documents SET warnings=?,revision=? WHERE id=? AND revision=?',
            (json.dumps(sorted(warnings)), revision, data['id'], data['revision']))
        if updated.rowcount != 1:
            raise ValueError('stale_revision')
        result = _document(store.db.execute(
            'SELECT * FROM classification_documents WHERE id=?', (data['id'],)).fetchone())
        _audit(store, 'document_marked_not_invoice', result,
               document_id=data['id'], previous=dict(previous))
        store.db.commit()
    except Exception:
        store.db.rollback()
        raise
    return {'document': result}


def mark_document_duplicate(store, data):
    """Explicitly hide a proven source duplicate, or restore only its marker.

    The caller verifies original-source equivalence. This operation performs no
    heuristic matching and never edits source files, links or bank transactions.
    The canonical document id is recorded in the audit, avoiding a schema change.
    """
    if (not isinstance(data, dict) or set(data) != {'id', 'revision', 'duplicate_of', 'confirmed'}
            or type(data['id']) is not int or data['id'] < 1
            or type(data['revision']) is not int or data['revision'] < 1
            or data['confirmed'] is not True
            or (data['duplicate_of'] is not None and (
                type(data['duplicate_of']) is not int or data['duplicate_of'] < 1
                or data['duplicate_of'] == data['id']))):
        raise ValueError('invalid_document_duplicate')
    store.db.execute('BEGIN IMMEDIATE')
    try:
        previous = store.db.execute(
            'SELECT * FROM classification_documents WHERE id=?', (data['id'],)).fetchone()
        if previous is None:
            raise ValueError('unknown_document')
        if previous['revision'] != data['revision']:
            raise ValueError('stale_revision')
        warnings = _warnings(json.loads(previous['warnings']))
        target_id = data['duplicate_of']
        if target_id is not None:
            from .bonsy_vouchers import voucher_total
            if voucher_total(store, data['id']) > 0:
                raise ValueError('voucher_payment_document_cannot_be_duplicate')
            target = store.db.execute(
                'SELECT * FROM classification_documents WHERE id=?', (target_id,)).fetchone()
            if target is None:
                raise ValueError('unknown_duplicate_target')
            if set(json.loads(target['warnings'])) & {
                    'source_excluded_bonsy', 'not_invoice_like', 'duplicate_source_document'}:
                raise ValueError('duplicate_target_excluded')
            if set(warnings) & {'source_excluded_bonsy', 'not_invoice_like'}:
                raise ValueError('duplicate_source_excluded')
            if store.db.execute('SELECT 1 FROM classification_document_links WHERE document_id=?',
                                (data['id'],)).fetchone():
                raise ValueError('linked_document_cannot_be_duplicate')
            if _bonsy_cash_allocated(store, data['id']):
                raise ValueError('bonsy_receipt_already_cash_allocated')
            references = store.db.execute(
                "SELECT a.current FROM classification_audit a "
                "JOIN classification_documents d ON d.id=a.document_id "
                "WHERE d.warnings LIKE ? AND a.id=(SELECT MAX(b.id) "
                "FROM classification_audit b WHERE b.document_id=a.document_id "
                "AND b.action IN ('document_marked_duplicate','document_duplicate_restored'))",
                ('%"duplicate_source_document"%',)).fetchall()
            if any(json.loads(row['current']).get('duplicate_of') == data['id'] for row in references):
                raise ValueError('duplicate_canonical_is_referenced')
        latest = store.db.execute(
            "SELECT current FROM classification_audit WHERE document_id=? "
            "AND action IN ('document_marked_duplicate','document_duplicate_restored') "
            'ORDER BY id DESC LIMIT 1', (data['id'],)).fetchone()
        last_target = None if latest is None else json.loads(latest['current']).get('duplicate_of')
        marked = 'duplicate_source_document' in warnings
        if (target_id is None and not marked) or (marked and target_id is not None and target_id == last_target):
            store.db.commit()
            return {'document': _document(previous), 'duplicate_of': target_id}
        if target_id is None:
            warnings.remove('duplicate_source_document')
        elif not marked:
            warnings.append('duplicate_source_document')
        updated = store.db.execute(
            'UPDATE classification_documents SET warnings=?,revision=revision+1 WHERE id=? AND revision=?',
            (json.dumps(sorted(warnings)), data['id'], data['revision']))
        if updated.rowcount != 1:
            raise ValueError('stale_revision')
        result = _document(store.db.execute(
            'SELECT * FROM classification_documents WHERE id=?', (data['id'],)).fetchone())
        _audit(store, 'document_duplicate_restored' if target_id is None else 'document_marked_duplicate',
               {**result, 'duplicate_of': target_id}, document_id=data['id'],
               previous={**dict(previous), 'duplicate_of': last_target})
        store.db.commit()
    except Exception:
        store.db.rollback()
        raise
    return {'document': result, 'duplicate_of': target_id}


def confirm_document(store, data):
    editable = {'vendor', 'title', 'document_date', 'amount', 'currency', 'source_reference', 'status'}
    if not isinstance(data, dict) or set(data) != {'id', 'revision', 'confirmed', *editable} or type(data['id']) is not int or type(data['revision']) is not int or data['confirmed'] is not True:
        raise ValueError('invalid_document_confirmation')
    store.db.execute('BEGIN IMMEDIATE')
    try:
        previous = store.db.execute('SELECT * FROM classification_documents WHERE id=?', (data['id'],)).fetchone()
        if previous is None:
            raise ValueError('unknown_document')
        if previous['revision'] != data['revision']:
            raise ValueError('stale_revision')
        if _source_reference(data['source_reference']) != previous['source_reference']:
            raise ValueError('source_reference_is_immutable')
        amount = _optional_amount(data['amount'])
        currency = None if data['currency'] is None else _text(data['currency'], 'currency', 3)
        if (amount is None) != (currency is None) or (currency is not None and currency != 'EUR'):
            raise ValueError('invalid_document_amount')
        status = data['status']
        if status != 'confirmed':
            raise ValueError('document_must_be_confirmed')
        if previous['kind'] == 'invoice' and (amount is None or money(amount) <= 0 or currency != 'EUR' or data['document_date'] is None):
            raise ValueError('confirmed_invoice_requires_amount_date_eur')
        linked = store.db.execute('SELECT 1 FROM classification_document_links WHERE document_id=?', (data['id'],)).fetchone()
        if previous['kind'] == 'invoice' and linked and (amount != previous['amount'] or _optional_date(data['document_date'], 'document_date') != previous['document_date']):
            raise ValueError('unlink_invoice_before_money_or_date_edit')
        if (previous['kind'] == 'invoice'
                and (amount != previous['amount'] or _optional_date(data['document_date'], 'document_date') != previous['document_date'])):
            from .bonsy_vouchers import voucher_total
            if voucher_total(store, data['id']) > 0:
                raise ValueError('voucher_payment_prevents_invoice_edit')
        values = (_text(data['vendor'], 'vendor'), _text(data['title'], 'title'), _optional_date(data['document_date'], 'document_date'), amount,
                  currency, _source_reference(data['source_reference']), status, previous['revision'] + 1, data['id'])
        store.db.execute('UPDATE classification_documents SET vendor=?,title=?,document_date=?,amount=?,currency=?,source_reference=?,status=?,revision=? WHERE id=?', values)
        result = _document(store.db.execute('SELECT * FROM classification_documents WHERE id=?', (data['id'],)).fetchone())
        _audit(store, 'document_confirmed', result, document_id=data['id'], previous=dict(previous))
        store.db.commit()
    except Exception:
        store.db.rollback()
        raise
    return {'document': result}


def auto_confirm_documents(store, data):
    """Confirm only conservative invoice candidates, with an audit trail.

    ``confirmed=True`` is an explicit API acknowledgement.  ``ids`` limits the
    operation to a caller supplied import batch; omitted ids mean all current
    unreviewed invoices and are intended for the review endpoint.
    """
    if not isinstance(data, dict) or set(data) - {'confirmed', 'ids'} or data.get('confirmed') is not True:
        raise ValueError('auto_document_review_confirmation_required')
    ids = data.get('ids')
    if ids is not None:
        if not isinstance(ids, list) or any(type(value) is not int or value < 1 for value in ids):
            raise ValueError('invalid_auto_document_ids')
        ids = sorted(set(ids))
    query = ("SELECT * FROM classification_documents WHERE kind='invoice' AND status='unreviewed' "
             'AND warnings NOT LIKE ? AND warnings NOT LIKE ? AND (document_date IS NULL OR document_date>=?)')
    params = ['%"source_excluded_bonsy"%', '%"duplicate_source_document"%', DOCUMENT_REVIEW_START_DATE]
    if ids is not None:
        if not ids:
            return {'confirmed': [], 'rejected': [], 'checked': 0}
        query += ' AND id IN (' + ','.join('?' for _ in ids) + ')'
        params.extend(ids)
    rows = store.db.execute(query + ' ORDER BY id', params).fetchall()
    confirmed, rejected = [], []
    store.db.execute('BEGIN IMMEDIATE')
    try:
        for row in rows:
            reasons = []
            vendor, title = row['vendor'].strip(), row['title'].strip()
            if row['warnings'] != '[]':
                reasons.append('warnings_present')
            if row['document_date'] is None or row['document_date'] < DOCUMENT_REVIEW_START_DATE:
                reasons.append('date_missing_or_before_2026')
            if row['currency'] != 'EUR' or row['amount'] is None or money(row['amount']) <= 0:
                reasons.append('positive_eur_amount_required')
            for value, field in ((vendor, 'vendor'), (title, 'title')):
                if value.casefold() in _AUTO_REVIEW_GENERIC:
                    reasons.append(f'generic_{field}')
                if _AUTO_REVIEW_URL.search(value):
                    reasons.append(f'url_in_{field}')
                if _AUTO_REVIEW_IMAGE.search(value):
                    reasons.append(f'image_marker_in_{field}')
                if _AUTO_REVIEW_SALUTATION.search(value):
                    reasons.append(f'salutation_in_{field}')
            if _AUTO_REVIEW_PLACEHOLDER.search(vendor):
                reasons.append('placeholder_vendor')
            # Bankverbindungen stehen oft vor dem eigentlichen Rechnungssteller
            # im extrahierten Text. Ohne Prüfung des Quelldokuments ist eine
            # automatische Bestätigung deshalb nicht belastbar.
            if _AUTO_REVIEW_BANK_VENDOR.search(vendor):
                reasons.append('bank_vendor_needs_source_review')
            duplicate = store.db.execute(
                'SELECT 1 FROM classification_documents WHERE id!=? '
                'AND lower(trim(vendor))=lower(trim(?)) AND lower(trim(title))=lower(trim(?)) '
                'AND document_date=? AND amount=? '
                'AND warnings NOT LIKE \'%"duplicate_source_document"%\' LIMIT 1',
                (row['id'], vendor, title, row['document_date'], row['amount'])).fetchone()
            if duplicate:
                reasons.append('possible_duplicate_document')
            if reasons:
                rejected.append({'id': row['id'], 'reasons': sorted(set(reasons))})
                current = _document(row)
                serialized = json.dumps(current, sort_keys=True)
                if not store.db.execute(
                        "SELECT 1 FROM classification_audit WHERE action='document_auto_review_rejected' "
                        'AND document_id=? AND current=?', (row['id'], serialized)).fetchone():
                    _audit(store, 'document_auto_review_rejected', current, document_id=row['id'])
                continue
            updated = store.db.execute(
                "UPDATE classification_documents SET status='confirmed', revision=revision+1 WHERE id=? AND revision=? AND status='unreviewed'",
                (row['id'], row['revision']))
            if updated.rowcount != 1:
                raise ValueError('stale_revision')
            result = _document(store.db.execute('SELECT * FROM classification_documents WHERE id=?', (row['id'],)).fetchone())
            confirmed.append(result)
            _audit(store, 'document_auto_confirmed', result, document_id=row['id'], previous=dict(row))
        store.db.commit()
    except Exception:
        store.db.rollback()
        raise
    return {'confirmed': confirmed, 'rejected': rejected, 'checked': len(rows)}


def auto_link_documents(store, data):
    """Link only unique, symmetric invoice/payment matches with a short date gap."""
    if not isinstance(data, dict) or set(data) - {'confirmed', 'ids'} or data.get('confirmed') is not True:
        raise ValueError('auto_document_link_confirmation_required')
    ids = data.get('ids')
    if ids is not None:
        if not isinstance(ids, list) or any(type(value) is not int or value < 1 for value in ids):
            raise ValueError('invalid_auto_document_link_ids')
        ids = sorted(set(ids))
    query = ("SELECT id,document_date FROM classification_documents WHERE kind='invoice' "
             "AND status='confirmed' AND warnings NOT LIKE ? AND document_date>=? AND NOT EXISTS ("
             'SELECT 1 FROM classification_document_links l WHERE l.document_id=classification_documents.id) '
             'AND NOT EXISTS (SELECT 1 FROM bonsy_receipts r JOIN bonsy_voucher_payments v USING(entry_id) '
             'WHERE r.document_id=classification_documents.id)')
    params = ['%"duplicate_source_document"%', DOCUMENT_REVIEW_START_DATE]
    if ids is not None:
        if not ids:
            return {'linked': [], 'rejected': [], 'checked': 0}
        query += ' AND id IN (' + ','.join('?' for _ in ids) + ')'
        params.extend(ids)
    rows = store.db.execute(query + ' ORDER BY id', params).fetchall()
    linked, rejected = [], []
    for row in rows:
        matches = document_match_suggestions(
            store, {'document_id': row['id'], 'max_days': 45, 'page': 0})
        reasons = []
        if matches['suggestion_groups']:
            reasons.append('group_match_requires_manual_review')
        exact_matches = [candidate for candidate in matches['suggestions']
                         if candidate['reason'] == 'exact_remaining_amount_date_and_recipient']
        if matches['total'] != 1 or len(exact_matches) != 1:
            reasons.append('unique_document_match_required')
        else:
            candidate = exact_matches[0]
            if _cash_withdrawal_evidence(
                    store, candidate['account_id'], candidate['external_id']):
                reasons.append('cash_withdrawal_requires_manual_review')
            day_gap = (date.fromisoformat(candidate['date'])
                       - date.fromisoformat(row['document_date'])).days
            if not 0 <= day_gap <= 7:
                reasons.append('payment_within_seven_days_required')
            inverse = match_suggestions(store, {
                'account_id': candidate['account_id'], 'external_id': candidate['external_id'],
                'max_days': 45,
            })
            if inverse['suggestion_groups']:
                reasons.append('group_match_requires_manual_review')
            if (len(inverse['suggestions']) != 1
                    or inverse['suggestions'][0]['document_id'] != row['id']):
                reasons.append('unique_transaction_match_required')
        if reasons:
            rejected.append({'id': row['id'], 'reasons': sorted(set(reasons))})
            continue
        result = link_document(store, {
            'account_id': candidate['account_id'], 'external_id': candidate['external_id'],
            'document_id': row['id'], 'confirmed': True,
        })['link']
        _audit(store, 'document_auto_linked', result, candidate['account_id'],
               candidate['external_id'], row['id'])
        store.db.commit()
        linked.append(result)
    return {'linked': linked, 'rejected': rejected, 'checked': len(rows)}


def reject_document_link(store, data):
    """Persist one explicit negative pair decision so it is not proposed again."""
    required = {'account_id', 'external_id', 'document_id', 'rejected'}
    if (not isinstance(data, dict) or set(data) != required or data['rejected'] is not True
            or type(data['document_id']) is not int or data['document_id'] < 1):
        raise ValueError('explicit_link_rejection_required')
    account_id = _text(data['account_id'], 'account_id', 120)
    external_id = _text(data['external_id'], 'external_id', 240)
    _transaction(store, account_id, external_id)
    if store.db.execute('SELECT 1 FROM classification_documents WHERE id=?',
                        (data['document_id'],)).fetchone() is None:
        raise ValueError('unknown_document')
    if store.db.execute(
            'SELECT 1 FROM classification_document_links WHERE account_id=? AND external_id=? AND document_id=?',
            (account_id, external_id, data['document_id'])).fetchone():
        raise ValueError('document_link_already_exists')
    result = {'account_id': account_id, 'external_id': external_id,
              'document_id': data['document_id'], 'rejected': True}
    if not _document_link_rejected(store, account_id, external_id, data['document_id']):
        _audit(store, 'document_link_rejected', result, account_id, external_id, data['document_id'])
        store.db.commit()
    return {'rejection': result}


def _bonsy_cash_allocated(store, document_id):
    """Preserve legacy cash allocations that predate the no-inference policy."""
    return store.db.execute(
        'SELECT 1 FROM bonsy_receipts r JOIN bonsy_cash_allocations c USING(entry_id) '
        'WHERE r.document_id=?', (document_id,)).fetchone() is not None


def _require_not_duplicate_document(document):
    if 'duplicate_source_document' in json.loads(document['warnings']):
        raise ValueError('duplicate_document_cannot_be_linked')


def confirm_and_link_document(store, data):
    """Confirm one invoice and its exact debit link in one SQLite transaction."""
    editable = {'vendor', 'title', 'document_date', 'amount', 'currency', 'source_reference', 'status'}
    required = {'account_id', 'external_id', 'id', 'revision', 'confirmed', *editable}
    if (not isinstance(data, dict) or set(data) != required or type(data['id']) is not int
            or type(data['revision']) is not int or data['confirmed'] is not True):
        raise ValueError('invalid_document_confirmation_link')
    account_id = _text(data['account_id'], 'account_id', 120)
    external_id = _text(data['external_id'], 'external_id', 240)
    store.db.execute('BEGIN IMMEDIATE')
    try:
        previous = store.db.execute('SELECT * FROM classification_documents WHERE id=?', (data['id'],)).fetchone()
        if previous is None:
            raise ValueError('unknown_document')
        if previous['revision'] != data['revision']:
            raise ValueError('stale_revision')
        if previous['kind'] != 'invoice':
            raise ValueError('confirmed_eur_invoice_required')
        _require_not_duplicate_document(previous)
        if _source_reference(data['source_reference']) != previous['source_reference']:
            raise ValueError('source_reference_is_immutable')
        amount = _optional_amount(data['amount'])
        currency = None if data['currency'] is None else _text(data['currency'], 'currency', 3)
        if ((amount is None) != (currency is None) or currency != 'EUR'
                or money(amount) <= 0 or data['document_date'] is None or data['status'] != 'confirmed'):
            raise ValueError('confirmed_invoice_requires_amount_date_eur')
        transaction = _transaction(store, account_id, external_id)
        if effective_transfer_id(store, transaction):
            raise ValueError('transfer_document_link_forbidden')
        if (transaction['currency'] != 'EUR' or money(transaction['amount']) >= 0
                or -money(transaction['amount']) != money(amount)):
            raise ValueError('invoice_amount_must_match_debit')
        from .bonsy_vouchers import voucher_total
        if voucher_total(store, data['id']) > 0:
            raise ValueError('voucher_payment_prevents_full_invoice_link')
        if store.db.execute('SELECT 1 FROM classification_document_links WHERE document_id=?', (data['id'],)).fetchone():
            raise ValueError('invoice_already_linked')
        if _bonsy_cash_allocated(store, data['id']):
            raise ValueError('bonsy_receipt_already_cash_allocated')
        if store.db.execute(
                "SELECT 1 FROM classification_document_links l JOIN classification_documents d ON d.id=l.document_id "
                "WHERE l.account_id=? AND l.external_id=? AND d.kind='invoice'", (account_id, external_id)).fetchone():
            raise ValueError('transaction_already_has_invoice')
        values = (_text(data['vendor'], 'vendor'), _text(data['title'], 'title'),
                  _optional_date(data['document_date'], 'document_date'), amount, currency,
                  _source_reference(data['source_reference']), 'confirmed', previous['revision'] + 1, data['id'])
        store.db.execute('UPDATE classification_documents SET vendor=?,title=?,document_date=?,amount=?,currency=?,source_reference=?,status=?,revision=? WHERE id=?', values)
        document = _document(store.db.execute('SELECT * FROM classification_documents WHERE id=?', (data['id'],)).fetchone())
        _audit(store, 'document_confirmed', document, document_id=data['id'], previous=dict(previous))
        link = {'account_id': account_id, 'external_id': external_id, 'document_id': data['id'],
                'allocated_amount': format(abs(money(transaction['amount'])), '.2f'), 'allocation_type': 'payment'}
        store.db.execute('INSERT INTO classification_document_links(account_id,external_id,document_id,allocated_amount,allocation_type) VALUES (?,?,?,?,?)',
                         (*link.values(),))
        _audit(store, 'document_linked', link, account_id, external_id, data['id'])
        store.db.commit()
    except Exception:
        store.db.rollback()
        raise
    return {'document': document, 'link': link}


def allocate_document(store, data):
    required = {'account_id', 'external_id', 'document_id', 'allocated_amount', 'allocation_type', 'confirmed'}
    if (not isinstance(data, dict) or set(data) != required or type(data['document_id']) is not int
            or data['confirmed'] is not True or data['allocation_type'] not in {'payment', 'refund'}):
        raise ValueError('explicit_allocation_confirmation_required')
    account_id = _text(data['account_id'], 'account_id', 120)
    external_id = _text(data['external_id'], 'external_id', 240)
    allocated_amount = _allocated_amount(data['allocated_amount'])
    store.db.execute('BEGIN IMMEDIATE')
    try:
        transaction = _transaction(store, account_id, external_id)
        if effective_transfer_id(store, transaction):
            raise ValueError('transfer_document_link_forbidden')
        if transaction['currency'] != 'EUR':
            raise ValueError('eur_transaction_required')
        amount = money(transaction['amount'])
        if amount == 0:
            raise ValueError('allocation_transaction_amount_invalid')
        expected_type = 'payment' if amount < 0 else 'refund'
        if data['allocation_type'] != expected_type:
            raise ValueError('allocation_direction_mismatch')
        document = store.db.execute('SELECT * FROM classification_documents WHERE id=?', (data['document_id'],)).fetchone()
        if (document is None or document['kind'] != 'invoice' or document['status'] != 'confirmed'
                or document['currency'] != 'EUR' or document['amount'] is None or money(document['amount']) <= 0):
            raise ValueError('confirmed_eur_invoice_required')
        _require_not_duplicate_document(document)
        if _bonsy_cash_allocated(store, document['id']):
            raise ValueError('bonsy_receipt_already_cash_allocated')
        if store.db.execute(
                'SELECT 1 FROM classification_document_links '
                'WHERE account_id=? AND external_id=? AND document_id=?',
                (account_id, external_id, document['id'])).fetchone():
            raise ValueError('document_allocation_already_exists')
        used_transaction = sum((money(row['allocated_amount']) for row in store.db.execute(
            'SELECT allocated_amount FROM classification_document_links WHERE account_id=? AND external_id=?',
            (account_id, external_id))), Decimal(0))
        if used_transaction + money(allocated_amount) > abs(amount):
            raise ValueError('transaction_allocation_exceeds_amount')
        used_document = sum((money(row['allocated_amount']) for row in store.db.execute(
            'SELECT allocated_amount FROM classification_document_links WHERE document_id=? AND allocation_type=?',
            (document['id'], data['allocation_type']))), Decimal(0))
        from .bonsy_vouchers import voucher_total
        voucher_amount = voucher_total(store, document['id']) if data['allocation_type'] == 'payment' else Decimal(0)
        if used_document + money(allocated_amount) + voucher_amount > money(document['amount']):
            raise ValueError('invoice_allocation_exceeds_amount')
        result = {'account_id': account_id, 'external_id': external_id, 'document_id': document['id'],
                  'allocated_amount': allocated_amount, 'allocation_type': data['allocation_type']}
        store.db.execute('INSERT INTO classification_document_links(account_id,external_id,document_id,allocated_amount,allocation_type) VALUES (?,?,?,?,?)',
                         (*result.values(),))
        _audit(store, 'document_allocated', result, account_id, external_id, document['id'])
        store.db.commit()
    except Exception:
        store.db.rollback()
        raise
    return {'link': result}


def link_document(store, data):
    if not isinstance(data, dict) or set(data) != {'account_id', 'external_id', 'document_id', 'confirmed'} or type(data['document_id']) is not int or data['confirmed'] is not True:
        raise ValueError('explicit_link_confirmation_required')
    account_id, external_id = _text(data['account_id'], 'account_id', 120), _text(data['external_id'], 'external_id', 240)
    store.db.execute('BEGIN IMMEDIATE')
    try:
        transaction = _transaction(store, account_id, external_id)
        document = store.db.execute('SELECT * FROM classification_documents WHERE id=?', (data['document_id'],)).fetchone()
        if document is None:
            raise ValueError('unknown_document')
        _require_not_duplicate_document(document)
        if _bonsy_cash_allocated(store, document['id']):
            raise ValueError('bonsy_receipt_already_cash_allocated')
        is_transfer = bool(effective_transfer_id(store, transaction))
        if is_transfer and document['kind'] != 'contract':
            raise ValueError('transfer_document_link_forbidden')
        if document['kind'] == 'contract' and document['status'] != 'confirmed':
            raise ValueError('confirmed_contract_required')
        if document['kind'] == 'invoice':
            if document['status'] != 'confirmed' or document['currency'] != 'EUR' or document['amount'] is None:
                raise ValueError('confirmed_eur_invoice_required')
            if money(transaction['amount']) >= 0 or -money(transaction['amount']) != money(document['amount']):
                raise ValueError('invoice_amount_must_match_debit')
            from .bonsy_vouchers import voucher_total
            if voucher_total(store, document['id']) > 0:
                raise ValueError('voucher_payment_prevents_full_invoice_link')
            if store.db.execute('SELECT 1 FROM classification_document_links WHERE document_id=?', (document['id'],)).fetchone():
                raise ValueError('invoice_already_linked')
            if store.db.execute('SELECT 1 FROM classification_document_links l JOIN classification_documents d ON d.id=l.document_id WHERE l.account_id=? AND l.external_id=? AND d.kind=\'invoice\'', (account_id, external_id)).fetchone():
                raise ValueError('transaction_already_has_invoice')
        allocated_amount = format(abs(money(transaction['amount'])), '.2f')
        # A confirmed contract may explain a wallet funding transfer without
        # turning that transfer into a second household expense.
        allocation_type = 'evidence' if is_transfer else 'payment'
        store.db.execute('INSERT OR IGNORE INTO classification_document_links(account_id,external_id,document_id,allocated_amount,allocation_type) VALUES (?,?,?,?,?)',
                         (account_id, external_id, document['id'], allocated_amount, allocation_type))
        result = {'account_id': account_id, 'external_id': external_id, 'document_id': document['id'],
                  'allocated_amount': allocated_amount, 'allocation_type': allocation_type}
        _audit(store, 'document_linked', result, account_id, external_id, document['id'])
        store.db.commit()
    except Exception:
        store.db.rollback()
        raise
    return {'link': result}


def unlink_document(store, data):
    if not isinstance(data, dict) or set(data) != {'account_id', 'external_id', 'document_id', 'confirmed'} or type(data['document_id']) is not int or data['confirmed'] is not True:
        raise ValueError('explicit_unlink_confirmation_required')
    account_id, external_id = _text(data['account_id'], 'account_id', 120), _text(data['external_id'], 'external_id', 240)
    store.db.execute('BEGIN IMMEDIATE')
    try:
        if store.db.execute('DELETE FROM classification_document_links WHERE account_id=? AND external_id=? AND document_id=?', (account_id, external_id, data['document_id'])).rowcount != 1:
            raise ValueError('unknown_document_link')
        result = {'account_id': account_id, 'external_id': external_id, 'document_id': data['document_id']}
        _audit(store, 'document_unlinked', result, account_id, external_id, data['document_id'])
        store.db.commit()
    except Exception:
        store.db.rollback()
        raise
    return {'unlinked': True}


def aggregates(store, data=None):
    data = {} if data is None else data
    base, where, params, period = _transaction_scope(
        data, _TRANSACTION_FILTERS, 'invalid_aggregate_request')
    catalog = {row['id']: dict(row) for row in store.db.execute('SELECT * FROM category_catalog')}
    links = {(row['account_id'], row['external_id']) for row in store.db.execute('SELECT DISTINCT account_id,external_id FROM classification_document_links')}
    grouped, counts = {}, {'total': 0, 'reviewed': 0, 'unreviewed': 0, 'linked': 0}
    counts['payment_status_open'] = store.db.execute(
        "SELECT count(*) FROM payment_mail_events WHERE match_status!='unique'"
    ).fetchone()[0]
    query = ('SELECT t.account_id,t.external_id,t.amount,t.category,t.transfer_id,'
             'c.description,o.category_id,o.confirmed '
             + base + where + ' ORDER BY t.account_id,t.external_id')
    for row in store.db.execute(query, params):
        direction, amount = _direction(store, row), money(row['amount'])
        confirmed = direction == 'transfer' or row['confirmed'] == 1
        category = 'TRANSFER' if direction == 'transfer' else (row['category_id'] if confirmed else {
            'income': 'EINNAHMEN_UNKLAR', 'expense': 'AUSGABEN_UNKLAR'}[direction])
        bucket = grouped.setdefault(category, {'category': category, 'label': catalog[category]['label'],
                                                'transaction_type': catalog[category]['transaction_type'],
                                                'count': 0, 'income': money('0'), 'outflow': money('0'),
                                                'transfer': money('0'), 'net': money('0')})
        bucket['count'] += 1
        bucket['net'] += amount
        bucket[direction if direction != 'expense' else 'outflow'] += -amount if direction == 'expense' else amount
        counts['total'] += 1
        counts['reviewed' if confirmed else 'unreviewed'] += 1
        if (row['account_id'], row['external_id']) in links:
            counts['linked'] += 1
    categories = []
    for category in sorted(grouped):
        bucket = grouped[category]
        categories.append({key: format(value, '.2f') if key in {'income', 'outflow', 'transfer', 'net'} else value
                           for key, value in bucket.items()})
    total = {key: money('0') for key in ('income', 'outflow', 'transfer', 'net')}
    for bucket in categories:
        for key in total:
            total[key] += Decimal(bucket[key])
    return {'categories': categories,
            'totals': {key: format(value, '.2f') for key, value in total.items()} | counts,
            'unconfirmed': {'count': counts['unreviewed']},
            'period': period}
