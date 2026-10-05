"""Read-only comparison of an immutable budget revision with classified ledger data."""

import calendar
import json
import re
from datetime import UTC, date, datetime
from decimal import Decimal

from .budget import (
    _item_active,
    _month,
    _revision,
    canonical_category_id,
    load,
)
from .budget import _shift_month as _budget_shift_month
from .cash_components import cash_principal as _cash_principal
from .cash_components import cash_receipt_key
from .classification import exact_rule_match, normalize_counterparty
from .core import money
from .historical_positions import historical_positions
from .person_attribution import account_allocations, person_label, split_cents
from .reporting_history import month_quality, scope_for_month
from .reporting_history import validate as validate_reporting_history
from .transfer_corrections import source_declares_transfer

_PAGE_SIZE = 25


def _request(data, *, details=False):
    required = {"revision", "month"}
    allowed = required | ({"context", "page"} if details else {
        "person_breakdown", "include_trend",
    })
    if (not isinstance(data, dict) or not required <= set(data) or set(data) - allowed
            or (details and set(data) != allowed)):
        raise ValueError("invalid actual comparison request")
    if "person_breakdown" in data and type(data["person_breakdown"]) is not bool:
        raise ValueError("invalid actual person breakdown")
    if "include_trend" in data and type(data["include_trend"]) is not bool:
        raise ValueError("invalid actual trend selection")
    revision = _revision(data["revision"])
    month = _month(data["month"], "month")
    if not details:
        return revision, month
    context = data["context"]
    if not isinstance(context, dict) or set(context) != {"type", "item_id"}:
        raise ValueError("invalid actual detail context")
    if context["type"] not in {
            "item", "unmapped", "unmapped_income", "unmapped_expense", "unclassified"}:
        raise ValueError("invalid actual detail context")
    item_id = context["item_id"]
    if context["type"] == "item":
        if not isinstance(item_id, str) or not item_id:
            raise ValueError("invalid actual detail item")
    elif item_id is not None:
        raise ValueError("invalid actual detail item")
    page = data["page"]
    if type(page) is not int or page < 1:
        raise ValueError("invalid actual detail page")
    return revision, month, context, page


def _watermark(store):
    audit = store.db.execute("SELECT COALESCE(MAX(id),0) FROM classification_audit").fetchone()[0]
    imported = store.db.execute("SELECT COALESCE(MAX(id),0) FROM imports").fetchone()[0]
    return {"classification_audit_id": audit, "import_id": imported}


def _monthly_reference(snapshot, month, reporting_history=None):
    """Choose the same saved, retrospective, or budget projection basis everywhere."""
    rows = snapshot["calculation"]["rows"]
    calculation_row = next((row for row in rows if row["period"] == month), None)
    if calculation_row is not None:
        return calculation_row, {"type": "saved_budget", "label": "Gespeicherte Budgetrevision"}
    if not rows:
        raise ValueError("month is outside the chosen budget revision")
    reference_items = (rows[0].get("items", []) if "items" in rows[0]
                       else [item for item in snapshot["plan"].get("items", [])
                             if _item_active(item, rows[0]["period"])])
    items = _retrospective_monthly_items(reference_items)
    retrospective = (reporting_history is None
                     or "retrospective_actual_years" not in reporting_history
                     or int(month[:4]) in reporting_history["retrospective_actual_years"])
    plan_source = ("reconstructed_from_monthly_actuals" if retrospective else
                   "monthly_budget_projection")
    if not retrospective:
        if rows[0]["period"][:4] != month[:4]:
            # A later year's monthly revision is not an earlier year's budget.
            plan_source = "no_saved_budget"
        else:
            items = _projected_monthly_items(snapshot["plan"], items, month)
    buckets = {kind: sum((money(item["amount"]) for item in items
                          if item["kind"] == kind), Decimal("0.00"))
               for kind in ("income", "fixed", "variable")}
    return {
        "period": month, "income": _format(buckets["income"]),
        "fixed": _format(buckets["fixed"]), "variable": _format(buckets["variable"]),
        "cashflow": _format(buckets["income"] - buckets["fixed"] - buckets["variable"]),
        "items": items,
    }, {
        "type": ("retrospective_reference" if retrospective else plan_source),
        "label": ("Rückblick aus den importierten Ist-Buchungen" if retrospective else
                  "Kein gespeicherter Monatsplan" if plan_source == "no_saved_budget" else
                  "Monatsbudget der gewählten Revision"),
        "plan_source": plan_source,
        "derived_from_month": rows[0]["period"],
        "excluded_nonmonthly_items": sum(
            (item.get("interval_months") or 1) != 1 for item in reference_items),
        "excluded_one_time_items": sum(
            (item.get("interval_months") or 1) == 1
            and item.get("start_month") == item.get("end_month")
            for item in reference_items),
    }


def _snapshot(store, revision, month, available_from_month=None, reporting_history=None):
    available_from_month = (available_from_month if available_from_month is not None
                            else _available_from_month(store, month))
    if month < available_from_month:
        raise ValueError("month predates the first imported transaction")
    snapshot = load(store, {"revision": revision})
    rows = snapshot["calculation"]["rows"]
    if (rows and month < rows[0]["period"]
            or any(row["period"] == month for row in rows)):
        return snapshot, *_monthly_reference(snapshot, month, reporting_history)
    raise ValueError("month is outside the chosen budget revision")


def _retrospective_monthly_items(reference_items):
    """Apply the latest recurring monthly plan as the reference for older months."""
    return [item.copy() for item in reference_items
            if (item.get("interval_months") or 1) == 1
            and item.get("start_month") != item.get("end_month")]


def _projected_monthly_items(plan, reference_items, month):
    """Use the chosen monthly reference, preferring explicitly dated older rules.

    The horizon start does not invalidate the user's monthly comparison reference.
    A monthly rule still active in the target month can, however, replace its
    later successor; do not count both income/expense rules for that position.
    """
    active = _retrospective_monthly_items(
        [item for item in plan.get("items", []) if _item_active(item, month)])
    categories = {}
    for mapping in plan.get("actual_mappings", []):
        categories.setdefault(mapping["item_id"], set()).add(mapping["category_id"])

    def possible_successor(first, second):
        if first["start_month"] <= month:
            return False
        if (first["kind"], first.get("account_id")) != (
                second["kind"], second.get("account_id")):
            return False
        return (" ".join(first["label"].split()).casefold()
                == " ".join(second["label"].split()).casefold()
                or bool(categories.get(first["id"], set())
                        & categories.get(second["id"], set())))

    active_ids = {item["id"] for item in active}
    later = [item for item in reference_items if item["id"] not in active_ids]
    candidates = {item["id"]: [older["id"] for older in active
                                if possible_successor(item, older)]
                  for item in later}
    predecessor_counts = {}
    for predecessors in candidates.values():
        for predecessor in predecessors:
            predecessor_counts[predecessor] = predecessor_counts.get(predecessor, 0) + 1
    replaced = {item_id for item_id, predecessors in candidates.items()
                if len(predecessors) == 1 and predecessor_counts[predecessors[0]] == 1}
    return active + [item.copy() for item in later if item["id"] not in replaced]


def _cash_purchase_category(store, item, noncash):
    """Project a purchase category only from one confirmed, exact Bonsy payment."""
    links = store.db.execute(
        "SELECT l.allocation_type,l.allocated_amount,d.amount,d.currency,d.status,"
        "d.warnings,r.entry_id,r.vendor,r.total,r.currency AS receipt_currency "
        "FROM classification_document_links l "
        "JOIN classification_documents d ON d.id=l.document_id AND d.kind='invoice' "
        "LEFT JOIN bonsy_receipts r ON r.document_id=d.id "
        "WHERE l.account_id=? AND l.external_id=? "
        "AND l.allocation_type IN ('payment','refund')",
        (item["account_id"], item["external_id"]),
    ).fetchall()
    if len(links) != 1:
        return None
    link = links[0]
    if (link["allocation_type"] != "payment" or link["entry_id"] is None
            or link["status"] != "confirmed" or link["currency"] != "EUR"
            or link["receipt_currency"] != "EUR"):
        return None
    try:
        warnings = json.loads(link["warnings"])
    except (TypeError, ValueError):
        return None
    if (not isinstance(warnings, list)
            or any(value in warnings for value in
                   ("source_excluded_bonsy", "duplicate_source_document"))):
        return None
    if not (money(link["total"]) == money(link["amount"])
            == money(link["allocated_amount"]) == noncash):
        return None
    match = exact_rule_match(store, link["vendor"], item["description"], "expense")
    if match is None or canonical_category_id(store, match["category"]) == "AUSGABEN_BARGELD":
        return None
    category = store.db.execute(
        "SELECT id,label FROM category_catalog WHERE id=? AND transaction_type='expense'",
        (match["category"],),
    ).fetchone()
    return category


def _ledger_rows(store, month, explicit_allocations=None, cash_receipt_item_id=None,
                 *, reporting_scope=None, cash_withdrawals=None):
    rows = store.db.execute(
        "SELECT t.*,c.counterparty,c.description,o.category_id,o.confirmed,"
        "cat.label AS category_label,cat.transaction_type,a.kind AS account_kind,"
        "a.owner AS account_owner "
        "FROM transactions t "
        "JOIN accounts a ON a.id=t.account_id "
        "LEFT JOIN transaction_context c USING(account_id,external_id) "
        "LEFT JOIN classification_overrides o USING(account_id,external_id) "
        "LEFT JOIN category_catalog cat ON cat.id=o.category_id "
        "WHERE substr(t.date,1,7)=? AND t.currency='EUR' "
        "ORDER BY t.date,t.account_id,t.external_id",
        (month,),
    ).fetchall()
    single_keys = {(row["account_id"], row["external_id"]) for row in store.db.execute(
        "SELECT s.account_id,s.external_id FROM transfer_single_legs s "
        "JOIN transactions t USING(account_id,external_id) "
        "WHERE substr(t.date,1,7)=? AND t.currency='EUR'", (month,))}
    correction_rows = store.db.execute(
        "SELECT m.account_id,m.external_id,m.pair_id, "
        "EXISTS(SELECT 1 FROM transfer_correction_members cm "
        "JOIN accounts ca ON ca.id=cm.account_id AND ca.kind='CREDIT_CARD' "
        "WHERE cm.pair_id=m.pair_id) AS has_card "
        "FROM transfer_correction_members m "
        "JOIN transfer_correction_pairs p ON p.id=m.pair_id AND p.revoked_at IS NULL "
        "JOIN transactions t ON t.account_id=m.account_id AND t.external_id=m.external_id "
        "WHERE substr(t.date,1,7)=? AND t.currency='EUR'", (month,)).fetchall()
    correction_by_key = {(row["account_id"], row["external_id"]): row
                         for row in correction_rows}
    imported_ids = {row["transfer_id"] for row in rows if row["transfer_id"]}
    depot_transfer_ids = {row["transfer_id"] for row in store.db.execute(
        "SELECT DISTINCT t.transfer_id FROM transactions t "
        "JOIN accounts a ON a.id=t.account_id AND a.kind='DEPOT' "
        "WHERE t.transfer_id IS NOT NULL")}
    card_transfer_ids = set()
    if imported_ids:
        placeholders = ",".join("?" for _ in imported_ids)
        card_transfer_ids = {row["transfer_id"] for row in store.db.execute(
            "SELECT DISTINCT t.transfer_id FROM transactions t "
            "JOIN accounts a ON a.id=t.account_id AND a.kind='CREDIT_CARD' "
            f"WHERE t.transfer_id IN ({placeholders})",
            tuple(imported_ids))}
    result = []
    transfer_ids = set()
    transfer_transaction_count = 0
    included_transfer_transaction_count = 0
    cash_withdrawal_count = 0
    depot_movement_count = 0
    explicit_allocation_keys = set(explicit_allocations or {})
    included = (set(reporting_scope["included_account_ids"])
                if reporting_scope is not None else None)
    for row in rows:
        if included is not None and row["account_id"] not in included:
            continue
        item = dict(row)
        key = (row["account_id"], row["external_id"])
        if row["transfer_id"]:
            transfer = row["transfer_id"]
        elif source_declares_transfer(row):
            transfer = f"source-category:{row['account_id']}:{row['external_id']}"
        elif key in single_keys:
            transfer = f"single:{row['account_id']}:{row['external_id']}"
        else:
            correction = correction_by_key.get(key)
            transfer = None if correction is None else f"correction:{correction['pair_id']}"
        explicitly_allocated_transfer = False
        if transfer:
            transfer_transaction_count += 1
            transfer_ids.add(transfer)
            correction = correction_by_key.get(key)
            has_card = (item["account_kind"] == "CREDIT_CARD"
                        or (bool(row["transfer_id"])
                            and row["transfer_id"] in card_transfer_ids)
                        or (correction is not None and correction["has_card"]))
            if has_card:
                card_transfer_ids.add(transfer)
        if (item["account_kind"] == "DEPOT"
                or (row["transfer_id"] and row["transfer_id"] in depot_transfer_ids)):
            depot_movement_count += 1
            continue
        cash_withdrawal = (
            item["confirmed"] == 1
            and canonical_category_id(store, item["category_id"]) == "AUSGABEN_BARGELD"
            and money(item["amount"]) < 0
        )
        if cash_withdrawal:
            cash_withdrawal_count += 1
            principal = _cash_principal(item)
            gross = -money(item["amount"])
            noncash = gross - principal
            if cash_withdrawals is not None:
                cash_withdrawals.append({
                    "account_id": item["account_id"], "principal": principal,
                    "gross": gross, "noncash": noncash,
                })
            if noncash == 0:
                continue
            item["amount"] = _format(-noncash)
            item["category_id"] = None
            item["category_label"] = "Gebühren / Kaufanteil einer Bargeldabhebung"
            purchase_category = _cash_purchase_category(store, item, noncash)
            if purchase_category is not None:
                item["category_id"] = purchase_category["id"]
                item["category_label"] = purchase_category["label"]
            item["transaction_type"] = "expense"
            # The residual is an ordinary expense, not a transfer or cash receipt.
            transfer = None
        if transfer:
            allocation_key = (item["account_id"], item["external_id"])
            if allocation_key in explicit_allocation_keys:
                amount = money(item["amount"])
                direction = "income" if amount > 0 else "expense" if amount < 0 else None
                if direction is None:
                    continue
                item["transaction_type"] = direction
                explicitly_allocated_transfer = True
                included_transfer_transaction_count += 1
            else:
                continue
        # A transfer label without a verified pair is deliberately not enough to
        # hide a card settlement or another posting from the open remainder.
        if (not explicitly_allocated_transfer
                and (item["confirmed"] != 1
                     or item["transaction_type"] not in {"income", "expense"})):
            item["actual_state"] = "unclassified"
        else:
            item["actual_state"] = "classified"
        result.append(item)
    if cash_receipt_item_id is not None:
        result.extend(row for row in _cash_receipt_rows(store, month, cash_receipt_item_id)
                      if included is None or row["account_id"] in included)
    return result, {
        "transfer_count": len(transfer_ids),
        "transfer_transaction_count": transfer_transaction_count,
        "card_settlement_count": len(card_transfer_ids),
        "included_transfer_transaction_count": included_transfer_transaction_count,
        "cash_withdrawal_count": cash_withdrawal_count,
        "depot_movement_count": depot_movement_count,
    }


def _include_open_historical_rows(ledger_rows):
    """Let open classifications contribute to retrospective totals without hiding their count."""
    totals = {"income": Decimal("0.00"), "expenses": Decimal("0.00"), "count": 0}
    keys = {"income": set(), "expenses": set()}
    for row in ledger_rows:
        if row["actual_state"] != "unclassified":
            continue
        amount = money(row["amount"])
        if amount == 0:
            continue
        direction = "income" if amount > 0 else "expenses"
        totals[direction] += abs(amount)
        totals["count"] += 1
        keys[direction].add((row["account_id"], row["external_id"]))
        row["transaction_type"] = "income" if amount > 0 else "expense"
        row["actual_state"] = "classified"
        row["historical_classification_open"] = True
    return totals, keys


def _cash_receipt_rows(store, month, item_id):
    """Return confirmed Bonsy cash allocations, capped against duplicate payment links."""
    rows = store.db.execute(
        "SELECT ca.entry_id,ca.account_id,ca.external_id,ca.allocated_amount,"
        "r.document_id,r.occurred_at,r.vendor,r.total,r.currency AS receipt_currency,"
        "t.amount,t.category,c.counterparty,c.description,"
        "t.currency AS transaction_currency,o.category_id,o.confirmed,"
        "a.kind AS account_kind,a.owner AS account_owner "
        "FROM bonsy_cash_allocations ca "
        "JOIN bonsy_receipts r USING(entry_id) "
        "JOIN classification_documents d ON d.id=r.document_id "
        "JOIN transactions t USING(account_id,external_id) "
        "JOIN accounts a ON a.id=ca.account_id "
        "LEFT JOIN transaction_context c USING(account_id,external_id) "
        "LEFT JOIN classification_overrides o USING(account_id,external_id) "
        "WHERE substr(r.occurred_at,1,7)<=? AND d.status='confirmed' "
        "ORDER BY r.occurred_at,ca.entry_id,ca.account_id,ca.external_id",
        (month,),
    ).fetchall()
    result_by_receipt_account = {}
    remaining_by_receipt = {}
    allocated_by_withdrawal = {}
    withdrawal_amounts = {}
    for row in rows:
        if (row["receipt_currency"] != "EUR" or row["transaction_currency"] != "EUR"
                or row["confirmed"] != 1
                or canonical_category_id(store, row["category_id"]) != "AUSGABEN_BARGELD"
                or money(row["amount"]) >= 0):
            continue
        receipt_key = row["entry_id"]
        if receipt_key not in remaining_by_receipt:
            direct_paid = sum((money(link[0]) for link in store.db.execute(
                "SELECT allocated_amount FROM classification_document_links "
                "WHERE document_id=? AND allocation_type='payment'",
                (row["document_id"],))), Decimal("0.00"))
            remaining_by_receipt[receipt_key] = max(
                money(row["total"]) - direct_paid, Decimal("0.00"))
        withdrawal_key = (row["account_id"], row["external_id"])
        if withdrawal_key not in withdrawal_amounts:
            withdrawal_amounts[withdrawal_key] = _cash_principal(row)
        withdrawal_remaining = max(
            withdrawal_amounts[withdrawal_key]
            - allocated_by_withdrawal.get(withdrawal_key, Decimal("0.00")),
            Decimal("0.00"),
        )
        amount = min(
            money(row["allocated_amount"]), remaining_by_receipt[receipt_key],
            withdrawal_remaining,
        )
        if amount <= 0:
            continue
        remaining_by_receipt[receipt_key] -= amount
        allocated_by_withdrawal[withdrawal_key] = (
            allocated_by_withdrawal.get(withdrawal_key, Decimal("0.00")) + amount)
        if row["occurred_at"][:7] != month:
            continue
        occurred_date = row["occurred_at"][:10]
        aggregate_key = (row["entry_id"], row["account_id"])
        synthetic = result_by_receipt_account.get(aggregate_key)
        if synthetic is None:
            synthetic = {
            "account_id": row["account_id"],
            "external_id": cash_receipt_key(row["entry_id"], row["account_id"]),
            "date": occurred_date, "amount": _format(-amount), "currency": "EUR",
            "counterparty": row["vendor"] or "", "description": "Bonsy cash receipt",
            "category_id": "AUSGABEN_BARGELD", "category_label": "Bargeldbeleg",
            "confirmed": 1, "transaction_type": "expense", "account_kind": row["account_kind"],
            "account_owner": row["account_owner"], "actual_state": "classified",
            "cash_receipt_item_id": item_id, "bonsy_entry_id": row["entry_id"],
            "bonsy_document_id": row["document_id"],
            "bonsy_sources": [],
            }
            result_by_receipt_account[aggregate_key] = synthetic
        else:
            synthetic["amount"] = _format(money(synthetic["amount"]) - amount)
        synthetic["bonsy_sources"].append({
            "account_id": row["account_id"], "external_id": row["external_id"],
            "allocated_amount": _format(amount),
        })
    return list(result_by_receipt_account.values())


def _positive_actual(row):
    amount = money(row["amount"])
    return amount if row["transaction_type"] == "income" else -amount


def _cash_activity(store, withdrawals, ledger_rows, receipt_by_item, people):
    """Separate cash funding in the booking month from receipt consumption.

    Receipt spending is already included in actual expenses. Neither withdrawal
    principal nor a difference between these monthly flows is an expense or a
    known wallet balance; receipts can use withdrawals from earlier months.
    Person attribution follows the source account, as in the actual comparison.
    """
    shares = account_allocations(store, people)
    withdrawal_accounts = {}
    withdrawal_people = {}
    receipt_people = {}

    def add_person(bucket, account_id, amount):
        parts = split_cents(amount, shares.get(account_id, {None: Decimal(1)}))
        for person, part in parts.items():
            person = person or "JOINT"
            bucket[person] = bucket.get(person, Decimal("0.00")) + part

    for withdrawal in withdrawals:
        account_id = withdrawal["account_id"]
        principal = withdrawal["principal"]
        withdrawal_accounts[account_id] = (
            withdrawal_accounts.get(account_id, Decimal("0.00")) + principal)
        add_person(withdrawal_people, account_id, principal)
    receipt_rows = [row for row in ledger_rows if row.get("bonsy_entry_id") is not None]
    for row in receipt_rows:
        add_person(receipt_people, row["account_id"], _positive_actual(row))
    formatted = lambda amounts: {key: _format(value) for key, value in amounts.items()}
    return {
        "withdrawals": {
            "total": _format(sum((row["principal"] for row in withdrawals), Decimal(0))),
            "gross_total": _format(sum((row["gross"] for row in withdrawals), Decimal(0))),
            "noncash_expenses": _format(sum((row["noncash"] for row in withdrawals), Decimal(0))),
            "count": len(withdrawals),
            "by_account": formatted(withdrawal_accounts),
            "by_person": formatted(withdrawal_people),
        },
        "receipt_spending": {
            "total": _format(sum((_positive_actual(row) for row in receipt_rows), Decimal(0))),
            "count": len({row["bonsy_entry_id"] for row in receipt_rows}),
            "by_person": formatted(receipt_people),
            "by_item": formatted(receipt_by_item),
        },
    }


def _mapping(store, snapshot):
    mappings = snapshot["plan"].get("actual_mappings", [])
    return {canonical_category_id(store, entry["category_id"]): entry["item_id"]
            for entry in mappings}


def _normalized_counterparty(value):
    """Normalize counterparty spelling while preserving meaningful words."""
    if not isinstance(value, str) or not value.strip():
        return ""
    return normalize_counterparty(value)


def _allocation_history(store, snapshot):
    """Index explicit allocations by counterparty and confirmed category.

    Exact historical amounts are retained to disambiguate signatures that
    were explicitly allocated to more than one plan item.
    """
    groups = _allocations(snapshot)
    history = {"counterparty": {}, "category": {}}
    rows = {}
    keys = list(groups)
    for start in range(0, len(keys), 400):
        chunk = keys[start:start + 400]
        placeholders = ",".join("(?,?)" for _ in chunk)
        parameters = [value for key in chunk for value in key]
        query = (
            f"WITH requested(account_id,external_id) AS (VALUES {placeholders}) "
            "SELECT t.account_id,t.external_id,t.amount,cat.transaction_type,"
            "c.counterparty,o.confirmed,o.category_id "
            "FROM requested r "
            "JOIN transactions t USING(account_id,external_id) "
            "LEFT JOIN transaction_context c USING(account_id,external_id) "
            "LEFT JOIN classification_overrides o USING(account_id,external_id) "
            "LEFT JOIN category_catalog cat ON cat.id=o.category_id"
        )
        for row in store.db.execute(query, parameters):
            rows[(row["account_id"], row["external_id"])] = row
    for (account_id, external_id), allocations in groups.items():
        row = rows.get((account_id, external_id))
        if (row is None or row["confirmed"] != 1
                or row["transaction_type"] not in {"income", "expense"}):
            continue
        counterparty = _normalized_counterparty(row["counterparty"])
        amount = money(row["amount"])
        positive = amount if row["transaction_type"] == "income" else -amount
        keys = []
        if counterparty:
            keys.append(("counterparty", (account_id, row["transaction_type"], counterparty)))
        category_id = canonical_category_id(store, row["category_id"])
        if category_id:
            keys.append(("category", (account_id, row["transaction_type"], category_id)))
        if not keys:
            continue
        for item_id, allocated in _split_actual(positive, allocations):
            for history_kind, key in keys:
                history[history_kind].setdefault(key, {}).setdefault(item_id, set()).add(allocated)
    return history


def _match_history_signature(row, candidates):
    """Resolve a unique history signature or a unique exact amount match."""
    if len(candidates) == 1:
        return next(iter(candidates))
    amount = _positive_actual(row)
    exact = [item_id for item_id, amounts in candidates.items() if amount in amounts]
    return exact[0] if len(exact) == 1 else None


def _historical_allocation_match(row, history):
    """Match by explicit allocation history for the same normalized counterparty."""
    counterparty = _normalized_counterparty(row.get("counterparty"))
    if not counterparty:
        return None
    key = (row["account_id"], row["transaction_type"], counterparty)
    return _match_history_signature(row, history["counterparty"].get(key, {}))


def _historical_category_match(store, row, history):
    """Use category history only when the booking has no usable counterparty."""
    if _normalized_counterparty(row.get("counterparty")):
        return None
    category_id = canonical_category_id(store, row["category_id"])
    if not category_id:
        return None
    key = (row["account_id"], row["transaction_type"], category_id)
    return _match_history_signature(row, history["category"].get(key, {}))


def _month_plan_candidates(snapshot, month_plan, month):
    """Index confirmed, active plan items with an explicit checking account."""
    if "items" in month_plan:
        items = month_plan["items"]
    else:
        items = [item for item in snapshot["plan"]["items"] if _item_active(item, month)]
    candidates = {}
    for item in items:
        account_id = item.get("account_id")
        if not item.get("confirmed") or not isinstance(account_id, str) or not account_id:
            continue
        direction = "income" if item["kind"] == "income" else "expense"
        key = (account_id, direction, money(item["amount"]))
        candidates.setdefault(key, set()).add(item["id"])
    return candidates


def _planned_account_match(row, candidates):
    """Use account, direction and exact planned amount only when unique."""
    account_id = row.get("account_id")
    if not isinstance(account_id, str) or not account_id:
        return None
    key = (account_id, row["transaction_type"], _positive_actual(row))
    matches = candidates.get(key, set())
    return next(iter(matches)) if len(matches) == 1 else None


def _resolved_item(store, mapping, snapshot, row, history, month_candidates):
    """Resolve a transaction through explicit evidence and safe fallbacks."""
    explicit = mapping.get(canonical_category_id(store, row["category_id"]))
    if explicit is not None:
        return explicit, False, None
    historical = _historical_allocation_match(row, history)
    if historical is not None:
        return historical, False, None
    planned = _planned_account_match(row, month_candidates)
    if planned is not None:
        return planned, False, None
    historical = _historical_category_match(store, row, history)
    if historical is not None:
        return historical, False, None
    mapped = _mapped_item(store, mapping, snapshot, row)
    if mapped[0] is not None:
        return mapped
    expense_fallback = snapshot["plan"].get("unmapped_expense_item_id")
    if row["transaction_type"] == "expense" and expense_fallback is not None:
        return expense_fallback, False, None
    return mapped


def _actual_item_allocations(store, mapping, snapshot, row, history, month_candidates,
                             allocations):
    """Resolve one classified booking using the comparison's shared precedence."""
    amount = _positive_actual(row)
    exact = allocations.get((row["account_id"], row["external_id"]))
    if exact is not None:
        return _split_actual(amount, exact), False, None
    if row.get("cash_receipt_item_id") is not None:
        return [(row["cash_receipt_item_id"], amount)], False, None
    item_id, owner_mapped, warning = _resolved_item(
        store, mapping, snapshot, row, history, month_candidates)
    return ([(item_id, amount)] if item_id is not None else []), owner_mapped, warning


def _historical_actual_parts(store, mapping, snapshot, row, history,
                             month_candidates, allocations):
    """Split a booking into plan destinations and historical fixed/other parts."""
    if row.get("historical_classification_open"):
        return [], Decimal("0.00"), Decimal("0.00"), False, None
    item_definitions = {item["id"]: item for item in snapshot["plan"]["items"]}
    item_amounts, owner_mapped, warning = _actual_item_allocations(
        store, mapping, snapshot, row, history, month_candidates, allocations)
    amount = _positive_actual(row)
    fixed_income = Decimal("0.00")
    fixed_expenses = Decimal("0.00")
    fixed_income_categories = {
        "EINNAHMEN_GEHALT", "EINKOMMEN_LOHNERSATZ",
        "EINNAHMEN_UNTERHALT",
    }
    category_id = canonical_category_id(store, row.get("category_id"))
    if (row["transaction_type"] == "income" and not item_amounts
            and category_id in fixed_income_categories):
        fixed_income = amount
    purpose = " ".join(str(row.get(key) or "") for key in
                       ("category", "source_category", "description"))
    if (not item_amounts and row.get("confirmed") == 1
            and row["transaction_type"] == "expense" and amount > 0
            and category_id == "FINANZEN_KREDIT"
            and re.search(r"\bBaufinanzierung\b", purpose, re.IGNORECASE)
            and re.search(r"\bLeistungen\s+zum\b", purpose, re.IGNORECASE)
            and "sondertilg" not in purpose.casefold()):
        # Historical regular mortgage payments remain fixed without a current
        # matching plan item; one-off repayments do not use this fallback.
        fixed_expenses = amount
    elif (not item_amounts and row.get("confirmed") == 1
          and row["transaction_type"] == "expense" and amount > 0
          and category_id == "MOBILITAET_FAHRZEUGRATE"):
        # A confirmed vehicle installment remains fixed without a current item.
        fixed_expenses = amount
    for item_id, amount in item_amounts:
        item = item_definitions.get(item_id)
        if item is None or not item.get("confirmed"):
            continue
        if row["transaction_type"] == "income" and item["kind"] == "income":
            fixed_income += amount
        elif row["transaction_type"] == "expense" and item["kind"] == "fixed":
            fixed_expenses += amount
    return item_amounts, fixed_income, fixed_expenses, owner_mapped, warning


def _allocations(snapshot):
    allocations = {}
    for entry in snapshot["plan"].get("actual_allocations", []):
        allocations.setdefault((entry["account_id"], entry["external_id"]), []).append({
            "item_id": entry["item_id"],
            "weight": Decimal(entry["weight"]) if "weight" in entry else None,
        })
    return allocations


def _split_actual(actual, allocations):
    """Split one cent-exact actual amount by weights without losing a cent."""
    if len(allocations) == 1 and allocations[0]["weight"] is None:
        return [(allocations[0]["item_id"], actual)]
    total_weight = sum((entry["weight"] for entry in allocations), Decimal(0))
    sign = Decimal(-1) if actual < 0 else Decimal(1)
    cents = abs(int(actual * 100))
    shares = []
    assigned = 0
    for index, entry in enumerate(allocations):
        numerator = cents * entry["weight"]
        base = int(numerator // total_weight)
        shares.append([entry["item_id"], base, numerator % total_weight, index])
        assigned += base
    for share in sorted(shares, key=lambda value: (-value[2], value[3]))[:cents - assigned]:
        share[1] += 1
    return [(item_id, sign * Decimal(share_cents) / 100)
            for item_id, share_cents, _, _ in shares]


def _mapped_item(store, mapping, snapshot, row):
    """Resolve an explicit mapping or one unambiguous salary owner match."""
    explicit = mapping.get(canonical_category_id(store, row["category_id"]))
    if explicit is not None:
        return explicit, False, None
    if row["category_id"] != "EINNAHMEN_GEHALT":
        return None, False, None
    owner = row["account_owner"]
    if not isinstance(owner, str) or not owner or owner == "JOINT":
        return None, False, None
    owner = row["account_owner"].casefold()
    candidates = [item["id"] for item in snapshot["plan"]["items"]
                  if item["kind"] == "income"
                  and _owner_label_matches(item["label"], owner)]
    if len(candidates) == 1:
        return candidates[0], True, None
    return None, False, "ambiguous_salary_owner_mapping" if len(candidates) > 1 else None


def _owner_label_matches(label, owner):
    """Match an owner prefix without confusing names such as Ann and Anna."""
    label_key = label.casefold().strip()
    owner_key = owner.casefold().strip()
    estimate_prefix = "schätzung · "
    if label_key.startswith(estimate_prefix):
        label_key = label_key[len(estimate_prefix):].lstrip()
    if not owner_key or not label_key.startswith(owner_key):
        return False
    return len(label_key) == len(owner_key) or not label_key[len(owner_key)].isalnum()


def _format(value):
    return format(value, ".2f")


def _split_by_household(amount, household_split):
    """Allocate a cent-exact signed amount by shares using largest remainders."""
    sign = Decimal(-1) if amount < 0 else Decimal(1)
    cents = abs(int(amount * 100))
    shares = []
    assigned = 0
    for index, entry in enumerate(household_split):
        exact_cents = Decimal(cents) * money(entry["share"])
        base_cents = int(exact_cents)
        shares.append([entry["party_id"], base_cents, exact_cents - base_cents, index])
        assigned += base_cents
    for share in sorted(shares, key=lambda value: (-value[2], value[3]))[:cents - assigned]:
        share[1] += 1
    return {party_id: _format(sign * Decimal(share_cents) / 100)
            for party_id, share_cents, _, _ in shares}


def _surplus_bridge(rows, unmapped, unclassified, planned_cashflow, household_split=None):
    """Explain confirmed headroom assuming every planned budget is fully used.

    Unused budget is deliberately absent from this bridge: it answers what is
    left after the plan is completed, rather than what is currently liquid.
    Unconfirmed ledger rows remain a separate scenario.
    """
    planned_cashflow = money(planned_cashflow)
    additional_income = money(unmapped["income"])
    unplanned_expenses = money(unmapped["expenses"])
    budget_planned = sum(
        (money(row["planned"]) for row in rows
         if row["kind"] == "variable" and money(row["planned"]) > 0),
        Decimal("0.00"),
    )
    budget_actual = sum(
        (money(row["actual"]) for row in rows
         if row["kind"] == "variable" and money(row["planned"]) > 0),
        Decimal("0.00"),
    )
    budget_remaining = max(budget_planned - budget_actual, Decimal("0.00"))
    budget_overrun = max(budget_actual - budget_planned, Decimal("0.00"))
    item_to_party = {
        item_id: entry["party_id"]
        for entry in household_split or []
        for item_id in entry.get("personal_item_ids", [])
    }
    personal_adjustments = {
        entry["party_id"]: Decimal("0.00") for entry in household_split or []
    }
    shared_still_unallocated = planned_cashflow + additional_income - unplanned_expenses
    for row in rows:
        planned = money(row["planned"])
        actual = money(row["actual"])
        party_id = item_to_party.get(row["item_id"])
        adjustment = Decimal("0.00")
        if row["kind"] == "income":
            adjustment = max(actual - planned, Decimal("0.00"))
            if party_id is None:
                additional_income += adjustment
        elif planned == 0:
            adjustment = -actual
            if party_id is None:
                unplanned_expenses += actual
        elif row["kind"] == "fixed":
            # Fixed-plan excess is not part of the adjustable variable pool.
            fixed_overrun = max(actual - planned, Decimal("0.00"))
            adjustment = -fixed_overrun
            if party_id is None:
                unplanned_expenses += fixed_overrun
        elif row["item_id"] in item_to_party:
            # Explicit personal exceptions do not draw on the common pool.
            adjustment = -max(actual - planned, Decimal("0.00"))
        if party_id is None:
            shared_still_unallocated += adjustment
        else:
            personal_adjustments[party_id] += adjustment

    personal_variable_ids = set(item_to_party)
    shared_budget_planned = sum(
        (money(row["planned"]) for row in rows
         if row["kind"] == "variable" and money(row["planned"]) > 0
         and row["item_id"] not in personal_variable_ids),
        Decimal("0.00"),
    )
    shared_budget_actual = sum(
        (money(row["actual"]) for row in rows
         if row["kind"] == "variable" and money(row["planned"]) > 0
         and row["item_id"] not in personal_variable_ids),
        Decimal("0.00"),
    )
    shared_still_unallocated -= max(
        shared_budget_actual - shared_budget_planned, Decimal("0.00"))

    still_unallocated = shared_still_unallocated + sum(
        personal_adjustments.values(), Decimal("0.00"))
    # ``absolute`` and ``net`` retain enough information to separate positive
    # and negative postings even where no category has been confirmed yet.
    unconfirmed_expenses = (
        money(unclassified["absolute"]) - money(unclassified["net"])
    ) / 2
    unconfirmed_income = (
        money(unclassified["absolute"]) + money(unclassified["net"])
    ) / 2
    unconfirmed_net = unconfirmed_income - unconfirmed_expenses
    result = {
        "original_planned_surplus": _format(planned_cashflow),
        "confirmed": {
            "additional_income": _format(additional_income),
            "unplanned_expenses": _format(unplanned_expenses),
            "budget_balance": {
                "planned_total": _format(budget_planned),
                "actual_total": _format(budget_actual),
                "remaining": _format(budget_remaining),
                "overrun": _format(budget_overrun),
            },
            "still_unallocated": _format(still_unallocated),
        },
        "unconfirmed": {
            "income": _format(unconfirmed_income),
            "expenses": _format(unconfirmed_expenses),
            "net": _format(unconfirmed_net),
            "count": unclassified["count"],
            "still_unallocated_if_confirmed": _format(still_unallocated + unconfirmed_net),
        },
    }
    result["household_split"] = None
    if household_split:
        # Unconfirmed bookings have no plan-item owner, even if their retained
        # category would otherwise match an actual mapping.
        shared_unconfirmed_net = unconfirmed_net
        including_unconfirmed_shared = shared_still_unallocated + shared_unconfirmed_net
        including_unconfirmed_personal = {
            party_id: personal_adjustments[party_id]
            for party_id in personal_adjustments
        }
        confirmed_allocations = _split_by_household(
            shared_still_unallocated, household_split)
        including_allocations = _split_by_household(
            including_unconfirmed_shared, household_split)
        for party_id, adjustment in personal_adjustments.items():
            confirmed_allocations[party_id] = _format(
                money(confirmed_allocations[party_id]) + adjustment)
        for party_id, adjustment in including_unconfirmed_personal.items():
            including_allocations[party_id] = _format(
                money(including_allocations[party_id]) + adjustment)
        including_unconfirmed = including_unconfirmed_shared + sum(
            including_unconfirmed_personal.values(), Decimal("0.00"))
        shared_budget_remaining = max(
            shared_budget_planned - shared_budget_actual, Decimal("0.00"))
        personal_budget_remaining = {
            party_id: sum(
                (max(money(row["planned"]) - money(row["actual"]), Decimal("0.00"))
                 for row in rows
                 if row["kind"] == "variable" and money(row["planned"]) > 0
                 and item_to_party.get(row["item_id"]) == party_id),
                Decimal("0.00"),
            )
            for party_id in personal_adjustments
        }
        confirmed_spendable_allocations = {
            party_id: money(amount)
            for party_id, amount in confirmed_allocations.items()
        }
        shared_budget_allocations = _split_by_household(
            shared_budget_remaining, household_split)
        for party_id in confirmed_spendable_allocations:
            confirmed_spendable_allocations[party_id] += (
                money(shared_budget_allocations[party_id])
                + personal_budget_remaining[party_id]
            )
        including_spendable_allocations = confirmed_spendable_allocations.copy()
        personal_unconfirmed_net = {
            party_id: Decimal("0.00") for party_id in personal_adjustments
        }
        shared_unconfirmed_net = unconfirmed_net
        shared_unconfirmed_allocations = _split_by_household(
            shared_unconfirmed_net, household_split)
        for party_id in including_spendable_allocations:
            including_spendable_allocations[party_id] += (
                money(shared_unconfirmed_allocations[party_id])
                + personal_unconfirmed_net[party_id]
            )
        confirmed_spendable_total = sum(
            confirmed_spendable_allocations.values(), Decimal("0.00"))
        including_spendable_total = sum(
            including_spendable_allocations.values(), Decimal("0.00"))
        result["household_split"] = {
            "shares": {entry["party_id"]: entry["share"] for entry in household_split},
            "confirmed": {
                "total": _format(still_unallocated),
                "shared_total": _format(shared_still_unallocated),
                "personal_adjustments": {
                    party_id: _format(adjustment)
                    for party_id, adjustment in personal_adjustments.items()
                },
                "allocations": confirmed_allocations,
            },
            "including_unconfirmed": {
                "total": _format(including_unconfirmed),
                "shared_total": _format(including_unconfirmed_shared),
                "personal_adjustments": {
                    party_id: _format(adjustment)
                    for party_id, adjustment in including_unconfirmed_personal.items()
                },
                "allocations": including_allocations,
            },
            "spendable": {
                "confirmed": {
                    "total": _format(confirmed_spendable_total),
                    "shared_budget_remaining": _format(shared_budget_remaining),
                    "personal_budget_remaining": {
                        party_id: _format(amount)
                        for party_id, amount in personal_budget_remaining.items()
                    },
                    "allocations": {
                        party_id: _format(amount)
                        for party_id, amount in confirmed_spendable_allocations.items()
                    },
                },
                "including_unconfirmed": {
                    "total": _format(including_spendable_total),
                    "shared_budget_remaining": _format(shared_budget_remaining),
                    "personal_budget_remaining": {
                        party_id: _format(amount)
                        for party_id, amount in personal_budget_remaining.items()
                    },
                    "unconfirmed_net": _format(unconfirmed_net),
                    "allocations": {
                        party_id: _format(amount)
                        for party_id, amount in including_spendable_allocations.items()
                    },
                },
            },
        }
    return result


def _tree(rows, unmapped, unclassified, *, retrospective_actual_plan=False,
          open_classification=None):
    """Build a deterministic plan hierarchy without guessing category meaning."""
    roots = {}

    def node(container, key, label, level, kind=None, attention=False):
        if key not in container:
            container[key] = {
                "key": key, "label": label, "level": level, "kind": kind,
                "planned": Decimal("0.00"), "actual": Decimal("0.00"),
                "remaining": Decimal("0.00"), "transaction_count": 0,
                "attention": False, "transaction_keys": set(), "children": {},
            }
        return container[key]

    def add_chain(kind, owner, group, item_id, label, planned, actual, transaction_keys,
                  attention):
        root_key = "income" if kind == "income" else "expense"
        root_label = "Einnahmen" if kind == "income" else "Ausgaben"
        root = node(roots, root_key, root_label, "kind", root_key)
        owner_node = node(root["children"], f"owner:{owner}", owner, "owner", kind)
        group_node = node(owner_node["children"], f"group:{group}", group, "group", kind)
        item_node = node(group_node["children"], f"item:{item_id}", label, "item", kind, attention)
        for current in (item_node, group_node, owner_node, root):
            current["planned"] += planned
            current["actual"] += actual
            current["remaining"] += planned - actual
            current["transaction_keys"].update(transaction_keys)
            current["attention"] |= attention

    for row in rows:
        if money(row["planned"]) == 0 and money(row["actual"]) != 0:
            group = "Sonstiges ohne Budget"
        elif row["kind"] == "income":
            group = "Einnahmen"
        elif row["kind"] == "fixed":
            group = "Feste Ausgaben" if row["confirmed"] else "Feste Schätzungen"
        else:
            group = "Steuerbare Budgets"
        add_chain(row["kind"], row["owner_group"], group, row["item_id"], row["label"],
                  money(row["planned"]), money(row["actual"]),
                  row.get("tree_transaction_keys", set()),
                  not row["confirmed"]
                  or (row["kind"] != "income" and money(row["remaining"]) < 0))

    for key, title, amount in (
            ("income", "Nicht zugeordnet", money(unmapped["income"])),
            ("expense", "Nicht zugeordnet", money(unmapped["expenses"]))):
        if amount:
            group = ("Sonstiges im Monatsrückblick" if retrospective_actual_plan
                     else "Sonstiges ohne Budget")
            add_chain(key, "Weitere", group, f"unmapped:{key}", title,
                      amount if retrospective_actual_plan else Decimal("0.00"), amount,
                      unmapped.get("income_keys" if key == "income" else "expense_keys", set()),
                      not retrospective_actual_plan)

    for direction, kind in (("income", "income"), ("expenses", "expense")):
        amount = money((open_classification or {}).get(direction, "0.00"))
        if amount:
            keys = (open_classification or {}).get(f"{direction}_keys", set())
            add_chain(kind, "Weitere", "Sonstiges / Klassifikation offen",
                      f"open-classification:{direction}", "Klassifikation offen",
                      amount if retrospective_actual_plan else Decimal("0.00"), amount,
                      keys, True)

    if unclassified["count"] and not retrospective_actual_plan:
        amount = money(unclassified["net"])
        open_node = node(roots, "unclassified", "Noch nicht eingeordnet", "kind", "unclassified")
        owner_node = node(open_node["children"], "owner:unclassified", "Weitere", "owner", "unclassified")
        group_node = node(owner_node["children"], "group:unclassified", "Unklassifizierte Buchungen",
                          "group", "unclassified")
        item_node = node(group_node["children"], "item:unclassified", "Unklassifizierte Buchungen",
                         "item", "unclassified", True)
        for current in (item_node, group_node, owner_node, open_node):
            current["actual"] += amount
            current["remaining"] -= amount
            current["transaction_count"] = unclassified["count"]
            current["attention"] = True

    def finish(current):
        if current["transaction_keys"]:
            current["transaction_count"] = len(current["transaction_keys"])
        current.pop("transaction_keys")
        current["planned"] = _format(current["planned"])
        current["actual"] = _format(current["actual"])
        current["remaining"] = _format(current["remaining"])
        children = sorted(current.pop("children").values(), key=lambda child: child["label"])
        current["children"] = [finish(child) for child in children]
        return current

    order = {"income": 0, "expense": 1, "unclassified": 2}
    return [finish(current) for current in sorted(
        roots.values(), key=lambda item: (order.get(item["kind"], 99), item["label"]))]


def _daily_owner(owner, known_person_ids):
    return owner if owner in known_person_ids and owner != "JOINT" else "JOINT"


def _cashflow_plan_label(label, owner, owner_label=None):
    """Remove supported estimate/owner prefixes only at explicit boundaries."""
    label = label.strip()
    estimate_prefix = "schätzung · "
    if label.casefold().startswith(estimate_prefix):
        label = label[len(estimate_prefix):].strip()
    for prefix in (owner_label, owner):
        if not prefix:
            continue
        prefix_key = prefix.strip().casefold()
        for delimiter in (" · ", ": ", " - "):
            head, separator, tail = label.partition(delimiter)
            if separator and head.strip().casefold() == prefix_key:
                label = tail.strip()
                break
    return label.casefold().strip()


def _daily_series(month, ledger_rows, month_plan, planned_by_item, snapshot, *, today=None,
                  account_owners=None, known_person_ids=None, person_ids=None):
    """Return cumulative ledger and plan curves grouped by whole account owner."""
    year, month_number = (int(part) for part in month.split("-"))
    start = date(year, month_number, 1)
    end = date(year, month_number, calendar.monthrange(year, month_number)[1])
    today = today or datetime.now().astimezone().date()
    as_of = min(today, end)
    known_person_ids = set(known_person_ids if known_person_ids is not None
                           else set())
    person_ids = list(dict.fromkeys(
        [*(person_ids or []), *sorted(known_person_ids)]))
    person_ids = [person for person in person_ids if person != "JOINT"]
    person_ids.append("JOINT")
    if as_of < start:
        return {"as_of": None, "today": today.isoformat(), "points": [],
                "points_by_person": {person: [] for person in person_ids},
                "basis": {"matched_due_count": 0, "linearized_amount": "0.00",
                          "matched_expense_due_count": 0,
                          "linearized_expense_amount": "0.00",
                          "matched_income_due_count": 0,
                          "linearized_income_amount": "0.00",
                          "unmatched_expense_plan_is_time_prorated": True,
                          "unmatched_income_plan_is_time_prorated": True}}

    actual_daily_by_person = {person: {} for person in person_ids}
    account_owners = account_owners or {}

    def owner_for_account(account_id):
        return _daily_owner(account_owners.get(account_id), known_person_ids)

    for row in ledger_rows:
        if row["actual_state"] != "classified":
            continue
        day = date.fromisoformat(row["date"])
        if day > as_of:
            continue
        amount = _positive_actual(row)
        field = "income" if row["transaction_type"] == "income" else "expenses"
        person = owner_for_account(row.get("account_id"))
        person_daily = actual_daily_by_person[person].setdefault(
            day, {"income": Decimal("0.00"), "expenses": Decimal("0.00")})
        person_daily[field] += amount

    # payday cashflows are the only existing source of dated commitments. A
    # match requires one unique label+amount pair; all other planned amounts
    # are spread evenly over calendar days and are marked as estimates below.
    # Older saved calculations did not persist per-month item details.  The
    # selected ``planned_by_item`` set is still authoritative; use it to pick
    # the matching definitions from the immutable plan instead of drawing a
    # misleading zero plan curve.
    definitions = {item["id"]: item for item in snapshot["plan"].get("items", [])}
    definitions.update({item["id"]: item for item in month_plan.get("items", [])})
    items = {item_id: definitions[item_id] for item_id in planned_by_item
             if item_id in definitions}
    possible = {}
    cycle_owner_labels = {}
    for cycle in snapshot["plan"].get("payday_cycles", []):
        if cycle.get("owner_label"):
            cycle_owner_labels.setdefault(cycle["account_id"], set()).add(cycle["owner_label"])
    for cycle in snapshot["plan"].get("payday_cycles", []):
        account_id = cycle["account_id"]
        for flow in cycle["cashflows"]:
            owner = account_owners.get(account_id)
            owner_labels = cycle_owner_labels.get(account_id, set())
            owner_label = next(iter(owner_labels)) if len(owner_labels) == 1 else None
            key = (account_id, flow["direction"], _cashflow_plan_label(
                flow["label"], owner, owner_label), money(flow["amount"]))
            possible.setdefault(key, []).append(flow)
    planned_items = {}
    for item_id, planned in planned_by_item.items():
        item = items.get(item_id)
        if item is None or planned <= 0:
            continue
        category = "income" if item["kind"] == "income" else "expenses"
        person = owner_for_account(item.get("account_id"))
        planned_items[item_id] = (item, planned, category, person)

    item_candidates = {}
    for item_id, (item, planned, category, person) in planned_items.items():
        account_id = item.get("account_id")
        if not account_id:
            continue
        direction = "inflow" if category == "income" else "outflow"
        owner = account_owners.get(account_id)
        owner_labels = cycle_owner_labels.get(account_id, set())
        owner_label = next(iter(owner_labels)) if len(owner_labels) == 1 else None
        key = (account_id, direction,
               _cashflow_plan_label(item["label"], owner, owner_label), planned)
        item_candidates.setdefault(key, []).append(item_id)

    scheduled = {person: {"income": {}, "expenses": {}}
                 for person in person_ids}
    matched_items = {"income": set(), "expenses": set()}
    matched_due_count = {"income": 0, "expenses": 0}
    for key, flow_list in possible.items():
        account_id, direction, _, amount = key
        field = "income" if direction == "inflow" else "expenses"
        ids = item_candidates.get(key, [])
        if len(flow_list) == 1 and len(ids) == 1:
            flow = flow_list[0]
            due = date.fromisoformat(flow["due_date"])
            if due.year == year and due.month == month_number:
                person = planned_items[ids[0]][3]
                scheduled[person][field].setdefault(due, Decimal("0.00"))
                scheduled[person][field][due] += amount
                matched_items[field].add(ids[0])
                matched_due_count[field] += 1

    # Salary cycles do not contain inflow cashflows. Use a cycle's salary date
    # only if exactly one positive planned income item belongs to that account.
    income_items_by_account = {}
    for item_id, (item, planned, category, person) in planned_items.items():
        account_id = item.get("account_id")
        if category == "income" and account_id:
            income_items_by_account.setdefault(account_id, []).append(item_id)
    for cycle in snapshot["plan"].get("payday_cycles", []):
        account_id = cycle["account_id"]
        ids = income_items_by_account.get(account_id, [])
        salary_dates = [cycle.get(key) for key in ("last_salary_date", "next_salary_date")
                        if cycle.get(key)]
        salary_dates = [date.fromisoformat(value) for value in salary_dates
                        if start <= date.fromisoformat(value) <= end]
        if len(ids) != 1 or len(set(salary_dates)) != 1:
            continue
        item_id = ids[0]
        if item_id in matched_items["income"]:
            continue
        item, amount, _, person = planned_items[item_id]
        due = salary_dates[0]
        scheduled[person]["income"].setdefault(due, Decimal("0.00"))
        scheduled[person]["income"][due] += amount
        matched_items["income"].add(item_id)
        matched_due_count["income"] += 1

    linearized = {person: {"income": Decimal("0.00"), "expenses": Decimal("0.00")}
                  for person in person_ids}
    for item_id, (_, amount, category, person) in planned_items.items():
        field = "income" if category == "income" else "expenses"
        if item_id not in matched_items[field]:
            linearized[person][field] += amount

    days_in_month = end.day
    daily_amounts = {}
    for person in scheduled:
        daily_amounts[person] = {}
        for field in ("income", "expenses"):
            daily_cents, extra_cents = divmod(int(linearized[person][field] * 100), days_in_month)
            daily_amounts[person][field] = (daily_cents, extra_cents)
    cumulative_actual = {person: {"income": Decimal("0.00"), "expenses": Decimal("0.00")}
                         for person in scheduled}
    cumulative_planned = {person: {"income": Decimal("0.00"), "expenses": Decimal("0.00")}
                          for person in scheduled}
    points_by_person = {person: [] for person in scheduled}
    for day_number in range(1, as_of.day + 1):
        current = date(year, month_number, day_number)
        for person in scheduled:
            actual = actual_daily_by_person[person].get(current, {})
            for field in ("income", "expenses"):
                cumulative_actual[person][field] += actual.get(field, Decimal("0.00"))
                daily_cents, extra_cents = daily_amounts[person][field]
                cumulative_planned[person][field] += scheduled[person][field].get(
                    current, Decimal("0.00"))
                cumulative_planned[person][field] += Decimal(
                    daily_cents + (day_number <= extra_cents)) / 100
            points_by_person[person].append({
                "date": current.isoformat(),
                "cumulative_expenses": _format(cumulative_actual[person]["expenses"]),
                "cumulative_income": _format(cumulative_actual[person]["income"]),
                "planned_cumulative_expenses": _format(cumulative_planned[person]["expenses"]),
                "planned_cumulative_income": _format(cumulative_planned[person]["income"]),
            })
    points = []
    date_person = person_ids[0]
    for index in range(as_of.day):
        points.append({
            "date": points_by_person[date_person][index]["date"],
            **{key: _format(sum((Decimal(points_by_person[person][index][key])
                                 for person in points_by_person), Decimal("0.00")))
               for key in ("cumulative_expenses", "cumulative_income",
                           "planned_cumulative_expenses", "planned_cumulative_income")},
        })
    linearized_total = {field: sum((values[field] for values in linearized.values()),
                                   Decimal("0.00"))
                        for field in ("income", "expenses")}
    return {
        "as_of": as_of.isoformat(), "today": today.isoformat(), "points": points,
        "points_by_person": points_by_person,
        "basis": {"matched_due_count": matched_due_count["expenses"],
                  "linearized_amount": _format(linearized_total["expenses"]),
                  "matched_expense_due_count": matched_due_count["expenses"],
                  "linearized_expense_amount": _format(linearized_total["expenses"]),
                  "matched_income_due_count": matched_due_count["income"],
                  "linearized_income_amount": _format(linearized_total["income"]),
                  "unmatched_expense_plan_is_time_prorated": True,
                  "unmatched_income_plan_is_time_prorated": True,
                  "unmatched_fixed_and_variable_are_time_prorated": True},
    }


def _set_daily_plan_to_classified_actual(daily):
    """Make a retrospective Plan curve follow each classified booking date."""
    for points in daily["points_by_person"].values():
        for point in points:
            point["planned_cumulative_income"] = point["cumulative_income"]
            point["planned_cumulative_expenses"] = point["cumulative_expenses"]
    for index, point in enumerate(daily["points"]):
        point["planned_cumulative_income"] = _format(sum(
            (Decimal(values[index]["planned_cumulative_income"])
             for values in daily["points_by_person"].values()), Decimal("0.00")))
        point["planned_cumulative_expenses"] = _format(sum(
            (Decimal(values[index]["planned_cumulative_expenses"])
             for values in daily["points_by_person"].values()), Decimal("0.00")))


def _hide_unavailable_plan(comparison):
    """Keep actual matching, but publish no plan inferred from a later revision."""
    for key in comparison["totals"]:
        if key.startswith(("planned_", "remaining_")):
            comparison["totals"][key] = None
    for row in comparison["rows"]:
        for key in ("planned", "remaining", "variance"):
            row[key] = None
        for key in ("planned_by_person", "variance_by_person"):
            if key in row:
                row[key] = dict.fromkeys(row[key], None)

    def hide_tree(nodes):
        for node in nodes:
            node["planned"] = None
            node["remaining"] = None
            hide_tree(node["children"])
            # Do not retain an over-budget flag computed from the later plan.
            node["attention"] = (node["kind"] == "unclassified"
                                 or "open-classification" in node["key"]
                                 or any(child["attention"] for child in node["children"]))

    hide_tree(comparison["tree"])
    for point in comparison["daily"]:
        point["planned_cumulative_income"] = None
        point["planned_cumulative_expenses"] = None
    for points in comparison["daily_by_person"].values():
        for point in points:
            point["planned_cumulative_income"] = None
            point["planned_cumulative_expenses"] = None
    comparison["daily_metadata"]["basis"] = None
    comparison["surplus_bridge"] = None
    if "person_breakdown" in comparison:
        breakdown = comparison["person_breakdown"]
        breakdown["planned_basis"] = "Kein gespeicherter Monatsplan"
        for key, values in breakdown["totals"].items():
            if key.startswith("planned_"):
                breakdown["totals"][key] = dict.fromkeys(values, None)
    # Both widgets contain assumptions from the selected revision. Independent
    # booked account balances remain in account_balance_change.
    comparison["liquidity"] = None
    comparison["payday"] = None


def _liquidity_view(store, snapshot, month, comparison_rows):
    """Separate checking-account liquidity from adjustable budget headroom."""
    account_ids = snapshot["plan"].get("liquidity_accounts", [])
    if not account_ids:
        return None
    year, month_number = (int(part) for part in month.split("-"))
    period_start = date(year, month_number, 1)
    today = datetime.now().astimezone().date()
    if period_start > today:
        return {"available": False, "reason": "future_month", "accounts": []}
    period_end = date(year, month_number, calendar.monthrange(year, month_number)[1])
    as_of = min(today, period_end).isoformat()
    balances = _account_balances(store, as_of, account_ids)
    if balances is None:
        return {"available": False, "reason": "before_opening_balances", "accounts": []}
    pending_by_account = {account_id: Decimal("0.00") for account_id in account_ids}
    for transfer in snapshot["plan"].get("pending_transfers", []):
        transfer_date = date.fromisoformat(transfer["value_date"])
        if not period_start <= transfer_date <= period_end or transfer_date < date.fromisoformat(as_of):
            continue
        amount = money(transfer["amount"])
        if transfer["from_account_id"] in pending_by_account:
            pending_by_account[transfer["from_account_id"]] -= amount
        if transfer["to_account_id"] in pending_by_account:
            pending_by_account[transfer["to_account_id"]] += amount
    row_by_item = {row["item_id"]: row for row in comparison_rows}
    account_rows = {row["id"]: row for row in store.db.execute(
        f"SELECT id,display_name,institution,owner FROM accounts WHERE id IN "
        f"({','.join('?' for _ in account_ids)})", account_ids
    )}
    result = []
    tracked_items = set()
    for account_id in account_ids:
        expected_income = Decimal("0.00")
        estimated_income = Decimal("0.00")
        committed_expenses = Decimal("0.00")
        estimated_expenses = Decimal("0.00")
        flexible_budget = Decimal("0.00")
        commitments = []
        estimates = []
        income_items = []
        for item in snapshot["plan"]["items"]:
            if item.get("account_id") != account_id or item["id"] not in row_by_item:
                continue
            tracked_items.add(item["id"])
            row = row_by_item[item["id"]]
            remaining = max(money(row["remaining"]), Decimal("0.00"))
            if item["kind"] == "income":
                if item["confirmed"]:
                    expected_income += remaining
                    if remaining:
                        income_items.append({"item_id": item["id"], "label": item["label"],
                                             "amount": _format(remaining)})
                else:
                    estimated_income += remaining
                    if remaining:
                        estimates.append({"item_id": item["id"], "label": item["label"],
                                          "amount": _format(remaining), "kind": "income"})
            elif item["kind"] == "fixed":
                if item["confirmed"]:
                    committed_expenses += remaining
                    if remaining:
                        commitments.append({"item_id": item["id"], "label": item["label"],
                                            "amount": _format(remaining)})
                else:
                    estimated_expenses += remaining
                    if remaining:
                        estimates.append({"item_id": item["id"], "label": item["label"],
                                          "amount": _format(remaining), "kind": "expense"})
            else:
                flexible_budget += remaining
        balance = balances.get(account_id, Decimal("0.00"))
        pending_transfer = pending_by_account[account_id]
        account = account_rows.get(account_id)
        label = (account["display_name"] or f"{account['owner']} · {account['institution']}") \
            if account is not None else account_id
        result.append({
            "account_id": account_id,
            "label": label,
            "current_balance": _format(balance),
            "pending_transfer": _format(pending_transfer),
            "balance_after_pending": _format(balance + pending_transfer),
            "expected_income": _format(expected_income),
            "estimated_income": _format(estimated_income),
            "committed_expenses": _format(committed_expenses),
            "estimated_expenses": _format(estimated_expenses),
            "available_after_commitments": _format(
                balance + pending_transfer + expected_income - committed_expenses),
            "flexible_budget_remaining": _format(flexible_budget),
            "commitments": commitments, "estimates": estimates, "income_items": income_items,
        })
    unassigned = []
    for item in snapshot["plan"]["items"]:
        row = row_by_item.get(item["id"])
        if row is None or item["id"] in tracked_items:
            continue
        remaining = max(money(row["remaining"]), Decimal("0.00"))
        if remaining:
            unassigned.append({"item_id": item["id"], "label": item["label"],
                               "kind": item["kind"], "amount": _format(remaining)})
    flexible_total = sum(
        (max(money(row_by_item[item["id"]]["remaining"]), Decimal("0.00"))
         for item in snapshot["plan"]["items"]
         if item["kind"] == "variable" and item["id"] in row_by_item
         and (not item.get("account_id") or item["account_id"] in account_ids)),
        Decimal("0.00"),
    )
    totals = {
        key: _format(sum((money(account[key]) for account in result), Decimal("0.00")))
        for key in ("current_balance", "pending_transfer", "balance_after_pending",
                    "expected_income", "committed_expenses",
                    "estimated_income", "estimated_expenses", "available_after_commitments")
    }
    totals["flexible_budget_remaining"] = _format(flexible_total)
    return {
        "available": True, "as_of": as_of, "accounts": result,
        "totals": totals,
        "unassigned": unassigned,
    }


def _account_balances(store, as_of, account_ids):
    """Return balances for selected accounts without unrelated opening-date gates."""
    if not account_ids:
        return {}
    placeholders = ",".join("?" for _ in account_ids)
    accounts = store.db.execute(
        f"SELECT id,opening,opening_date FROM accounts WHERE id IN ({placeholders})",
        account_ids,
    ).fetchall()
    if len(accounts) != len(account_ids) or any(
            date.fromisoformat(row["opening_date"]) > date.fromisoformat(as_of)
            for row in accounts):
        return None
    balances = {row["id"]: money(row["opening"]) for row in accounts}
    for row in store.db.execute(
            f"SELECT account_id,amount FROM transactions WHERE date<=? "
            f"AND account_id IN ({placeholders})",
            (as_of, *account_ids)):
        balances[row["account_id"]] += money(row["amount"])
    return balances


def _reporting_checking_account_ids(reporting_scope, liquidity_account_ids):
    """Select historical balance accounts, falling back to scoped plan accounts."""
    selected = (reporting_scope or {}).get("balance_account_ids", liquidity_account_ids)
    return sorted(selected)


def _monthly_account_balance_change(store, month, account_ids, account_owners,
                                    known_person_ids, person_ids, *, today=None,
                                    opening_balances=None, reporting_scope=None):
    """Return opening/end balances and actual daily changes for selected checking accounts."""
    unavailable = {"available": False, "as_of": None, "by_person": {}}
    if not account_ids:
        unavailable["reason"] = (
            "balance_accounts_outside_month_scope"
            if (reporting_scope or {}).get("balance_accounts_excluded_by_scope")
            else "no_liquidity_accounts"
        )
        return unavailable
    year, month_number = (int(part) for part in month.split("-"))
    start = date(year, month_number, 1)
    end = date(year, month_number, calendar.monthrange(year, month_number)[1])
    today = today or datetime.now().astimezone().date()
    as_of = min(today, end)
    unavailable["as_of"] = as_of.isoformat()
    if as_of < start:
        unavailable["reason"] = "future_month"
        return unavailable
    coverage = (reporting_scope or {}).get("available_from_by_account", {})
    if any(account_id in coverage and start.isoformat() < coverage[account_id]
           for account_id in account_ids):
        unavailable["reason"] = "incomplete_source_coverage"
        return unavailable
    placeholders = ",".join("?" for _ in account_ids)
    accounts = list(store.db.execute(
        f"SELECT id,kind FROM accounts WHERE id IN ({placeholders})", account_ids))
    if len(accounts) != len(account_ids) or any(row["kind"] != "CHECKING" for row in accounts):
        unavailable["reason"] = "checking_account_unavailable"
        return unavailable
    opening_date = start.fromordinal(start.toordinal() - 1)
    start_balances = (opening_balances if opening_balances is not None else
                      _account_balances(store, opening_date.isoformat(), account_ids))
    if start_balances is None:
        unavailable["reason"] = "opening_balance_unavailable"
        return unavailable

    person_ids = [person for person in person_ids if person != "JOINT"] + ["JOINT"]
    actual_change = {person: Decimal("0.00") for person in person_ids}
    start_by_person = actual_change.copy()
    end_by_person = actual_change.copy()
    deltas_by_day = {}
    account_person = {
        row["id"]: _daily_owner(account_owners.get(row["id"]), known_person_ids)
        for row in accounts
    }
    end_balances = dict(start_balances)
    for account_id, balance in start_balances.items():
        start_by_person[account_person[account_id]] += balance
    for row in store.db.execute(
            f"SELECT account_id,date,amount FROM transactions WHERE date BETWEEN ? AND ? "
            f"AND account_id IN ({placeholders}) ORDER BY date,account_id",
            (start.isoformat(), as_of.isoformat(), *account_ids)):
        person = account_person[row["account_id"]]
        day = date.fromisoformat(row["date"])
        delta = money(row["amount"])
        actual_change[person] += delta
        end_balances[row["account_id"]] += delta
        day_values = deltas_by_day.setdefault(
            day, {owner: Decimal("0.00") for owner in person_ids})
        day_values[person] += delta
    for account_id, balance in end_balances.items():
        end_by_person[account_person[account_id]] += balance

    values = {"TOTAL": {"start": sum(start_by_person.values(), Decimal("0.00")),
                        "end": sum(end_by_person.values(), Decimal("0.00")),
                        "change": sum(actual_change.values(), Decimal("0.00"))}}
    for person in person_ids:
        values[person] = {"start": start_by_person[person], "end": end_by_person[person],
                          "change": actual_change[person]}
    cumulative = {person: Decimal("0.00") for person in person_ids}
    points = {person: [] for person in person_ids}
    for day_number in range(1, as_of.day + 1):
        current = date(year, month_number, day_number)
        for person in person_ids:
            cumulative[person] += deltas_by_day.get(current, {}).get(person, Decimal("0.00"))
            points[person].append({"date": current.isoformat(),
                                   "change": _format(cumulative[person])})
    points["TOTAL"] = [
        {"date": current["date"],
         "change": _format(sum((Decimal(points[person][index]["change"])
                                for person in person_ids), Decimal("0.00")))}
        for index, current in enumerate(points[person_ids[0]])
    ] if person_ids else []
    return {
        "available": True, "as_of": as_of.isoformat(), "by_person": {
            person: {key: _format(value) for key, value in summary.items()}
            | {"points": points[person]}
            for person, summary in values.items()
        },
        "opening_date": opening_date.isoformat(),
        "_account_end_balances": end_balances,
        "source": "Rohbuchungen der ausgewählten Girokonten einschließlich Umbuchungen",
    }


def _trend_snapshot_for_month(snapshot, month, selected_month, *, force=False):
    """Reuse uniquely configured due/payday days for retrospective months."""
    if month == selected_month and not force:
        return snapshot
    year, month_number = (int(part) for part in month.split("-"))
    last_day = calendar.monthrange(year, month_number)[1]
    plan = dict(snapshot["plan"])
    cycles = []
    for original in plan.get("payday_cycles", []):
        cycle = dict(original)
        cashflows = []
        for original_flow in cycle.get("cashflows", []):
            flow = dict(original_flow)
            try:
                due = date.fromisoformat(flow["due_date"])
            except (KeyError, TypeError, ValueError):
                cashflows.append(flow)
                continue
            flow["due_date"] = date(year, month_number, min(due.day, last_day)).isoformat()
            cashflows.append(flow)
        cycle["cashflows"] = cashflows
        salary_values = [cycle.get(key) for key in ("last_salary_date", "next_salary_date")]
        try:
            salary_dates = [date.fromisoformat(value) for value in salary_values if value]
        except (TypeError, ValueError):
            salary_dates = []
        if len(salary_dates) == 2 and salary_dates[0].day == salary_dates[1].day:
            salary_date = date(year, month_number, min(salary_dates[0].day, last_day)).isoformat()
            cycle["last_salary_date"] = salary_date
            cycle["next_salary_date"] = salary_date
        cycles.append(cycle)
    plan["payday_cycles"] = cycles
    return dict(snapshot) | {"plan": plan}


def _monthly_trend(store, selected_month, snapshot, account_owners,
                   known_person_ids, person_ids, *, today=None,
                   selected_ledger_rows=None, selected_daily=None,
                   selected_balance=None, available_from_month=None, reporting_history=None):
    """Build monthly totals from the selected revision and exact ledger rows."""
    calendar_year_start = f"{selected_month[:4]}-01"
    six_month_start = _budget_shift_month(selected_month, -5)
    first_month = min(calendar_year_start, six_month_start)
    months = []
    cursor = first_month
    while cursor <= selected_month:
        months.append(cursor)
        cursor = _budget_shift_month(cursor, 1)

    plan = snapshot["plan"]
    cash_item_id = plan.get("cash_receipt_item_id")
    allocations = _allocations(snapshot)
    saved_rows = {row["period"]: row for row in snapshot.get("calculation", {}).get("rows", [])}
    mapping = _mapping(store, snapshot)
    allocation_history = _allocation_history(store, snapshot)
    person_ids = [*sorted(set(person_ids) - {"JOINT"}), "JOINT"]
    rows = []
    daily_person_ids = ["TOTAL", *person_ids]
    balance_carry = None
    daily_points_by_person = {person: [] for person in daily_person_ids}
    previous_account_ids = None
    for month in months:
        scope = scope_for_month(store, month, reporting_history)
        month_snapshot = _scoped_plan(snapshot, scope)
        month_account_ids = _reporting_checking_account_ids(
            scope, month_snapshot["plan"].get("liquidity_accounts", []))
        if previous_account_ids != month_account_ids:
            balance_carry = None
        previous_account_ids = month_account_ids
        planned = {person: {"income": Decimal("0.00"), "expenses": Decimal("0.00")}
                   for person in person_ids}
        saved_row = saved_rows.get(month)
        if saved_row is not None:
            month_items = (saved_row["items"] if "items" in saved_row else
                           [item.copy() for item in plan.get("items", [])
                            if _item_active(item, month)])
            plan_source = "selected_revision" if month == selected_month else "saved_revision_projection"
        else:
            month_row, month_basis = _monthly_reference(snapshot, month, reporting_history)
            month_items = month_row["items"]
            plan_source = month_basis["plan_source"]
        month_items = _scope_items(month_items, scope)
        if plan_source != "reconstructed_from_monthly_actuals":
            for item in month_items:
                person = _daily_owner(account_owners.get(item.get("account_id")),
                                      known_person_ids)
                category = "income" if item["kind"] == "income" else "expenses"
                planned[person][category] += money(item["amount"])
        if saved_row is not None and "items" not in saved_row and scope["mode"] != "individual":
            # Old revisions retained authoritative totals but no item-level
            # projection. Attribute only the unexplained difference to JOINT.
            saved_totals = {"income": money(saved_row["income"]),
                            "expenses": money(saved_row["fixed"]) + money(saved_row["variable"])}
            for category, total in saved_totals.items():
                planned["JOINT"][category] += total - sum(
                    values[category] for values in planned.values())
        month_plan = {"items": month_items}
        month_candidates = _month_plan_candidates(snapshot, month_plan, month)

        actual = {person: {"income": Decimal("0.00"), "expenses": Decimal("0.00")}
                  for person in person_ids}
        actual_fixed = {person: {"income": Decimal("0.00"), "expenses": Decimal("0.00")}
                        for person in person_ids}
        if month == selected_month and selected_ledger_rows is not None:
            ledger_rows = selected_ledger_rows
        else:
            ledger_rows, _ = _ledger_rows(
                store, month, allocations, cash_item_id, reporting_scope=scope)
        if plan_source == "reconstructed_from_monthly_actuals":
            _include_open_historical_rows(ledger_rows)
        for transaction in ledger_rows:
            if transaction["actual_state"] != "classified":
                continue
            person = _daily_owner(account_owners.get(transaction.get("account_id")),
                                  known_person_ids)
            category = "income" if transaction["transaction_type"] == "income" else "expenses"
            actual[person][category] += _positive_actual(transaction)
            _, fixed_income, fixed_expenses, _, _ = _historical_actual_parts(
                store, mapping, snapshot, transaction, allocation_history,
                month_candidates, allocations)
            actual_fixed[person]["income"] += fixed_income
            actual_fixed[person]["expenses"] += fixed_expenses
        actual_other_income = {
            person: actual[person]["income"] - actual_fixed[person]["income"]
            for person in person_ids
        }
        actual_other_expenses = {
            person: actual[person]["expenses"] - actual_fixed[person]["expenses"]
            for person in person_ids
        }
        if plan_source == "reconstructed_from_monthly_actuals":
            planned = {person: values.copy() for person, values in actual.items()}

        planned_by_item = ({item["id"]: money(item["amount"]) for item in month_items}
                           if plan_source != "no_saved_budget" else {})
        if plan_source != "no_saved_budget":
            month_snapshot = _trend_snapshot_for_month(month_snapshot, month, selected_month)
        daily = (selected_daily
                 if (month == selected_month and selected_daily is not None
                 and plan_source != "reconstructed_from_monthly_actuals")
                 else _daily_series(
                     month, ledger_rows,
                     month_plan if plan_source != "no_saved_budget" else {"items": []},
                     planned_by_item, month_snapshot,
                     today=today, account_owners=account_owners,
                     known_person_ids=known_person_ids, person_ids=person_ids))
        if plan_source == "reconstructed_from_monthly_actuals":
            _set_daily_plan_to_classified_actual(daily)

        balance = (dict(selected_balance)
                   if month == selected_month and selected_balance is not None
                   else _monthly_account_balance_change(
                       store, month, month_account_ids, account_owners, known_person_ids,
                       person_ids, today=today, opening_balances=balance_carry,
                       reporting_scope=scope))
        balance_carry = balance.get("_account_end_balances") if balance["available"] else None
        _scope_person_values(daily["points_by_person"], scope)
        _scope_person_values(balance["by_person"], scope)
        for values in (planned, actual, actual_fixed, actual_other_income, actual_other_expenses):
            _scope_person_values(values, scope)
        daily_sources = {"TOTAL": daily["points"], **daily["points_by_person"]}
        for person in daily_person_ids:
            previous = {"cumulative_income": Decimal("0.00"),
                        "cumulative_expenses": Decimal("0.00"),
                        "planned_cumulative_income": Decimal("0.00"),
                        "planned_cumulative_expenses": Decimal("0.00"),
                        "balance": Decimal("0.00")}
            balance_points = balance.get("by_person", {}).get(person, {}).get("points", [])
            for index, point in enumerate(daily_sources.get(person, [])):
                values = {
                    "actual_income": Decimal(point["cumulative_income"]),
                    "actual_expenses": Decimal(point["cumulative_expenses"]),
                    "planned_income": Decimal(point["planned_cumulative_income"]),
                    "planned_expenses": Decimal(point["planned_cumulative_expenses"]),
                }
                balance_value = (Decimal(balance_points[index]["change"])
                                 if balance["available"] and index < len(balance_points)
                                 else None)
                daily_point = {
                    "date": point["date"],
                    "actual_income_delta": _format(values["actual_income"] - previous["cumulative_income"]),
                    "actual_expenses_delta": _format(values["actual_expenses"] - previous["cumulative_expenses"]),
                    "planned_income_delta": _format(values["planned_income"] - previous["planned_cumulative_income"]),
                    "planned_expenses_delta": _format(values["planned_expenses"] - previous["planned_cumulative_expenses"]),
                    "balance_change_delta": (_format(balance_value - previous["balance"])
                                              if balance_value is not None else None),
                }
                daily_points_by_person[person].append(daily_point)
                previous.update({
                    "cumulative_income": values["actual_income"],
                    "cumulative_expenses": values["actual_expenses"],
                    "planned_cumulative_income": values["planned_income"],
                    "planned_cumulative_expenses": values["planned_expenses"],
                })
                if balance_value is not None:
                    previous["balance"] = balance_value
        rows.append({
            "month": month, "reporting_scope": scope,
            "data_quality": month_quality(
                month, scope, unclassified_count=sum(
                    row["actual_state"] == "unclassified"
                    or row.get("historical_classification_open", False) for row in ledger_rows)),
            "plan": {"TOTAL": {
                "income": _format(sum((value["income"] for value in planned.values()),
                                      Decimal("0.00"))),
                "expenses": _format(sum((value["expenses"] for value in planned.values()),
                                        Decimal("0.00"))),
            }, **{person: {key: _format(value) for key, value in values.items()}
                 for person, values in planned.items()}},
            "actual": {"TOTAL": {
                "income": _format(sum((value["income"] for value in actual.values()),
                                      Decimal("0.00"))),
                "expenses": _format(sum((value["expenses"] for value in actual.values()),
                                        Decimal("0.00"))),
            }, **{person: {key: _format(value) for key, value in values.items()}
                 for person, values in actual.items()}},
            "actual_fixed": {"TOTAL": {
                key: _format(sum((value[key] for value in actual_fixed.values()),
                                 Decimal("0.00")))
                for key in ("income", "expenses")
            }, **{person: {key: _format(value) for key, value in values.items()}
                 for person, values in actual_fixed.items()}},
            "actual_other_expenses": {
                "TOTAL": _format(sum(actual_other_expenses.values(), Decimal("0.00"))),
                **{person: _format(value) for person, value in actual_other_expenses.items()},
            },
            "actual_other_income": {
                "TOTAL": _format(sum(actual_other_income.values(), Decimal("0.00"))),
                **{person: _format(value) for person, value in actual_other_income.items()},
            },
            "balance_available": balance["available"],
            "balance_change": {
                person: summary["change"] for person, summary in balance.get(
                    "by_person", {}).items()},
            "plan_source": plan_source,
            "plan_available": plan_source != "no_saved_budget",
        })
        if plan_source == "no_saved_budget":
            # The copied positions above only support classification of actuals.
            # A later revision supplies no publishable plan for this month.
            for values in rows[-1]["plan"].values():
                values["income"] = None
                values["expenses"] = None
            for person_points in daily_points_by_person.values():
                for point in person_points:
                    if point["date"].startswith(month):
                        point["planned_income_delta"] = None
                        point["planned_expenses_delta"] = None
        balance.pop("_account_end_balances", None)
    _scope_person_values(daily_points_by_person, scope)
    balance_availability = {
        "month": bool(rows and rows[-1]["balance_available"]),
        "3": all(row["balance_available"] for row in rows[-3:]),
        "6": all(row["balance_available"] for row in rows[-6:]),
        "year": all(row["balance_available"] for row in rows
                    if row["month"].startswith(selected_month[:4] + "-")),
    }
    return {
        "selected_month": selected_month,
        "reporting_history": (validate_reporting_history(reporting_history)
                              if reporting_history is not None else None),
        "revision": snapshot["revision"],
        "months": rows,
        "daily_points_by_person": daily_points_by_person,
        "daily_balance_available": balance_availability["month"],
        "daily_balance_available_by_range": balance_availability,
        "available_from_month": (available_from_month or
                                 _available_from_month(store, selected_month)),
        "historical_plan_source": ("reconstructed_from_monthly_actuals"
                                   if reporting_history is None or
                                   "retrospective_actual_years" not in reporting_history else
                                   "year_specific_budget_reference"),
        "note": ("Historische Monatspläne vor gespeicherten Monaten entsprechen den klassifizierten Ist-Buchungen. Die Zusammensetzung fester und sonstiger Einnahmen und Ausgaben wird getrennt ausgewiesen."
                 if reporting_history is None or
                 "retrospective_actual_years" not in reporting_history else
                  "Nur freigegebene Rückblickjahre verwenden Ist als Planreferenz; Monate vor der gewählten Revision ohne gespeicherten Plan zeigen ausschließlich Ist-Werte."),
    }


def _available_from_month(store, selected_month):
    """Return the first month with transactions in the EUR plan-Ist ledger."""
    first_import = store.db.execute(
        "SELECT MIN(substr(date,1,7)) FROM transactions WHERE currency='EUR'",
    ).fetchone()[0]
    return first_import or f"{selected_month[:4]}-01"


def metadata(store, *, reporting_history=None):
    """Return Plan-Ist bootstrap metadata without requiring a selected month."""
    first_import = store.db.execute(
        "SELECT MIN(substr(date,1,7)) FROM transactions WHERE currency='EUR'",
    ).fetchone()[0]
    result = {"available_from_month": first_import}
    if reporting_history is not None:
        result["reporting_history"] = validate_reporting_history(reporting_history)
    return result


def _payday_view(store, snapshot, month):
    """Project configured cashflows until each checking account's next salary."""
    cycles = snapshot["plan"].get("payday_cycles", [])
    if not cycles:
        return None
    year, month_number = (int(part) for part in month.split("-"))
    period_end = date(year, month_number, calendar.monthrange(year, month_number)[1])
    as_of_date = min(datetime.now().astimezone().date(), period_end)
    balances = _account_balances(
        store, as_of_date.isoformat(), [cycle["account_id"] for cycle in cycles])
    if balances is None:
        return None
    result = []
    for cycle in cycles:
        last_salary = date.fromisoformat(cycle["last_salary_date"])
        next_salary = date.fromisoformat(cycle["next_salary_date"])
        if not last_salary <= as_of_date <= next_salary:
            continue
        upcoming = [flow.copy() for flow in cycle["cashflows"]
                    if as_of_date <= date.fromisoformat(flow["due_date"]) < next_salary]
        for transfer in snapshot["plan"].get("pending_transfers", []):
            transfer_date = date.fromisoformat(transfer["value_date"])
            if not as_of_date <= transfer_date < next_salary:
                continue
            if transfer["from_account_id"] == cycle["account_id"]:
                upcoming.append({
                    "label": f"{transfer['label']} · vorgemerkt", "direction": "outflow",
                    "amount": transfer["amount"], "due_date": transfer["value_date"],
                    "evidence": transfer["evidence"],
                })
            elif transfer["to_account_id"] == cycle["account_id"]:
                upcoming.append({
                    "label": f"{transfer['label']} · vorgemerkt", "direction": "inflow",
                    "amount": transfer["amount"], "due_date": transfer["value_date"],
                    "evidence": transfer["evidence"],
                })
        upcoming.sort(key=lambda flow: (flow["due_date"], flow["label"]))
        inflows = sum((money(flow["amount"]) for flow in upcoming
                       if flow["direction"] == "inflow"), Decimal("0.00"))
        outflows = sum((money(flow["amount"]) for flow in upcoming
                        if flow["direction"] == "outflow"), Decimal("0.00"))
        current = balances.get(cycle["account_id"], Decimal("0.00"))
        target = money(cycle["target_balance"])
        projected = current + inflows - outflows
        result.append({
            "account_id": cycle["account_id"], "owner_label": cycle["owner_label"],
            "as_of": as_of_date.isoformat(),
            "last_salary_date": cycle["last_salary_date"],
            "next_salary_date": cycle["next_salary_date"],
            "next_salary_basis": cycle["next_salary_basis"],
            "current_balance": _format(current), "target_balance": _format(target),
            "upcoming_inflows": _format(inflows), "upcoming_outflows": _format(outflows),
            "projected_balance": _format(projected),
            "free_spendable": _format(max(projected - target, Decimal("0.00"))),
            "shortfall": _format(max(target - projected, Decimal("0.00"))),
            "cashflows": upcoming,
        })
    gross_free = sum((money(cycle["free_spendable"]) for cycle in result), Decimal("0.00"))
    gross_shortfall = sum((money(cycle["shortfall"]) for cycle in result), Decimal("0.00"))
    net_headroom = gross_free - gross_shortfall
    return {
        "available": bool(result), "as_of": as_of_date.isoformat(), "cycles": result,
        "total_free_spendable": _format(max(net_headroom, Decimal("0.00"))),
        "total_shortfall": _format(max(-net_headroom, Decimal("0.00"))),
    }


def _person_item_allocations(parts, item_amounts):
    """Keep both the booking's person totals and its existing item totals exact.

    Item amounts share one sign. Allocate each item from the remaining person
    cent budgets; the last item receives exactly the remaining cents.
    """
    remaining = {person: abs(value) for person, value in parts.items()}
    result = {}
    for item_id, amount in item_amounts:
        total = sum(remaining.values(), Decimal(0))
        weights = [{"item_id": person, "weight": value}
                   for person, value in remaining.items() if value > 0]
        allocated = (dict(_split_actual(amount, weights))
                     if total and amount else {})
        result[item_id] = allocated
        for person, value in allocated.items():
            remaining[person] -= abs(value)
    return result


def _person_comparison(store, snapshot, rows, ledger_rows, mapping, allocations,
                       allocation_history, month_candidates, people,
                       retrospective_actual_plan=False):
    labels = ({person["id"]: person["label"] for person in people}
              if people is not None else {row["id"]: person_label(row["id"])
                                          for row in store.db.execute(
                                              "SELECT id FROM persons ORDER BY id")})
    labels.pop("JOINT", None)
    ids = [*labels, 'JOINT']
    shares = account_allocations(store, labels)
    empty = lambda: dict.fromkeys(ids, Decimal("0.00"))

    def visible_shares(account_id):
        account_shares = shares.get(account_id, {None: Decimal(1)})
        return {person if person in labels or person == "JOINT" else "JOINT": share
                for person, share in account_shares.items()}

    actuals = {row["item_id"]: empty() for row in rows}
    unmapped = {"income": empty(), "expenses": empty()}
    for booking in ledger_rows:
        if booking["actual_state"] != "classified":
            continue
        amount = _positive_actual(booking)
        parts = split_cents(amount, visible_shares(booking["account_id"]))
        if booking.get("historical_classification_open"):
            kind = "income" if booking["transaction_type"] == "income" else "expenses"
            for person, part in parts.items():
                unmapped[kind][person] += part
            continue
        item_amounts, _, _ = _actual_item_allocations(
            store, mapping, snapshot, booking, allocation_history,
            month_candidates, allocations)
        if not item_amounts:
            kind = "income" if booking["transaction_type"] == "income" else "expenses"
            for person, part in parts.items():
                unmapped[kind][person] += part
            continue
        for item_id, distributed in _person_item_allocations(parts, item_amounts).items():
            # A zero-plan/zero-Ist row can be omitted from the existing display,
            # yet contain expense and refund postings with distinct ownership.
            bucket = actuals.setdefault(item_id, empty())
            for person, part in distributed.items():
                bucket[person] += part
    item_accounts = {item['id']: item.get('account_id') for item in snapshot['plan']['items']}
    totals = {name: empty() for name in (
        "planned_income", "planned_expenses", "actual_income", "actual_expenses")}
    formatted = lambda values: {person: _format(value) for person, value in values.items()}
    for row in rows:
        planned = empty()
        actual = actuals[row["item_id"]]
        if retrospective_actual_plan:
            planned.update(actual)
        else:
            parts = split_cents(money(row['planned']), visible_shares(
                item_accounts[row['item_id']]))
            planned.update(parts)
        row["planned_by_person"] = formatted(planned)
        row["actual_by_person"] = formatted(actual)
        row["variance_by_person"] = formatted({person: actual[person] - planned[person]
                                               for person in ids})
        kind = "income" if row["kind"] == "income" else "expenses"
        for person in ids:
            totals["planned_" + kind][person] += planned[person]
    # Aggregate all attributed bookings, including omitted net-zero positions.
    item_kinds = {item["id"]: item["kind"] for item in snapshot["plan"]["items"]}
    for item_id, actual in actuals.items():
        kind = "income" if item_kinds[item_id] == "income" else "expenses"
        for person in ids:
            totals["actual_" + kind][person] += actual[person]
    for kind, bucket in unmapped.items():
        for person in ids:
            totals["actual_" + kind][person] += bucket[person]
    if retrospective_actual_plan:
        # Net-zero items can be absent from rows while their bookings still
        # affect different people. The retrospective plan follows every actual.
        for kind in ("income", "expenses"):
            totals["planned_" + kind] = totals["actual_" + kind].copy()
    return {
        "planned_basis": ("Rückblick aus den importierten Ist-Buchungen" if retrospective_actual_plan
                          else "Zuordnung nach geplantem Konto"),
        "actual_basis": "Zuordnung nach Kontoinhaberschaft",
        "people": [{"id": person, "label": label} for person, label in labels.items()]
                  + [{"id": "JOINT", "label": "Gemeinsam"}],
        "totals": {name: formatted(values) for name, values in totals.items()},
        "unmapped_by_person": {kind: formatted(values) for kind, values in unmapped.items()},
    }


def _scoped_plan(snapshot, scope):
    """Filter projection references without changing immutable plan definitions."""
    if scope["mode"] != "individual":
        return snapshot
    allowed = set(scope["included_account_ids"])
    plan = dict(snapshot["plan"])
    plan["liquidity_accounts"] = [value for value in plan.get("liquidity_accounts", [])
                                  if value in allowed]
    plan["payday_cycles"] = [value for value in plan.get("payday_cycles", [])
                             if value["account_id"] in allowed]
    # A pending transfer contributes only its included account leg; the existing
    # liquidity/payday helpers already apply this account selection.
    plan.pop("household_split", None)
    return dict(snapshot, plan=plan)


def _scope_items(items, scope):
    if scope["mode"] != "individual":
        return items
    allowed = set(scope["included_account_ids"])
    return [item for item in items if item.get("account_id") in allowed]


def _scope_person_values(values, scope):
    if scope["mode"] == "individual":
        allowed = {"TOTAL", *scope["included_person_ids"]}
        for person in list(values):
            if person not in allowed:
                values.pop(person)
    return values


def _individual_item_label(item, account_owners, people):
    """Remove a current plan's owner prefix when showing an individual's history."""
    label = item["label"]
    owner = account_owners.get(item.get("account_id"))
    prefixes = {"JOINT", "Gemeinsam"}
    if owner:
        prefixes.update({owner, person_label(owner)})
        prefixes.update(person.get("label", person["id"]) for person in people
                        if person["id"] == owner)
    for prefix in sorted(prefixes, key=len, reverse=True):
        for delimiter in (" · ", ": ", " - ", " "):
            head = prefix + delimiter
            if label.casefold().startswith(head.casefold()):
                return label[len(head):].strip()
    return label


def _retrospective_item_label(transaction_keys, ledger_rows_by_key, people):
    """Name retrospective rows from their assigned, confirmed ledger categories."""
    labels = []
    owners = set()
    person_labels = {person["id"]: person.get("label", person["id"])
                     for person in people}
    indexed_rows = sorted(
        (ordinal, row)
        for key in transaction_keys
        for ordinal, row in ledger_rows_by_key.get(key, ())
    )
    for _, row in indexed_rows:
        if (row["actual_state"] != "classified"
                or row.get("historical_classification_open")
                or row.get("confirmed") != 1):
            continue
        label = row.get("category_label")
        if isinstance(label, str) and label.strip() and label not in labels:
            labels.append(label.strip())
        owner = row.get("account_owner")
        if owner:
            owners.add(owner)
    if not labels:
        return "Bestätigte Ist-Buchungen"
    label = " · ".join(labels)
    if len(owners) == 1:
        owner = next(iter(owners))
        owner_label = ("Gemeinsam" if owner == "JOINT" else
                       person_labels.get(owner) or person_label(owner))
        label = f"{owner_label} · {label}"
    return label


def compare_actual(store, data, *, people=None, reporting_history=None):
    """Compare one selected saved revision and month without mutating either source."""
    revision, month = _request(data)
    scope = scope_for_month(store, month, reporting_history)
    available_from_month = _available_from_month(store, month)
    snapshot, month_plan, basis = _snapshot(
        store, revision, month, available_from_month=available_from_month,
        reporting_history=reporting_history)
    scoped_snapshot = _scoped_plan(snapshot, scope)
    month_plan = dict(month_plan)
    if "items" in month_plan:
        month_plan["items"] = _scope_items(month_plan["items"], scope)
    mapping = _mapping(store, snapshot)
    allocations = _allocations(snapshot)
    allocation_history = _allocation_history(store, snapshot)
    month_candidates = _month_plan_candidates(snapshot, month_plan, month)
    actual_by_item = {item["id"]: Decimal("0.00") for item in snapshot["plan"]["items"]}
    retrospective_actual_plan = basis.get("type") == "retrospective_reference"
    planned_by_item = {
        item["id"]: (Decimal("0.00") if retrospective_actual_plan
                     else money(item["amount"]))
        for item in month_plan.get("items", [])
    }
    actual_counts = {item_id: 0 for item_id in actual_by_item}
    transaction_keys_by_item = {item_id: set() for item_id in actual_by_item}
    unmapped = {"income": Decimal("0.00"), "expenses": Decimal("0.00"), "count": 0}
    unmapped_warning_counts = {}
    unmapped_keys = {"income": set(), "expenses": set()}
    unclassified = {
        "net": Decimal("0.00"), "absolute": Decimal("0.00"), "count": 0,
    }
    open_classification = {"income": Decimal("0.00"), "expenses": Decimal("0.00")}
    open_classification_keys = {"income": set(), "expenses": set()}
    automatic_owner_mapping_count = 0
    cash_item_id = snapshot["plan"].get("cash_receipt_item_id")
    cash_withdrawals = []
    cash_receipt_by_item = {}
    ledger_rows, excluded = _ledger_rows(
        store, month, allocations, cash_item_id, reporting_scope=scope,
        cash_withdrawals=cash_withdrawals)
    ledger_rows_by_key = {}
    for ordinal, row in enumerate(ledger_rows):
        key = (row["account_id"], row["external_id"])
        ledger_rows_by_key.setdefault(key, []).append((ordinal, row))
    for row in ledger_rows:
        if row["actual_state"] == "unclassified":
            amount = money(row["amount"])
            unclassified["net"] += amount
            unclassified["absolute"] += abs(amount)
            unclassified["count"] += 1
    if retrospective_actual_plan:
        open_values, open_keys = _include_open_historical_rows(ledger_rows)
        open_classification["income"] = open_values["income"]
        open_classification["expenses"] = open_values["expenses"]
        open_classification_keys = open_keys
    for row in ledger_rows:
        amount = money(row["amount"])
        if row["actual_state"] == "unclassified":
            continue
        if row.get("historical_classification_open"):
            continue
        actual = _positive_actual(row)
        transaction_key = (row["account_id"], row["external_id"])
        item_allocations, owner_mapped, warning = _actual_item_allocations(
            store, mapping, snapshot, row, allocation_history, month_candidates,
            allocations)
        if not item_allocations:
            direction = "income" if row["transaction_type"] == "income" else "expenses"
            if row.get("historical_classification_open"):
                continue
            unmapped[direction] += actual
            unmapped_keys[direction].add(transaction_key)
            unmapped["count"] += 1
            if warning is not None:
                unmapped_warning_counts[warning] = unmapped_warning_counts.get(warning, 0) + 1
            continue
        automatic_owner_mapping_count += int(owner_mapped)
        for item_id, allocated_actual in item_allocations:
            if row.get("bonsy_entry_id") is not None:
                cash_receipt_by_item[item_id] = (
                    cash_receipt_by_item.get(item_id, Decimal("0.00")) + allocated_actual)
            actual_by_item[item_id] += allocated_actual
            actual_counts[item_id] += 1
            transaction_keys_by_item[item_id].add(transaction_key)
            if retrospective_actual_plan:
                planned_by_item[item_id] = planned_by_item.get(
                    item_id, Decimal("0.00")) + allocated_actual

    if "items" not in month_plan:  # Legacy calculations did not persist item details.
        planned_by_item = {
            item["id"]: money(item["amount"]) for item in snapshot["plan"]["items"]
            if _item_active(item, month)
        }
    if retrospective_actual_plan and "items" not in month_plan:
        planned_by_item = {item["id"]: Decimal("0.00")
                           for item in snapshot["plan"]["items"]}
    if scope["mode"] == "individual" and not retrospective_actual_plan:
        allowed_items = {item["id"] for item in _scope_items(snapshot["plan"]["items"], scope)}
        planned_by_item = {key: value for key, value in planned_by_item.items()
                           if key in allowed_items}
    account_owners = {row["id"]: row["owner"] for row in store.db.execute(
        "SELECT id,owner FROM accounts"
    )}
    configured_people = (list(people) if people is not None else [
        {"id": row["id"], "label": person_label(row["id"])}
        for row in store.db.execute("SELECT id FROM persons ORDER BY id")
    ])
    label_people = configured_people
    if scope["mode"] == "individual":
        configured_people = [person for person in configured_people
                             if person["id"] == scope["prior_person_id"]]
    known_person_ids = {person["id"] for person in configured_people}
    known_person_ids.discard("JOINT")
    daily_people = [{"id": "TOTAL", "label": "Gesamt"}, *[
        {"id": person_id, "label": next((person.get("label") for person in configured_people
                                           if person["id"] == person_id),
                                          None) or ("Gemeinsam" if person_id == "JOINT"
                                                    else person_label(person_id))}
        for person_id in [*sorted(known_person_ids), *([] if scope["mode"] == "individual"
                                                    else ["JOINT"])]
    ]]
    rows = []
    for item in snapshot["plan"]["items"]:
        planned = planned_by_item.get(item["id"], Decimal("0.00"))
        actual = actual_by_item[item["id"]]
        # With no saved plan, later-revision items having no actual booking
        # would be phantom budget rows, so show only items with actuals.
        if (planned == 0 and actual == 0
                or basis["type"] == "no_saved_budget" and actual == 0):
            continue
        rows.append({
            "item_id": item["id"],
            "label": (_retrospective_item_label(
                transaction_keys_by_item[item["id"]], ledger_rows_by_key, label_people)
                      if retrospective_actual_plan else
                      _individual_item_label(item, account_owners, label_people)
                      if scope["mode"] == "individual" else item["label"]),
            "kind": item["kind"],
            "confirmed": item["confirmed"],
            "owner_group": (
                scope["prior_person_id"] if scope["mode"] == "individual" else
                account_owners[item.get("account_id")]
                if account_owners.get(item.get("account_id")) in known_person_ids
                else "Gemeinsam"),
            "planned": _format(planned), "actual": _format(actual),
            "remaining": _format(planned - actual), "variance": _format(actual - planned),
            "transaction_count": actual_counts[item["id"]],
            "tree_transaction_keys": transaction_keys_by_item[item["id"]],
        })
    if retrospective_actual_plan:
        planned_income = (
            sum((planned_by_item.get(item["id"], Decimal("0.00"))
                 for item in snapshot["plan"]["items"] if item["kind"] == "income"),
                Decimal("0.00")) + unmapped["income"] + open_classification["income"])
        planned_expenses = (
            sum((planned_by_item.get(item["id"], Decimal("0.00"))
                 for item in snapshot["plan"]["items"] if item["kind"] != "income"),
                Decimal("0.00")) + unmapped["expenses"] + open_classification["expenses"])
    elif scope["mode"] == "individual":
        planned_income = sum((planned_by_item.get(item["id"], Decimal("0.00"))
                              for item in snapshot["plan"]["items"]
                              if item["kind"] == "income"), Decimal("0.00"))
        planned_expenses = sum((planned_by_item.get(item["id"], Decimal("0.00"))
                                for item in snapshot["plan"]["items"]
                                if item["kind"] != "income"), Decimal("0.00"))
    else:
        planned_income = Decimal(month_plan["income"])
        planned_expenses = Decimal(month_plan["fixed"]) + Decimal(month_plan["variable"])
    mapped_income = sum((actual_by_item[i["id"]] for i in snapshot["plan"]["items"]
                         if i["kind"] == "income"), Decimal("0.00"))
    mapped_expenses = sum((actual_by_item[i["id"]] for i in snapshot["plan"]["items"]
                           if i["kind"] != "income"), Decimal("0.00"))
    actual_income = mapped_income + unmapped["income"] + open_classification["income"]
    actual_expenses = mapped_expenses + unmapped["expenses"] + open_classification["expenses"]
    remaining_fixed = sum(
        (max(money(row["remaining"]), Decimal("0.00")) for row in rows
         if row["kind"] == "fixed" and row["confirmed"]), Decimal("0.00"))
    remaining_variable = sum(
        (max(money(row["remaining"]), Decimal("0.00")) for row in rows
         if row["kind"] == "variable"), Decimal("0.00"))
    remaining_estimated = sum(
        (max(money(row["remaining"]), Decimal("0.00")) for row in rows
         if row["kind"] == "fixed" and not row["confirmed"]), Decimal("0.00"))
    generated_at = datetime.now(UTC).isoformat()
    comparison = {
        "revision": revision, "month": month, "generated_at": generated_at,
        "basis": basis, "plan_reference": basis,
        "plan_available": basis["type"] != "no_saved_budget",
        "reporting_scope": scope,
        "audit_watermark": _watermark(store), "rows": rows,
        "totals": {
            "planned_income": _format(planned_income),
            "planned_expenses": _format(planned_expenses),
            "planned_cashflow": _format(planned_income - planned_expenses),
            "actual_income": _format(actual_income),
            "actual_expenses": _format(actual_expenses),
            "actual_cashflow": _format(actual_income - actual_expenses),
            "remaining_expense_budget": _format(planned_expenses - actual_expenses),
            "remaining_fixed_expenses": _format(remaining_fixed),
            "remaining_variable_budget": _format(remaining_variable),
            "remaining_estimated_expenses": _format(remaining_estimated),
        },
        "unmapped": {"income": _format(unmapped["income"]),
                     "expenses": _format(unmapped["expenses"]), "count": unmapped["count"]},
        "unclassified": {"net": _format(unclassified["net"]),
                         "absolute": _format(unclassified["absolute"]),
                         "count": unclassified["count"]},
        "automatic_owner_mapping_count": automatic_owner_mapping_count,
        "excluded": excluded,
        "cash_activity": _cash_activity(
            store, cash_withdrawals, ledger_rows, cash_receipt_by_item, known_person_ids),
    }
    if retrospective_actual_plan:
        comparison["retrospective_positions"] = historical_positions(
            ledger_rows, known_person_ids=known_person_ids)
        fixed_income = Decimal("0.00")
        fixed_expenses = Decimal("0.00")
        for row in ledger_rows:
            if row["actual_state"] != "classified":
                continue
            _, fixed_income_part, fixed_expense_part, _, _ = _historical_actual_parts(
                store, mapping, snapshot, row, allocation_history,
                month_candidates, allocations)
            fixed_income += fixed_income_part
            fixed_expenses += fixed_expense_part
        comparison["actual_composition"] = {
            "fixed_income": _format(fixed_income),
            "fixed_expenses": _format(fixed_expenses),
            "other_income": _format(actual_income - fixed_income),
            "other_expenses": _format(actual_expenses - fixed_expenses),
        }
    if unmapped_warning_counts:
        comparison["unmapped_warnings"] = [
            {"code": code, "count": count}
            for code, count in sorted(unmapped_warning_counts.items())
        ]
    bridge_unmapped = (dict(unmapped, income=Decimal("0.00"), expenses=Decimal("0.00"))
                       if retrospective_actual_plan else unmapped)
    bridge_unclassified = ({"net": Decimal("0.00"), "absolute": Decimal("0.00"),
                            "count": 0}
                           if retrospective_actual_plan else unclassified)
    comparison["surplus_bridge"] = (_surplus_bridge(
        rows, bridge_unmapped, bridge_unclassified, planned_income - planned_expenses,
        scoped_snapshot["plan"].get("household_split"))
        if comparison["plan_available"] else None)
    tree_unmapped = dict(unmapped) | {
        "income_keys": unmapped_keys["income"],
        "expense_keys": unmapped_keys["expenses"],
    }
    tree_open_classification = dict(open_classification) | {
        "income_keys": open_classification_keys["income"],
        "expenses_keys": open_classification_keys["expenses"],
    }
    daily_snapshot = (_trend_snapshot_for_month(
        scoped_snapshot, month, month, force=retrospective_actual_plan)
        if comparison["plan_available"] else scoped_snapshot)
    daily = _daily_series(month, ledger_rows,
                          month_plan if comparison["plan_available"] else {"items": []},
                          planned_by_item if comparison["plan_available"] else {},
                          daily_snapshot,
                          account_owners=account_owners, known_person_ids=known_person_ids,
                          person_ids=sorted(known_person_ids))
    if retrospective_actual_plan:
        _set_daily_plan_to_classified_actual(daily)
    _scope_person_values(daily["points_by_person"], scope)
    selected_daily = {"points": daily["points"],
                      "points_by_person": daily["points_by_person"]}
    selected_balance = _monthly_account_balance_change(
        store, month, _reporting_checking_account_ids(
            scope, scoped_snapshot["plan"].get("liquidity_accounts", [])), account_owners,
        known_person_ids, sorted(known_person_ids), reporting_scope=scope)
    _scope_person_values(selected_balance["by_person"], scope)
    comparison["tree"] = _tree(
        rows, tree_unmapped, unclassified,
        retrospective_actual_plan=retrospective_actual_plan,
        open_classification=tree_open_classification)
    for row in rows:
        row.pop("tree_transaction_keys", None)
    comparison["daily"] = daily.pop("points")
    comparison["daily_by_person"] = daily.pop("points_by_person")
    comparison["daily_people"] = daily_people
    comparison["daily_metadata"] = daily
    comparison["account_balance_change"] = {
        key: value for key, value in selected_balance.items()
        if not key.startswith("_")
    }
    comparison["available_from_month"] = available_from_month
    comparison["data_quality"] = month_quality(
        month, scope, unclassified_count=unclassified["count"])
    if data.get("include_trend"):
        comparison["monthly_trend"] = _monthly_trend(
            store, month, snapshot, account_owners, known_person_ids,
            sorted(known_person_ids), selected_ledger_rows=ledger_rows,
            selected_daily=selected_daily, selected_balance=selected_balance,
            available_from_month=available_from_month, reporting_history=reporting_history)
    comparison["liquidity"] = (_liquidity_view(store, scoped_snapshot, month, rows)
                               if comparison["plan_available"] else None)
    comparison["payday"] = (_payday_view(store, scoped_snapshot, month)
                            if comparison["plan_available"] else None)
    if data.get("person_breakdown"):
        comparison["person_breakdown"] = _person_comparison(
            store, snapshot, rows, ledger_rows, mapping, allocations,
            allocation_history, month_candidates, configured_people,
            retrospective_actual_plan=retrospective_actual_plan)
    if data.get("person_breakdown") and scope["mode"] == "individual":
        breakdown = comparison["person_breakdown"]
        breakdown["people"] = [person for person in breakdown["people"]
                               if person["id"] in scope["included_person_ids"]]
        for group in (breakdown["totals"], breakdown["unmapped_by_person"]):
            for values in group.values():
                _scope_person_values(values, scope)
        for row in rows:
            for key in ("planned_by_person", "actual_by_person", "variance_by_person"):
                if key in row:
                    _scope_person_values(row[key], scope)
    if comparison["payday"] is not None:
        free = money(comparison["payday"]["total_free_spendable"])
        comparison["payday"]["flexible_budget_remaining"] = _format(remaining_variable)
        comparison["payday"]["unallocated_after_budgets"] = _format(
            max(free - remaining_variable, Decimal("0.00")))
        comparison["payday"]["budget_shortfall"] = _format(
            max(remaining_variable - free, Decimal("0.00")))
    if reporting_history is None:
        comparison.pop("reporting_scope", None)
        if "monthly_trend" in comparison:
            comparison["monthly_trend"].pop("reporting_history", None)
            for row in comparison["monthly_trend"]["months"]:
                row.pop("reporting_scope", None)
    if not comparison["plan_available"]:
        _hide_unavailable_plan(comparison)
    return comparison


def actual_details(store, data, *, reporting_history=None):
    """Return only transactions belonging to the requested comparison context."""
    revision, month, context, page = _request(data, details=True)
    scope = scope_for_month(store, month, reporting_history)
    snapshot, month_plan, basis = _snapshot(
        store, revision, month, reporting_history=reporting_history)
    mapping = _mapping(store, snapshot)
    allocations = _allocations(snapshot)
    allocation_history = _allocation_history(store, snapshot)
    month_candidates = _month_plan_candidates(snapshot, month_plan, month)
    if context["type"] == "item" and context["item_id"] not in {
            item["id"] for item in snapshot["plan"]["items"]}:
        raise ValueError("unknown actual detail item")
    selected = []
    ledger_rows, _ = _ledger_rows(
        store, month, allocations, snapshot["plan"].get("cash_receipt_item_id"),
        reporting_scope=scope)
    for row in ledger_rows:
        mapped_items = {}
        if row["actual_state"] == "classified":
            mapped_items = dict(_actual_item_allocations(
                store, mapping, snapshot, row, allocation_history,
                month_candidates, allocations)[0])
        include = (
            (context["type"] == "item" and context["item_id"] in mapped_items)
            or (context["type"] == "unmapped" and row["actual_state"] == "classified"
                and not mapped_items)
            or (context["type"] == "unmapped_income" and row["actual_state"] == "classified"
                and not mapped_items and row["transaction_type"] == "income")
            or (context["type"] == "unmapped_expense" and row["actual_state"] == "classified"
                and not mapped_items and row["transaction_type"] == "expense")
            or (context["type"] == "unclassified" and row["actual_state"] == "unclassified")
        )
        if include:
            detail = {
                "account_id": row["account_id"], "external_id": row["external_id"],
                "date": row["date"], "amount": row["amount"],
                "counterparty": row["counterparty"] or "",
                "description": row["description"] or "",
                "category_id": row["category_id"],
                "category_label": row["category_label"],
            }
            if row.get("bonsy_entry_id") is not None:
                detail["bonsy_entry_id"] = row["bonsy_entry_id"]
                detail["bonsy_sources"] = row.get("bonsy_sources", [])
            if context["type"] == "item":
                detail["allocated_amount"] = _format(mapped_items[context["item_id"]])
            selected.append(detail)
    start = (page - 1) * _PAGE_SIZE
    total = len(selected)
    return {
        "revision": revision, "month": month, "context": context,
        "plan_available": basis["type"] != "no_saved_budget",
        "plan_reference": basis,
        "generated_at": datetime.now(UTC).isoformat(), "audit_watermark": _watermark(store),
        "page": page, "page_size": _PAGE_SIZE, "total_count": total,
        "has_more": start + _PAGE_SIZE < total,
        "transactions": selected[start:start + _PAGE_SIZE],
    }
