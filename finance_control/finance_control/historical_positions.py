"""Build display-only positions from already selected historical ledger rows."""

import hashlib
import json
from decimal import Decimal

from .classification import normalize_counterparty
from .core import money

_TRANSACTION_FIELDS = (
    "account_id", "external_id", "date", "amount", "counterparty",
    "description", "category_id", "category_label",
)


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _recipient_key(row):
    counterparty = _text(row.get("counterparty"))
    if counterparty:
        return "counterparty", normalize_counterparty(counterparty)
    purpose = _text(row.get("description"))
    if purpose:
        return "purpose", normalize_counterparty(purpose)
    return "neutral", "nicht angegeben"


def _category(row):
    if row.get("historical_classification_open"):
        return ("open", "", "Kategorie offen")
    category_id = _text(row.get("category_id"))
    category_label = _text(row.get("category_label"))
    key = category_id or category_label or "unavailable"
    label = category_label or "Kategorie nicht angegeben"
    return ("confirmed", key, label)


def _position_id(key):
    payload = json.dumps(key, ensure_ascii=False, separators=(",", ":"))
    return "historical-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def historical_positions(ledger_rows, *, known_person_ids=None):
    """Group selected historical bookings without applying budget mappings.

    Rows are expected to have passed ledger filtering and historical open-row
    inclusion already. Each eligible booking is retained exactly once.
    """
    groups = {}
    for row in ledger_rows:
        if (row.get("actual_state") != "classified"
                or row.get("transaction_type") not in {"income", "expense"}):
            continue
        amount = money(row.get("amount"))
        if amount == 0:
            continue

        account_id = row.get("account_id")
        person_id = row.get("account_owner")
        if (known_person_ids is not None and person_id != "JOINT"
                and person_id not in known_person_ids):
            person_id = "JOINT"
        kind = row["transaction_type"]
        recipient_key = _recipient_key(row)
        category_key = _category(row)
        key = (account_id, person_id, kind, *recipient_key, *category_key[:2])
        group = groups.get(key)
        if group is None:
            group = {
                "id": _position_id(key),
                "person_id": person_id,
                "account_id": account_id,
                "kind": kind,
                "label": (_text(row.get("counterparty"))
                          or _text(row.get("description")) or "Nicht angegeben"),
                "category_label": category_key[2],
                "signed_amount": Decimal("0.00"),
                "transaction_count": 0,
                "transactions": [],
            }
            groups[key] = group

        group["signed_amount"] += amount
        group["transaction_count"] += 1
        transaction = {field: row.get(field) for field in _TRANSACTION_FIELDS}
        transaction["amount"] = format(amount, ".2f")
        if row.get("bonsy_entry_id") is not None:
            transaction["bonsy_entry_id"] = row["bonsy_entry_id"]
        group["transactions"].append(transaction)

    result = []
    for group in groups.values():
        group["transactions"].sort(key=lambda row: (
            row.get("date") or "", row.get("account_id") or "",
            row.get("external_id") or ""))
        group["signed_amount"] = format(money(group["signed_amount"]), ".2f")
        result.append(group)
    result.sort(key=lambda group: (-abs(money(group["signed_amount"])), group["id"]))
    return result
