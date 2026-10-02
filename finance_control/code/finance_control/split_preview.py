"""Reconcile exported split groups without choosing a ledger representation."""
from collections import defaultdict
from decimal import Decimal
from .core import money


def inspect_split_groups(rows):
    groups, originals = defaultdict(list), defaultdict(list)
    unknown = 0
    for row in rows:
        kind = row.get('Split-Typ')
        if not kind:
            continue
        account = row.get('Referenzkonto')
        if kind == 'Original':
            originals[(account, row.get('Buchungs-ID'))].append(row)
        elif kind in {'Teilbuchung', 'Restbetrag'}:
            groups[(account, row.get('Referenz-Original-ID'))].append(row)
        else:
            unknown += 1
    result = {'groups': 0, 'balanced': 0, 'unbalanced': 0,
              'missing_or_ambiguous_original': 0, 'invalid_groups': 0,
              'unknown_split_rows': unknown, 'ledger_written': False}
    for key in originals.keys() | groups.keys():
        result['groups'] += 1
        parents, children = originals[key], groups[key]
        if not key[0] or not key[1] or len(parents) != 1 or not children:
            result['missing_or_ambiguous_original'] += 1
            continue
        try:
            ids = [child.get('Buchungs-ID') for child in children]
            currency = parents[0].get('Waehrung')
            if (currency not in {'EUR', '€'} or any(c.get('Waehrung') not in {'EUR', '€'} for c in children)
                    or not all(ids) or len(ids) != len(set(ids)) or key[1] in ids):
                raise ValueError('Invalid split identity or currency')
            expected = money(str(parents[0]['Betrag']))
            actual = sum((money(str(c['Betrag'])) for c in children), Decimal(0))
            result['balanced' if actual == expected else 'unbalanced'] += 1
        except (ValueError, TypeError, KeyError, ArithmeticError):
            result['invalid_groups'] += 1
    return result
