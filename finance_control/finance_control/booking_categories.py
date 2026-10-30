"""Read-only resolution of a transaction's display category and suggestions."""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation

from .classification_rules import is_intermediary, rule_is_eligible


_LEADING_DE_IBAN = re.compile(r'^\s*(DE(?:[ \t\u00a0]?\d){20})', re.IGNORECASE)
_TECHNICAL_CATEGORY_PREFIXES = (
    'bankabruf', 'bank abruf', 'kontoabruf', 'konto abruf', 'fints',
    'onlinebanking', 'online banking', 'online-banking', 'kreditkartenabruf',
    'kartenabruf', 'bankimport', 'bank import', 'finanzabruf',
)
# Keep aligned with classification._GENERIC_COUNTERPARTIES; history is too weak
# to turn these non-identifying source labels into a category suggestion.
_GENERIC_COUNTERPARTIES = frozenset({
    'abbuchung', 'belastung', 'buchung', 'kartenzahlung', 'kreditkartenumsatz',
    'lastschrift', 'sepa-lastschrift', 'sonstige',
})
_MISSING = object()


def _field(row, name, default=None):
    try:
        return row[name]
    except (KeyError, IndexError, TypeError):
        return default


def _valid_de_iban(value):
    compact = re.sub(r'\s', '', value).upper()
    if re.fullmatch(r'DE[0-9]{20}', compact) is None:
        return False
    rearranged = compact[4:] + compact[:4]
    remainder = 0
    for character in rearranged:
        digits = str(ord(character) - ord('A') + 10) if character.isalpha() else character
        for digit in digits:
            remainder = (remainder * 10 + int(digit)) % 97
    return remainder == 1


def _canonical_counterparty(value):
    if not isinstance(value, str):
        return ''
    text = ' '.join(value.split())
    match = _LEADING_DE_IBAN.match(text)
    if match and _valid_de_iban(match.group(1)):
        text = text[match.end():].lstrip(' \t\u00a0,;|')
    return ' '.join(text.casefold().split())


def _is_technical_category(value):
    if not isinstance(value, str):
        return False
    normalized = ' '.join(value.casefold().split())
    return any(normalized.startswith(prefix) for prefix in _TECHNICAL_CATEGORY_PREFIXES)


def _direction(row):
    if _field(row, 'is_transfer') in (True, 1) or _field(row, 'direction') == 'transfer':
        return 'transfer'
    if _field(row, 'transfer_id'):
        return 'transfer'
    direction = _field(row, 'direction')
    if direction in {'income', 'expense'}:
        return direction
    try:
        amount = Decimal(str(_field(row, 'amount')))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return 'income' if amount > 0 else 'expense' if amount < 0 else None


class CategoryResolver:
    """Resolve stored choices and return suggestions without writing anything."""

    def __init__(self, store, *, lazy_history=False):
        """Keep the eager snapshot unless a request opts into lazy history.

        Lazy readers remain within this store's lifetime. The fallback is
        loaded once per resolver, never cached across requests or edits.
        """
        self._store = store
        self._catalog = {
            row['id']: dict(row) for row in store.db.execute(
                'SELECT id,label,transaction_type FROM category_catalog')
        }
        self._aliases = {
            row['alias_id']: row['canonical_id'] for row in store.db.execute(
                'SELECT alias_id,canonical_id FROM category_aliases')
        }
        self._rules = defaultdict(list)
        for row in store.db.execute(
                'SELECT * FROM classification_rules WHERE enabled=1'):
            party = _canonical_counterparty(row['counterparty_normalized'])
            self._rules[(party, row['direction'])].append(dict(row))
        self._overrides = {
            (row['account_id'], row['external_id']): dict(row)
            for row in store.db.execute(
                'SELECT account_id,external_id,category_id,confirmed,revision '
                'FROM classification_overrides')
        }
        self._models = {
            (row['account_id'], row['external_id']): row['category_id']
            for row in store.db.execute(
                """SELECT m.account_id,m.external_id,m.category_id
                   FROM classification_model_reviews m
                   JOIN category_catalog cat ON cat.id=m.category_id
                   WHERE m.status='proposed'
                     AND NOT EXISTS (
                       SELECT 1 FROM classification_model_rejections r
                       WHERE r.account_id=m.account_id AND r.external_id=m.external_id
                         AND r.category_id=m.category_id)""")
        }
        self._history = None if lazy_history else self._load_history(store)

    def _canonical_category_id(self, category_id):
        return self._aliases.get(category_id, category_id)

    def _category_result(self, category_id, status, source):
        category_id = self._canonical_category_id(category_id)
        category = self._catalog.get(category_id)
        if category is None:
            return self._open_result()
        return {
            'category': category['label'], 'category_id': category_id,
            'category_status': status, 'category_source': source,
        }

    @staticmethod
    def _open_result():
        return {'category': 'Kategorie offen', 'category_id': None,
                'category_status': 'open', 'category_source': 'none'}

    def _load_history(self, store):
        from .transfer_corrections import sql_transfer_predicate

        counts = defaultdict(Counter)
        transfer_predicate = sql_transfer_predicate(context_alias='c')
        rows = store.db.execute(
            f"""SELECT t.account_id,t.external_id,t.amount,t.transfer_id,
                      c.counterparty,o.category_id,cat.transaction_type
               FROM classification_overrides o
               JOIN active_transactions t USING(account_id,external_id)
               LEFT JOIN transaction_context c USING(account_id,external_id)
               JOIN category_catalog cat ON cat.id=o.category_id
               WHERE o.confirmed=1 AND o.category_id IS NOT NULL
                 AND NOT COALESCE({transfer_predicate},0)
                 AND cat.transaction_type IN ('income','expense')""")
        for row in rows:
            party = _canonical_counterparty(row['counterparty'])
            if not party:
                continue
            direction = _direction(row)
            if direction not in {'income', 'expense'} or row['transaction_type'] != direction:
                continue
            category_id = self._canonical_category_id(row['category_id'])
            if category_id != 'AUSGABEN_BARGELD':
                counts[(party, direction)][category_id] += 1
        return counts

    def resolve(self, row):
        """Return display category, stable ID, status, and provenance for a row."""
        if row is None:
            return self._open_result()

        key = (_field(row, 'account_id'), _field(row, 'external_id'))
        row_override_flag = _field(row, 'override_present', _MISSING)
        if row_override_flag is _MISSING:
            override = self._overrides.get(key)
        elif row_override_flag:
            override = {
                'category_id': _field(row, 'category_id'),
                'confirmed': bool(_field(row, 'confirmed')),
            }
        else:
            override = None
        direction = _direction(row)

        if override is not None:
            category_id = override['category_id']
            if override['confirmed'] and category_id is not None:
                return self._category_result(category_id, 'confirmed', 'override')
            if category_id is not None:
                return self._category_result(category_id, 'proposal', 'override')
            # A reset/rejection anchor records an explicit open decision.
            return self._open_result()

        source_category = _field(row, 'source_category', _field(row, 'category'))
        if source_category and not _is_technical_category(source_category):
            return {'category': source_category, 'category_id': None,
                    'category_status': 'imported', 'category_source': 'imported'}

        if direction not in {'income', 'expense'}:
            return self._open_result()
        party = _canonical_counterparty(_field(row, 'counterparty'))

        rule_categories = {
            rule['category_id'] for rule in self._rules.get((party, direction), [])
            if rule_is_eligible(rule, _field(row, 'date'))
        } if party else set()
        if len(rule_categories) > 1:
            return self._open_result()
        if rule_categories:
            category_id = self._canonical_category_id(next(iter(rule_categories)))
            category = self._catalog.get(category_id)
            if (category_id != 'AUSGABEN_BARGELD' and category is not None
                    and category['transaction_type'] == direction):
                return self._category_result(category_id, 'proposal', 'rule')
            return self._open_result()

        model_category = self._models.get(key)
        if model_category is not None:
            category_id = self._canonical_category_id(model_category)
            category = self._catalog.get(category_id)
            if (category_id != 'AUSGABEN_BARGELD' and category is not None
                    and category['transaction_type'] == direction):
                return self._category_result(category_id, 'proposal', 'model')

        if (not party or party in _GENERIC_COUNTERPARTIES
                or is_intermediary(party, _field(row, 'description', ''))):
            return self._open_result()

        if self._history is None:
            self._history = self._load_history(self._store)
        history = self._history.get((party, direction), Counter())
        if len(history) > 1:
            return self._open_result()
        if history:
            category_id, count = next(iter(history.items()))
            if count >= 2:
                return self._category_result(category_id, 'proposal', 'history')
        return self._open_result()
