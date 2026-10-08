"""Read-only, per-account monthly movements and cautiously reconstructed balances."""

import calendar
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal

from .cash_components import explicit_cash_component
from .classification import canonical_category_id
from .core import money
from .transfer_corrections import sql_transfer_predicate


_ZERO = Decimal("0.00")


def _fmt(value):
    return format(value, ".2f")


def build_account_overview(store, month, snapshot, planned_by_item, *,
                           plan_available, reporting_scope, cash_activity,
                           item_allocations=None, today=None, include_cash=True):
    """Build account columns from raw postings; unknown balances stay unknown.

    A plan covers only active items explicitly assigned to the account. Cash is
    a movement record, never a reconstructed wallet balance or person balance.
    """
    year, number = (int(part) for part in month.split("-"))
    first = date(year, number, 1)
    last = date(year, number, calendar.monthrange(year, number)[1])
    as_of = min(today or datetime.now().astimezone().date(), last)
    scope = reporting_scope or {}
    selected = set(scope["included_account_ids"]) if "included_account_ids" in scope else None
    coverage = scope.get("available_from_by_account", {})
    accounts = store.db.execute(
        "SELECT id,owner,kind,currency,opening,opening_date,display_name "
        "FROM accounts ORDER BY id").fetchall()
    plan_items = snapshot.get("plan", {}).get("items", []) if snapshot else []
    items_by_id = {item["id"]: item for item in plan_items}
    item_allocations = item_allocations or {}
    assigned = defaultdict(list)
    if plan_available:
        for item in plan_items:
            account_id = item.get("account_id")
            if account_id and item["id"] in planned_by_item:
                assigned[account_id].append(item)
    transfers = defaultdict(list)
    if plan_available and snapshot:
        for transfer in snapshot.get("plan", {}).get("pending_transfers", []):
            if transfer["value_date"][:7] == month:
                transfers[transfer["from_account_id"]].append(("outflow", transfer))
                transfers[transfer["to_account_id"]].append(("inflow", transfer))

    result = []
    for account in accounts:
        account_id = account["id"]
        if selected is not None and account_id not in selected:
            continue
        opening_date = date.fromisoformat(account["opening_date"])
        coverage_date = date.fromisoformat(coverage[account_id]) if account_id in coverage else None
        future = as_of < first
        # A later complete month cannot repair a gap after the opening anchor.
        # The day immediately after that anchor is a continuous history start.
        incomplete = coverage_date is not None and (
            coverage_date > first
            or coverage_date > opening_date.fromordinal(opening_date.toordinal() + 1))
        start_known = not future and opening_date < first and not incomplete
        end_known = (start_known or (not future and first <= opening_date <= as_of
                                    and (coverage_date is None or coverage_date <= opening_date)))
        reason = ("future_month" if future else
                  "incomplete_source_coverage" if incomplete else
                  "opening_balance_unavailable" if not start_known else None)

        previous = _ZERO
        if start_known:
            previous = money(account["opening"])
            for row in store.db.execute(
                    "SELECT amount FROM transactions WHERE account_id=? AND date<?",
                    (account_id, first.isoformat())):
                previous += money(row["amount"])
        elif end_known:
            previous = money(account["opening"])

        inflow = outflow = _ZERO
        cash_review_count = 0
        by_position = {}

        def position(key, label):
            return by_position.setdefault(key, {"label": label, "inflow": _ZERO,
                                                "outflow": _ZERO,
                                                "plan_inflow": None,
                                                "plan_outflow": None})

        def add_actual(key, label, value):
            entry = position(key, label)
            if value >= 0:
                entry["inflow"] += value
            else:
                entry["outflow"] -= value

        by_day = defaultdict(lambda: _ZERO)
        if not future:
            transfer_predicate = sql_transfer_predicate(alias="t", context_alias="c")
            rows = store.db.execute(
                "SELECT t.external_id,t.date,t.amount,t.currency,t.category,"
                "c.counterparty,c.description,o.category_id,o.confirmed,"
                "cat.label AS category_label,cat.transaction_type,"
                f"CASE WHEN {transfer_predicate} THEN 1 ELSE 0 END AS is_transfer "
                "FROM transactions t "
                "LEFT JOIN transaction_context c USING(account_id,external_id) "
                "LEFT JOIN classification_overrides o USING(account_id,external_id) "
                "LEFT JOIN category_catalog cat ON cat.id=o.category_id "
                "WHERE t.account_id=? AND t.date BETWEEN ? AND ? "
                "ORDER BY t.date,t.external_id",
                (account_id, first.isoformat(), as_of.isoformat()))
            for row in rows:
                value = money(row["amount"])
                label = row["category_label"] or row["category"] or "Ohne Kategorie"
                cash_principal = _ZERO
                if (account["kind"] == "CHECKING" and row["currency"] == "EUR"
                        and value < 0 and not row["is_transfer"]
                        and row["transaction_type"] in (None, "expense")
                        and not (row["confirmed"] == 1 and canonical_category_id(
                            store, row["category_id"]) == "AUSGABEN_BARGELD")):
                    status, amount = explicit_cash_component(row)
                    if status == "explicit":
                        cash_principal = amount
                    elif status == "review_required":
                        cash_review_count += 1
                assigned_parts = item_allocations.get((account_id, row["external_id"]), ())
                valid_parts = []
                for item_id, part in assigned_parts:
                    item = items_by_id.get(item_id)
                    amount = money(part)
                    if (plan_available and item is not None
                            and item.get("account_id") == account_id
                            and item_id in planned_by_item and amount
                            and ((amount > 0) == (value > 0))):
                        valid_parts.append((item_id, amount))
                allocated = sum((part for _, part in valid_parts), _ZERO)
                purchase_amount = value + cash_principal
                if abs(allocated) > abs(purchase_amount):
                    raise ValueError("Account item allocation exceeds purchase component")
                for item_id, part in valid_parts:
                    add_actual(("item", item_id), items_by_id[item_id]["label"], part)
                remainder = purchase_amount - allocated
                if remainder:
                    add_actual(("category", label), label, remainder)
                if cash_principal:
                    add_actual(("cash_component", "AUSGABEN_BARGELD"),
                               "Bargeldauszahlung", -cash_principal)
                if value >= 0:
                    inflow += value
                else:
                    outflow -= value
                by_day[row["date"]] += value

        points = []
        if end_known:
            balance = previous
            begin = first if start_known else opening_date
            day = begin
            while day <= as_of:
                balance += by_day[day.isoformat()]
                points.append({"date": day.isoformat(), "balance": _fmt(balance)})
                day = day.fromordinal(day.toordinal() + 1)
        account_plan = None
        if assigned[account_id] or transfers[account_id]:
            planned_in = planned_out = _ZERO
            for item in assigned[account_id]:
                value = money(planned_by_item[item["id"]])
                entry = position(("item", item["id"]), item["label"])
                if item["kind"] == "income":
                    planned_in += value
                    entry["plan_inflow"] = (entry["plan_inflow"] or _ZERO) + value
                else:
                    planned_out += value
                    entry["plan_outflow"] = (entry["plan_outflow"] or _ZERO) + value
            for direction, transfer in transfers[account_id]:
                entry = position(("transfer", transfer["id"], direction), transfer["label"])
                if direction == "inflow":
                    planned_in += money(transfer["amount"])
                    entry["plan_inflow"] = money(transfer["amount"])
                else:
                    planned_out += money(transfer["amount"])
                    entry["plan_outflow"] = money(transfer["amount"])
            account_plan = {"inflow": _fmt(planned_in), "outflow": _fmt(planned_out),
                            "change": _fmt(planned_in - planned_out)}
        positions = [{field: (_fmt(value) if isinstance(value, Decimal) else value)
                      for field, value in entry.items()}
                     for _, entry in sorted(by_position.items(), key=lambda pair: str(pair[0]))]
        result.append({
            "id": account_id, "owner": account["owner"],
            "display_name": account["display_name"] or account_id,
            "kind": account["kind"], "currency": account["currency"],
            "available": start_known, "reason": reason,
            "start": _fmt(previous) if start_known else None,
            "end": points[-1]["balance"] if end_known and points else None,
            "inflow": _fmt(inflow), "outflow": _fmt(outflow),
            "cash_review_count": cash_review_count,
            "change": _fmt(inflow - outflow), "plan": account_plan,
            "plan_partial": account_plan is not None,
            "positions": positions, "points": points,
            "note": ("Rekonstruierter monetärer Buchsaldo; kein Depot-Marktwert."
                     if account["kind"] == "DEPOT" else None),
        })

    if not include_cash:
        return {"as_of": as_of.isoformat(), "accounts": result, "cash": None}

    cash_activity = cash_activity or {}
    funding = (money(cash_activity.get("withdrawals", {}).get("total", "0.00"))
               if as_of >= first else _ZERO)
    receipt_spending = (money(cash_activity.get("receipt_spending", {}).get("total", "0.00"))
                        if as_of >= first else _ZERO)
    rule_spending = _ZERO
    if as_of >= first and (not scope or scope.get("mode") != "individual"):
        from .bonsy_cash import overview
        for receipt in overview(store, _today=as_of)["confirmed"]:
            if (receipt["reason"] == "cash_by_explicit_user_rule"
                    and first.isoformat() <= receipt["date"] <= as_of.isoformat()):
                rule_spending += money(receipt["remaining"])
    spending = receipt_spending + rule_spending
    cash_positions = [
        {"label": "Abhebungen ins Barvermögen", "inflow": _fmt(funding),
         "outflow": "0.00", "plan_inflow": None, "plan_outflow": None},
        {"label": "Zugeordnete Barbelege", "inflow": "0.00",
         "outflow": _fmt(receipt_spending), "plan_inflow": None, "plan_outflow": None},
        {"label": "Barbelege nach bestätigter Regel", "inflow": "0.00",
         "outflow": _fmt(rule_spending), "plan_inflow": None, "plan_outflow": None},
    ]
    cash = {"id": "VIRTUAL_CASH", "owner": "JOINT",
            "display_name": "Gemeinsames Barvermögen", "kind": "CASH",
            "currency": "EUR", "inflow": _fmt(funding),
            "outflow": _fmt(spending), "change": _fmt(funding - spending),
            "start": None, "end": None, "plan": None, "positions": cash_positions,
            "note": "Erfasste Abhebungen, zugeordnete Barbelege und bestätigte Regel-Barbelege; kein bekannter Barbestand."}
    # Before the configured household start, there is no shared cash pool to
    # attribute these historical personal movements to.
    return {"as_of": as_of.isoformat(), "accounts": result,
            "cash": None if scope.get("mode") == "individual" else cash}
