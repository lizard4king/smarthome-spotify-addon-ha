"""Shared, read-only notices for transfers that sums cannot prove.

Two situations make household sums unreliable without being provably wrong:

* A credit-card statement debit on a checking account and the card's single
  postings are all ordinary postings (no confirmed transfer pair).  Both are
  then counted as spending.
* A posting whose *source category* says "Umbuchung" is neutral even though no
  counter-posting, confirmed correction pair or confirmed single leg exists.

Neither case changes any sum.  Callers attach these notices next to their
totals so the situation is visible and separately reportable, never silent.
All rules are evidence based (exact amounts, explicit source category), never
guesses from merchant names.
"""
import re
from datetime import date, timedelta
from decimal import Decimal
from itertools import combinations

from .core import money
from .transfer_corrections import effective_transfer_id, source_transfer_without_counterpart

_ITEM_LIMIT = 50
_CARD_MARKER = re.compile(
    r'\b(?:kredit-?karte\w*|karten-?abrechnung\w*|credit ?card|visa|master-?card|amex|'
    r'american express)\b')


def card_linked_transfer_ids(store, transfer_ids):
    """Return those imported transfer IDs that have a credit-card leg.

    This is the single rule for "card settlement" used by Plan/Ist counts and
    by the notices below.
    """
    transfer_ids = {value for value in transfer_ids if value}
    if not transfer_ids:
        return set()
    placeholders = ','.join('?' for _ in transfer_ids)
    return {row['transfer_id'] for row in store.db.execute(
        'SELECT DISTINCT t.transfer_id FROM active_transactions t '
        "JOIN accounts a ON a.id=t.account_id AND a.kind='CREDIT_CARD' "
        f'WHERE t.transfer_id IN ({placeholders})', tuple(transfer_ids))}


def correction_pair_has_card_sql(alias='m'):
    """SQL expression: the confirmed correction pair contains a credit-card leg."""
    return ('EXISTS(SELECT 1 FROM transfer_correction_members cm '
            "JOIN accounts ca ON ca.id=cm.account_id AND ca.kind='CREDIT_CARD' "
            f'WHERE cm.pair_id={alias}.pair_id)')


def _rows(store, start, end, account_ids):
    query = (
        "SELECT t.account_id,t.external_id,t.date,t.amount,t.currency,t.category,"
        "t.transfer_id,coalesce(c.description,'') AS description,"
        "coalesce(c.counterparty,'') AS counterparty,a.kind AS account_kind,"
        "coalesce(nullif(trim(a.display_name),''),a.id) AS account_label "
        'FROM active_transactions t JOIN accounts a ON a.id=t.account_id '
        'LEFT JOIN transaction_context c ON c.account_id=t.account_id '
        'AND c.external_id=t.external_id '
        "WHERE t.currency='EUR' AND t.date BETWEEN ? AND ?")
    params = [start, end]
    if account_ids is not None:
        if not account_ids:
            return []
        query += f" AND t.account_id IN ({','.join('?' for _ in account_ids)})"
        params.extend(sorted(account_ids))
    return store.db.execute(query + ' ORDER BY t.date,t.account_id,t.external_id', params).fetchall()


def _text(value):
    return format(value, '.2f')


def _previous_month(month):
    year, number = int(month[:4]), int(month[5:7])
    return f'{year - (number == 1):04d}-{(number - 2) % 12 + 1:02d}'


def _next_month(month):
    year, number = int(month[:4]), int(month[5:7])
    return f'{year + (number == 12):04d}-{number % 12 + 1:02d}'


def _eur(value):
    """German money text for reasons, e.g. 1.234,50 EUR."""
    text = format(abs(value), ',.2f').replace(',', 'X').replace('.', ',').replace('X', '.')
    return f"{'-' if value < 0 else ''}{text} EUR"


def _signed_eur(value):
    return ('+' if value > 0 else '') + _eur(value)


def _card_text(row):
    """A card marker in the purpose or counterparty text of a debit."""
    return _CARD_MARKER.search(f"{row['description']} {row['counterparty']}".casefold()) is not None


def _cycle_end(day, cut):
    """End date of the card cycle that contains ``day`` (cut 0 = calendar month end)."""
    if cut == 0:
        nxt = date(day.year + (day.month == 12), day.month % 12 + 1, 1)
        return nxt - timedelta(days=1)
    if day.day <= cut:
        return date(day.year, day.month, cut)
    return date(day.year + (day.month == 12), day.month % 12 + 1, cut)


def _posting_info(postings, limit=12):
    ordered = sorted(postings, key=lambda p: (p['date'], p['account_id'], p['external_id']))
    return [{'account_id': p['account_id'], 'external_id': p['external_id'],
             'date': p['date'].isoformat(), 'amount': _text(p['amount'])}
            for p in ordered[:limit]]


def card_double_count_candidates(store, start, end, account_ids=None):
    """Find ordinary checking debits that duplicate card postings.

    Every hit is a *suggestion* with its reasoning (``reason``, the single
    postings, ``difference``); nothing is ever confirmed automatically and no sum
    changes.  Evidence kinds, strongest first:

    * ``repayment_posting``: the card account holds an equally large inflow
      within seven days, so debit and repayment are an unconfirmed transfer
      pair (expense and income are both counted).
    * ``card_spend``: the purchases of one card month add up centgenau to the
      debit, which falls into that month or the one after.
    * ``card_spend_multi`` / ``card_spend_cycle``: several cards together
      and/or a statement cycle that crosses the month boundary (statement day
      1-28) add up centgenau to the debit.  Only reported if exactly one set of
      postings fits; ambiguous sets stay unreported.
    * ``card_partial`` / ``card_surplus``: the debit text names a card
      (Kreditkarte, Visa, ...) but the amount differs from the card month: a
      part payment (10 percent up to below the total, remainder open) or a
      larger debit (up to twice the total).  The difference is reported, not
      explained away.

    Postings that already belong to a transfer (confirmed pair, shared
    transfer ID, single leg, source category) are excluded, because those are
    neutral and cannot double count.  Each debit and each card posting is
    used by at most one suggestion; several equally high debits are reported as
    one suggestion with ``alternative_debits``.
    """
    spend_by_card = {}
    card_inflows = []
    debits = []
    for row in _rows(store, start, end, account_ids):
        if effective_transfer_id(store, row):
            continue
        amount = money(row['amount'])
        if row['account_kind'] == 'CREDIT_CARD':
            if amount > 0:
                card_inflows.append(row)
            elif amount < 0:
                spend_by_card.setdefault(row['account_id'], []).append({
                    'account_id': row['account_id'], 'external_id': row['external_id'],
                    'date': date.fromisoformat(row['date']), 'amount': -amount,
                    'label': row['account_label']})
        elif row['account_kind'] != 'DEPOT' and amount < 0:
            debits.append(row)
    ordered_debits = sorted(debits, key=lambda row: not _card_text(row))  # stable
    result = []
    used_debits = set()
    used_postings = set()

    def ref(row):
        return (row['account_id'], row['external_id'])

    def posting_ref(posting):
        return (posting['account_id'], posting['external_id'])

    def alternatives(debit, accept):
        return sum(1 for other in debits
                   if ref(other) != ref(debit) and ref(other) not in used_debits
                   and money(other['amount']) == money(debit['amount']) and accept(other))

    def emit(kind, debit, postings, spend, month, reason, accept, confidence):
        used_debits.add(ref(debit))
        used_postings.update(posting_ref(p) for p in postings)
        card_ids = sorted({p['account_id'] for p in postings})
        debit_amount = -money(debit['amount'])
        other = alternatives(debit, accept)
        if other:
            reason += (f' Es gibt {other} weitere gleich hohe Abbuchung(en) im Zeitraum; '
                       'welche die Abrechnung ist, ist nicht belegt.')
        result.append({
            'kind': kind, 'card_account_id': card_ids[0], 'card_account_ids': card_ids,
            'card_month': month, 'card_posting_count': len(postings),
            'amount': _text(min(spend, debit_amount)), 'difference': _text(debit_amount - spend),
            'postings': _posting_info(postings), 'confidence': confidence,
            'reason': reason, 'alternative_debits': other,
            'debit_account_id': debit['account_id'],
            'debit_external_id': debit['external_id'], 'debit_date': debit['date']})

    # 1. A repayment posting on the card account.
    for debit in ordered_debits:
        for inflow in card_inflows:
            iref = ref(inflow)
            if (iref not in used_postings and money(inflow['amount']) == -money(debit['amount'])
                    and abs((date.fromisoformat(inflow['date'])
                             - date.fromisoformat(debit['date'])).days) <= 7):
                used_postings.add(iref)
                used_debits.add(ref(debit))
                result.append({
                    'kind': 'repayment_posting', 'card_account_id': inflow['account_id'],
                    'card_account_ids': [inflow['account_id']],
                    'card_month': inflow['date'][:7], 'card_posting_count': 1,
                    'amount': _text(money(inflow['amount'])), 'difference': '0.00',
                    'postings': [{'account_id': inflow['account_id'],
                                  'external_id': inflow['external_id'], 'date': inflow['date'],
                                  'amount': _text(money(inflow['amount']))}],
                    'confidence': 'high', 'alternative_debits': 0,
                    'reason': (f"Zufluss von {_eur(money(inflow['amount']))} auf "
                               f"{inflow['account_label']} am {inflow['date']} entspricht der "
                               f"Abbuchung vom {debit['date']} centgenau (Differenz 0,00 EUR, "
                               'höchstens 7 Tage Abstand).'),
                    'debit_account_id': debit['account_id'],
                    'debit_external_id': debit['external_id'], 'debit_date': debit['date']})
                break

    # 2. One card month equals the debit (debit in that month or the next).
    buckets = {}
    for card_id, postings in spend_by_card.items():
        for posting in postings:
            buckets.setdefault((card_id, posting['date'].isoformat()[:7]), []).append(posting)
    for (card_id, month), postings in sorted(buckets.items(), key=lambda item: (item[0][1], item[0][0])):
        spend = sum((p['amount'] for p in postings), Decimal(0))

        def month_ok(row, month=month):
            return row['date'][:7] in (month, _next_month(month))

        for debit in ordered_debits:
            if (ref(debit) not in used_debits and -money(debit['amount']) == spend and month_ok(debit)
                    and not any(posting_ref(p) in used_postings for p in postings)):
                emit('card_spend', debit, postings, spend, month,
                     f"{len(postings)} Kartenumsätze {month} auf {postings[0]['label']} summieren "
                     f"sich auf {_eur(spend)} und entsprechen der Abbuchung vom {debit['date']} "
                     'centgenau (Differenz 0,00 EUR).', month_ok, 'high')
                break

    # 3. Several cards and/or a statement cycle across the month boundary.
    index = {}
    for cut in range(0, 29):
        cycles = {}
        for card_id, postings in spend_by_card.items():
            for posting in postings:
                cycles.setdefault(_cycle_end(posting['date'], cut), {}).setdefault(
                    card_id, []).append(posting)
        for cycle_end, per_card in cycles.items():
            card_ids = sorted(per_card)
            for size in range(1, min(len(card_ids), 4) + 1):
                if size == 1 and cut == 0:
                    continue  # one card, calendar month: step 2
                for subset in combinations(card_ids, size):
                    members = [p for card_id in subset for p in per_card[card_id]]
                    if len(members) < 2:
                        continue
                    spend = sum((p['amount'] for p in members), Decimal(0))
                    key = frozenset(posting_ref(p) for p in members)
                    index.setdefault(spend, {}).setdefault(key, (cycle_end, cut, members, spend))
    for debit in ordered_debits:
        if ref(debit) in used_debits:
            continue
        debit_day = date.fromisoformat(debit['date'])
        fits = [entry for entry in index.get(-money(debit['amount']), {}).values()
                if max(p['date'] for p in entry[2]) <= debit_day <= entry[0] + timedelta(days=31)
                and not any(posting_ref(p) in used_postings for p in entry[2])]
        if len(fits) != 1:
            continue  # nothing fits, or the posting set is ambiguous
        cycle_end, cut, members, spend = fits[0]
        multi = len({p['account_id'] for p in members}) > 1
        names = ', '.join(sorted({p['label'] for p in members}))
        cycle_text = ('Kalendermonat' if cut == 0
                      else f'Abrechnungszeitraum bis {cycle_end.isoformat()}')
        lo = max(p['date'] for p in members)
        hi = cycle_end + timedelta(days=31)
        emit('card_spend_multi' if multi else 'card_spend_cycle', debit, members, spend,
             cycle_end.isoformat()[:7],
             f"{len(members)} Kartenumsätze auf {names} ({cycle_text}) summieren sich auf "
             f"{_eur(spend)} und entsprechen der Abbuchung vom {debit['date']} centgenau "
             '(Differenz 0,00 EUR).',
             lambda row, lo=lo, hi=hi: lo <= date.fromisoformat(row['date']) <= hi,
             'medium')

    # 4. Part payment / larger debit: only with a card marker in the debit text.
    all_months = {}
    for postings in spend_by_card.values():
        for posting in postings:
            all_months.setdefault(posting['date'].isoformat()[:7], []).append(posting)
    for debit in ordered_debits:
        if ref(debit) in used_debits or not _card_text(debit):
            continue
        debit_month = debit['date'][:7]
        previous = _previous_month(debit_month)
        month = previous if previous in all_months else debit_month
        postings = [p for p in all_months.get(month, []) if posting_ref(p) not in used_postings]
        if not postings:
            continue
        spend = sum((p['amount'] for p in postings), Decimal(0))
        amount = -money(debit['amount'])
        if spend / 10 <= amount < spend:
            kind, hint = 'card_partial', 'Teilzahlung; der Restposten ist nicht belegt'
        elif spend < amount <= 2 * spend:
            kind, hint = 'card_surplus', 'höhere Abbuchung; der Mehrbetrag ist nicht belegt'
        else:
            continue
        emit(kind, debit, postings, spend, month,
             f"Die Abbuchung vom {debit['date']} über {_eur(amount)} nennt eine Karte im Text; "
             f"{len(postings)} Kartenumsätze {month} ergeben {_eur(spend)}, Differenz "
             f"{_signed_eur(amount - spend)} ({hint}).",
             lambda row: True, 'low')
    return sorted(result, key=lambda item: (item['debit_date'], item['debit_account_id'],
                                            item['debit_external_id']))


def one_sided_source_transfers(store, start, end, account_ids=None):
    """List source "Umbuchung" postings that stay neutral without any counterpart."""
    items = []
    inflow = outflow = Decimal(0)
    for row in _rows(store, start, end, account_ids):
        if not source_transfer_without_counterpart(store, row):
            continue
        amount = money(row['amount'])
        inflow += max(amount, Decimal(0))
        outflow += max(-amount, Decimal(0))
        items.append({'account_id': row['account_id'], 'external_id': row['external_id'],
                      'date': row['date'], 'amount': _text(amount)})
    return {'count': len(items), 'outflow': _text(outflow), 'inflow': _text(inflow),
            'items': items[:_ITEM_LIMIT], 'truncated': len(items) > _ITEM_LIMIT}


def period_notices(store, start, end, account_ids=None):
    """Return both notices for an inclusive ISO date window; sums stay untouched."""
    date.fromisoformat(start)
    date.fromisoformat(end)
    candidates = card_double_count_candidates(store, start, end, account_ids)
    one_sided = one_sided_source_transfers(store, start, end, account_ids)
    return {
        'card_double_count': {
            'count': len(candidates),
            'amount': _text(sum((Decimal(item['amount']) for item in candidates), Decimal(0))),
            'items': candidates[:_ITEM_LIMIT],
            'note': ('Kartenumsätze und eine gleich hohe Sammelabbuchung sind beide als '
                     'gewöhnliche Buchungen gezählt, weil keine bestätigte Kartenabrechnung '
                     'vorliegt. Die Summen sind nicht verändert.'),
        },
        'one_sided_source_transfers': {
            **one_sided,
            'note': ('Die Quellkategorie nennt Umbuchung, eine Gegenbuchung fehlt. Die Buchungen '
                     'sind neutral gerechnet und hier getrennt ausgewiesen; ein Abfluss kann '
                     'ein Konto außerhalb des Ledgers meinen.'),
        },
        'has_findings': bool(candidates) or one_sided['count'] > 0,
    }
