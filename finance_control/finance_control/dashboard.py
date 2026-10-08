"""Read-only account dashboard data from the local ledger."""

from datetime import date

from .account_overview import build_account_overview
from .core import money
from .person_attribution import person_label
from .reporting_history import scope_for_month


class DashboardConfigurationError(ValueError):
    """The reporting configuration is invalid for the dashboard."""


def build(store, as_of, accounts=None, *, reporting_history=None):
    """Return account movements through ``as_of`` without household netting.

    ``accounts`` may be the account dictionaries already enriched with owner
    labels by the web layer. The reporting policy supplies known source gaps.
    """
    try:
        day = as_of if type(as_of) is date else date.fromisoformat(as_of)
    except (TypeError, ValueError) as error:
        raise ValueError("invalid dashboard date") from error
    if isinstance(as_of, str) and as_of != day.isoformat():
        raise ValueError("invalid dashboard date")
    month = day.strftime("%Y-%m")
    supplied = store.accounts() if accounts is None else accounts
    account_by_id = {account["id"]: account for account in supplied}
    try:
        scope = scope_for_month(store, month, reporting_history)
    except ValueError as error:
        raise DashboardConfigurationError(
            "Dashboard-Konfiguration ist ungültig.") from error
    scope["included_account_ids"] = [account_id for account_id in scope["included_account_ids"]
                                     if account_id in account_by_id]
    overview = build_account_overview(
        store, month, None, {}, plan_available=False, reporting_scope=scope,
        cash_activity={}, today=day, include_cash=False)
    account_rows = overview["accounts"]
    # This is ledger coverage metadata, not a statement that the bank was
    # queried on this date. Keep it independent of the displayed month.
    last_booking_by_account = {}
    if account_rows:
        account_ids = sorted(row["id"] for row in account_rows)
        placeholders = ",".join("?" for _ in account_ids)
        last_booking_by_account = {
            row["account_id"]: row["last_booking_date"]
            for row in store.db.execute(
                "SELECT account_id,MAX(date) AS last_booking_date "
                "FROM transactions "
                f"WHERE date<=? AND account_id IN ({placeholders}) "
                "GROUP BY account_id",
                (day.isoformat(), *account_ids),
            )
        }
    for row in account_rows:
        row["last_booking_date"] = last_booking_by_account.get(row["id"])
        supplied_account = account_by_id[row["id"]]
        row["owner_label"] = supplied_account.get("owner_label") or (
            "Gemeinsam" if row["owner"] == "JOINT" else person_label(row["owner"]))
    account_rows.sort(key=lambda row: (row["kind"] != "CHECKING",
                                       row["display_name"].casefold(), row["id"]))
    selected = {row["id"] for row in account_rows}
    bookings = []
    if selected:
        # Load the taxonomy once so each booking can expose its authoritative
        # root-to-leaf path without parsing category labels.
        catalog = {
            row["id"]: {"id": row["id"], "label": row["label"],
                        "parent_id": row["parent_id"]}
            for row in store.db.execute(
                "SELECT id,label,parent_id FROM category_catalog")
        }

        def category_path(category_id, leaf_label):
            if category_id is None:
                return [{"id": None, "label": leaf_label}]
            leaf = catalog.get(category_id)
            if leaf is None:
                return [{"id": category_id, "label": leaf_label}]
            chain = []
            visited = set()
            current = leaf
            while current is not None:
                if current["id"] in visited:
                    return [{"id": leaf["id"], "label": leaf["label"]}]
                visited.add(current["id"])
                chain.append({"id": current["id"], "label": current["label"]})
                parent_id = current["parent_id"]
                if parent_id is None:
                    break
                current = catalog.get(parent_id)
                if current is None:
                    return [{"id": leaf["id"], "label": leaf["label"]}]
            return list(reversed(chain))

        account_ids = sorted(selected)
        placeholders = ",".join("?" for _ in account_ids)
        query = (
            "SELECT t.account_id,t.external_id,t.date,t.amount,t.currency,"
            "t.category,c.counterparty,c.description,o.category_id,cat.label AS override_label "
            "FROM transactions t "
            "LEFT JOIN transaction_context c USING(account_id,external_id) "
            "LEFT JOIN classification_overrides o USING(account_id,external_id) "
            "LEFT JOIN category_catalog cat ON cat.id=o.category_id "
            f"WHERE t.date BETWEEN ? AND ? AND t.account_id IN ({placeholders}) "
            "ORDER BY t.date DESC,t.account_id,t.external_id")
        params = (month + "-01", day.isoformat(), *account_ids)
        for row in store.db.execute(query, params):
            raw_category = row["category"] or "Ohne Kategorie"
            category_id = row["category_id"]
            category_label = row["override_label"] or raw_category
            bookings.append({
                "account_id": row["account_id"], "external_id": row["external_id"],
                "date": row["date"], "amount": format(money(row["amount"]), ".2f"),
                "currency": row["currency"], "counterparty": row["counterparty"] or "",
                "description": row["description"] or "",
                "category": category_label,
                "category_id": category_id,
                "category_path": category_path(category_id, category_label),
            })
    return {"as_of": day.isoformat(), "month": month, "accounts": account_rows,
            "bookings": bookings,
            "currencies": sorted({row["currency"] for row in account_rows})}
