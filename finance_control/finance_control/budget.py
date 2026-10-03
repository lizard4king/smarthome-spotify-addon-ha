"""Validated, revisioned budget plans without access to ledger data."""

import csv
import io
import json
import re
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from .transfer_corrections import effective_transfer_id

_PLAN_FIELDS = {"title", "start_month", "notes", "items"}
_PLAN_OPTIONAL_FIELDS = _PLAN_FIELDS | {
    "horizon_months", "actual_mappings", "actual_allocations", "liquidity_accounts",
    "payday_cycles", "pending_transfers", "household_split", "unmapped_expense_item_id",
    "cash_receipt_item_id",
}
_ITEM_FIELDS = {
    "id", "label", "kind", "amount", "start_month", "end_month", "source", "confirmed"
}
_ITEM_OPTIONAL_FIELDS = _ITEM_FIELDS | {"interval_months", "account_id"}
_MONTH_RE = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])\Z")
_DATE_RE = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])-([0-2][0-9]|3[01])\Z")
_CENT = Decimal("0.01")
_MAX_AMOUNT = Decimal("999999999999.99")


def canonical_category_id(store, category_id):
    """Resolve a retired category ID without rewriting immutable plans."""
    row = store.db.execute(
        "SELECT canonical_id FROM category_aliases WHERE alias_id=?", (category_id,)
    ).fetchone()
    return row["canonical_id"] if row is not None else category_id


def _object(value, fields, name):
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"{name} has invalid fields")


def _text(value, name, minimum, maximum):
    if (not isinstance(value, str) or not minimum <= len(value) <= maximum
            or (minimum > 0 and not value.strip())):
        raise ValueError(f"{name} has invalid length")
    return value


def _month(value, name):
    if not isinstance(value, str) or not _MONTH_RE.fullmatch(value):
        raise ValueError(f"{name} must use YYYY-MM")
    year = int(value[:4])
    if not 1 <= year <= 9998:
        raise ValueError(f"{name} is outside the supported range")
    return value


def _amount(value):
    if not isinstance(value, str) or not value:
        raise ValueError("amount must be decimal text")
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("amount must be decimal text") from exc
    if (not amount.is_finite() or amount < 0 or amount > _MAX_AMOUNT
            or amount != amount.quantize(_CENT)):
        raise ValueError("amount must be nonnegative, finite and cent-exact")
    return format(amount, ".2f")


def _signed_amount(value, name):
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be decimal text")
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{name} must be decimal text") from exc
    if (not amount.is_finite() or abs(amount) > _MAX_AMOUNT
            or amount != amount.quantize(_CENT)):
        raise ValueError(f"{name} must be finite and cent-exact")
    return format(amount, ".2f")


def _date(value, name):
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise ValueError(f"{name} must use YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a valid date") from exc
    return value


def _validate_plan(value, store=None):
    if not isinstance(value, dict) or not _PLAN_FIELDS <= set(value) or set(value) - _PLAN_OPTIONAL_FIELDS:
        raise ValueError("plan has invalid fields")
    items = value["items"]
    if not isinstance(items, list) or len(items) > 200:
        raise ValueError("plan supports at most 200 items")
    plan = {
        "title": _text(value["title"], "title", 1, 120),
        "start_month": _month(value["start_month"], "start_month"),
        "notes": _text(value["notes"], "notes", 0, 2000),
        "items": [],
    }
    personal_item_ids = set()
    if "horizon_months" in value:
        horizon = value["horizon_months"]
        if type(horizon) is not int or not 1 <= horizon <= 120:
            raise ValueError("horizon_months must be an integer from 1 to 120")
        plan["horizon_months"] = horizon
    if "household_split" in value:
        split = value["household_split"]
        if not isinstance(split, list) or not 1 <= len(split) <= 10:
            raise ValueError("household_split must contain 1 to 10 entries")
        validated_split = []
        party_ids = set()
        total_share = Decimal("0.00")
        for raw in split:
            fields = set(raw) if isinstance(raw, dict) else set()
            if fields not in (
                    {"party_id", "share"}, {"party_id", "share", "personal_item_ids"}):
                raise ValueError("household split has invalid fields")
            party_id = _text(raw["party_id"], "household split party_id", 1, 64)
            if party_id in party_ids:
                raise ValueError("household split party_ids must be unique")
            party_ids.add(party_id)
            share = _amount(raw["share"])
            if Decimal(share) <= 0:
                raise ValueError("household split shares must be positive")
            total_share += Decimal(share)
            entry = {"party_id": party_id, "share": share}
            if "personal_item_ids" in raw:
                item_ids = raw["personal_item_ids"]
                if not isinstance(item_ids, list) or len(item_ids) > 200:
                    raise ValueError(
                        "household split personal_item_ids must contain at most 200 items")
                validated_items = []
                for raw_item_id in item_ids:
                    item_id = _text(
                        raw_item_id, "household split personal item id", 1, 64)
                    if item_id in personal_item_ids:
                        raise ValueError(
                            "household split personal items may belong to only one party")
                    personal_item_ids.add(item_id)
                    validated_items.append(item_id)
                entry["personal_item_ids"] = validated_items
            validated_split.append(entry)
        if total_share != Decimal("1.00"):
            raise ValueError("household split shares must sum to 1.00")
        plan["household_split"] = validated_split
    if "liquidity_accounts" in value:
        account_ids = value["liquidity_accounts"]
        if (not isinstance(account_ids, list) or not 1 <= len(account_ids) <= 10
                or len(set(account_ids)) != len(account_ids)):
            raise ValueError("liquidity_accounts must contain 1 to 10 unique accounts")
        validated_accounts = []
        for raw_account_id in account_ids:
            account_id = _text(raw_account_id, "liquidity account id", 1, 120)
            if store is not None:
                account = store.db.execute(
                    "SELECT kind FROM accounts WHERE id=?", (account_id,)
                ).fetchone()
                if account is None or account["kind"] != "CHECKING":
                    raise ValueError("liquidity account must reference a checking account")
            validated_accounts.append(account_id)
        plan["liquidity_accounts"] = validated_accounts
    if "payday_cycles" in value:
        cycles = value["payday_cycles"]
        if not isinstance(cycles, list) or len(cycles) > 10:
            raise ValueError("payday_cycles must be a list with at most 10 entries")
        validated_cycles = []
        cycle_accounts = set()
        for raw in cycles:
            _object(raw, {"account_id", "owner_label", "last_salary_date",
                          "next_salary_date", "next_salary_basis", "target_balance",
                          "cashflows"}, "payday cycle")
            account_id = _text(raw["account_id"], "payday account id", 1, 120)
            if account_id in cycle_accounts:
                raise ValueError("payday cycle accounts must be unique")
            cycle_accounts.add(account_id)
            if store is not None:
                account = store.db.execute(
                    "SELECT kind FROM accounts WHERE id=?", (account_id,)
                ).fetchone()
                if account is None or account["kind"] != "CHECKING":
                    raise ValueError("payday cycle must reference a checking account")
            last_salary = _date(raw["last_salary_date"], "last_salary_date")
            next_salary = _date(raw["next_salary_date"], "next_salary_date")
            if next_salary <= last_salary:
                raise ValueError("next_salary_date must follow last_salary_date")
            cashflows = raw["cashflows"]
            if not isinstance(cashflows, list) or len(cashflows) > 100:
                raise ValueError("payday cashflows must be a list with at most 100 entries")
            validated_cashflows = []
            for flow in cashflows:
                _object(flow, {"label", "direction", "amount", "due_date", "evidence"},
                        "payday cashflow")
                if flow["direction"] not in {"inflow", "outflow"}:
                    raise ValueError("payday cashflow direction is invalid")
                validated_cashflows.append({
                    "label": _text(flow["label"], "payday cashflow label", 1, 160),
                    "direction": flow["direction"],
                    "amount": _amount(flow["amount"]),
                    "due_date": _date(flow["due_date"], "payday cashflow due_date"),
                    "evidence": _text(flow["evidence"], "payday cashflow evidence", 1, 240),
                })
            validated_cycles.append({
                "account_id": account_id,
                "owner_label": _text(raw["owner_label"], "payday owner label", 1, 120),
                "last_salary_date": last_salary,
                "next_salary_date": next_salary,
                "next_salary_basis": _text(
                    raw["next_salary_basis"], "next_salary_basis", 1, 240),
                "target_balance": _signed_amount(raw["target_balance"], "target_balance"),
                "cashflows": validated_cashflows,
            })
        plan["payday_cycles"] = validated_cycles
    if "pending_transfers" in value:
        transfers = value["pending_transfers"]
        if not isinstance(transfers, list) or len(transfers) > 100:
            raise ValueError("pending_transfers must be a list with at most 100 entries")
        validated_transfers = []
        transfer_ids = set()
        for raw in transfers:
            _object(raw, {"id", "label", "from_account_id", "to_account_id", "amount",
                          "value_date", "evidence"}, "pending transfer")
            transfer_id = _text(raw["id"], "pending transfer id", 1, 64)
            if transfer_id in transfer_ids:
                raise ValueError("pending transfer ids must be unique")
            transfer_ids.add(transfer_id)
            from_account = _text(raw["from_account_id"], "from_account_id", 1, 120)
            to_account = _text(raw["to_account_id"], "to_account_id", 1, 120)
            if from_account == to_account:
                raise ValueError("pending transfer accounts must differ")
            if store is not None:
                accounts = store.db.execute(
                    "SELECT id,kind FROM accounts WHERE id IN (?,?)", (from_account, to_account)
                ).fetchall()
                if (len(accounts) != 2
                        or any(account["kind"] != "CHECKING" for account in accounts)):
                    raise ValueError("pending transfer must reference checking accounts")
            validated_transfers.append({
                "id": transfer_id,
                "label": _text(raw["label"], "pending transfer label", 1, 160),
                "from_account_id": from_account, "to_account_id": to_account,
                "amount": _amount(raw["amount"]),
                "value_date": _date(raw["value_date"], "pending transfer value_date"),
                "evidence": _text(raw["evidence"], "pending transfer evidence", 1, 240),
            })
        plan["pending_transfers"] = validated_transfers
    # Keep legacy payloads byte-for-byte shape compatible when the new field is omitted.
    horizon = plan.get("horizon_months", 12)
    if int(_shift_month(plan["start_month"], horizon - 1).split("-")[0]) > 9998:
        raise ValueError("horizon_months extends beyond the supported month range")
    identifiers = set()
    for raw in items:
        if (not isinstance(raw, dict) or not _ITEM_FIELDS <= set(raw)
                or set(raw) - _ITEM_OPTIONAL_FIELDS):
            raise ValueError("item has invalid fields")
        identifier = _text(raw["id"], "item id", 1, 64)
        if identifier in identifiers:
            raise ValueError("item ids must be unique")
        identifiers.add(identifier)
        kind = raw["kind"]
        if kind not in {"income", "fixed", "variable"}:
            raise ValueError("invalid item kind")
        start = _month(raw["start_month"], "item start_month")
        end = raw["end_month"]
        if end is not None:
            end = _month(end, "item end_month")
            if end < start:
                raise ValueError("item end_month precedes start_month")
        if type(raw["confirmed"]) is not bool:
            raise ValueError("confirmed must be boolean")
        interval = raw.get("interval_months", 1)
        if type(interval) is not int or not 1 <= interval <= 120:
            raise ValueError("item interval_months must be an integer from 1 to 120")
        item = {
            "id": identifier,
            "label": _text(raw["label"], "item label", 1, 160),
            "kind": kind,
            "amount": _amount(raw["amount"]),
            "start_month": start,
            "end_month": end,
            "source": _text(raw["source"], "item source", 1, 240),
            "confirmed": raw["confirmed"],
        }
        if "account_id" in raw:
            account_id = _text(raw["account_id"], "item account id", 1, 120)
            if store is not None:
                account = store.db.execute(
                    "SELECT kind FROM accounts WHERE id=?", (account_id,)
                ).fetchone()
                if account is None or account["kind"] != "CHECKING":
                    raise ValueError("item account must reference a checking account")
            item["account_id"] = account_id
        # Preserve legacy snapshot shape while making monthly cadence the default.
        if "interval_months" in raw:
            item["interval_months"] = interval
        plan["items"].append(item)
    if "unmapped_expense_item_id" in value:
        fallback_item_id = _text(
            value["unmapped_expense_item_id"], "unmapped expense item id", 1, 64)
        fallback_item = next((item for item in plan["items"]
                              if item["id"] == fallback_item_id), None)
        if fallback_item is None or fallback_item["kind"] == "income":
            raise ValueError(
                "unmapped_expense_item_id must reference an existing expense item")
        plan["unmapped_expense_item_id"] = fallback_item_id
    if "cash_receipt_item_id" in value:
        cash_item_id = _text(value["cash_receipt_item_id"], "cash receipt item id", 1, 64)
        cash_item = next((item for item in plan["items"] if item["id"] == cash_item_id), None)
        if cash_item is None or cash_item["kind"] != "variable":
            raise ValueError("cash_receipt_item_id must reference an existing variable item")
        plan["cash_receipt_item_id"] = cash_item_id
    if personal_item_ids - identifiers:
        raise ValueError("household split references an unknown personal item")
    if "actual_mappings" in value:
        mappings = value["actual_mappings"]
        if not isinstance(mappings, list) or len(mappings) > 500:
            raise ValueError("actual_mappings must be a list with at most 500 entries")
        category_ids = set()
        item_by_id = {item["id"]: item for item in plan["items"]}
        validated = []
        for raw in mappings:
            _object(raw, {"category_id", "item_id"}, "actual mapping")
            category_id = canonical_category_id(
                store, _text(raw["category_id"], "category_id", 1, 120)
            ) if store is not None else _text(raw["category_id"], "category_id", 1, 120)
            item_id = _text(raw["item_id"], "item_id", 1, 64)
            if category_id in category_ids:
                raise ValueError("each actual category may be mapped only once")
            category_ids.add(category_id)
            item = item_by_id.get(item_id)
            if item is None:
                raise ValueError("actual mapping references an unknown item")
            if store is not None:
                category = store.db.execute(
                    "SELECT transaction_type FROM category_catalog WHERE id=?", (category_id,)
                ).fetchone()
                expected = "income" if item["kind"] == "income" else "expense"
                if category is None or category["transaction_type"] != expected:
                    raise ValueError("actual mapping category and budget item have incompatible types")
            validated.append({"category_id": category_id, "item_id": item_id})
        plan["actual_mappings"] = validated
    if "actual_allocations" in value:
        allocations = value["actual_allocations"]
        if not isinstance(allocations, list) or len(allocations) > 20_000:
            raise ValueError("actual_allocations must be a list with at most 20000 entries")
        allocation_groups = {}
        item_by_id = {item["id"]: item for item in plan["items"]}
        transaction_types = {}
        validated = []
        for raw in allocations:
            fields = set(raw) if isinstance(raw, dict) else set()
            if fields not in ({"account_id", "external_id", "item_id"},
                              {"account_id", "external_id", "item_id", "weight"}):
                raise ValueError("actual allocation has invalid fields")
            account_id = _text(raw["account_id"], "account_id", 1, 120)
            external_id = _text(raw["external_id"], "external_id", 1, 240)
            item_id = _text(raw["item_id"], "item_id", 1, 64)
            key = (account_id, external_id)
            if item_id not in identifiers:
                raise ValueError("actual allocation references an unknown item")
            if store is not None:
                transaction_type = transaction_types.get(key)
                if transaction_type is None:
                    transaction = store.db.execute(
                        "SELECT t.*,cat.transaction_type,o.confirmed FROM transactions t "
                        "LEFT JOIN classification_overrides o "
                        "ON o.account_id=t.account_id AND o.external_id=t.external_id "
                        "LEFT JOIN category_catalog cat ON cat.id=o.category_id "
                        "WHERE t.account_id=? AND t.external_id=?", key
                    ).fetchone()
                    if transaction is None:
                        raise ValueError("actual allocation must reference a classified transaction")
                    if effective_transfer_id(store, transaction):
                        amount = Decimal(transaction["amount"])
                        transaction_type = (
                            "income" if amount > 0 else "expense" if amount < 0 else None)
                    elif (transaction["confirmed"] == 1
                          and transaction["transaction_type"] in {"income", "expense"}):
                        transaction_type = transaction["transaction_type"]
                    else:
                        raise ValueError(
                            "actual allocation must reference a classified transaction")
                    if transaction_type not in {"income", "expense"}:
                        raise ValueError(
                            "actual allocation transaction and item have incompatible types")
                    transaction_types[key] = transaction_type
                expected_type = "income" if item_by_id[item_id]["kind"] == "income" else "expense"
                if transaction_type != expected_type:
                    raise ValueError("actual allocation transaction and item have incompatible types")
            group = allocation_groups.setdefault(key, [])
            if item_id in {entry["item_id"] for entry in group}:
                raise ValueError("a transaction may reference each item only once")
            allocation = {"account_id": account_id, "external_id": external_id,
                          "item_id": item_id}
            if "weight" in raw:
                weight = _amount(raw["weight"])
                if Decimal(weight) <= 0:
                    raise ValueError("actual allocation weight must be positive")
                allocation["weight"] = weight
            group.append(allocation)
            validated.append(allocation)
        for group in allocation_groups.values():
            if len(group) > 1 and not all("weight" in entry for entry in group):
                raise ValueError("split allocations require a weight for every item")
        plan["actual_allocations"] = validated
    return plan


def _latest_revision(db):
    return db.execute("SELECT COALESCE(MAX(revision), 0) FROM budget_snapshots").fetchone()[0]


def _explicit_active_revision(db):
    row = db.execute(
        "SELECT revision FROM budget_activations ORDER BY sequence DESC LIMIT 1"
    ).fetchone()
    return row["revision"] if row is not None else None


def _active_revision(db, latest=None):
    return _explicit_active_revision(db) or (
        _latest_revision(db) if latest is None else latest
    )


def load(store, data=None):
    """Return the active snapshot, or an explicitly requested historical one."""
    if data is None:
        data = {}
    if not isinstance(data, dict) or set(data) - {'revision'}:
        raise ValueError('invalid budget load request')
    requested = data.get('revision')
    if 'revision' in data:
        _revision(requested)
    rows = store.db.execute(
        "SELECT * FROM budget_snapshots ORDER BY revision"
    ).fetchall()
    if not rows:
        if requested is not None:
            raise ValueError('unknown budget revision')
        return {"revision": 0, "plan": None, "history": []}
    active_revision = _active_revision(store.db, rows[-1]['revision'])
    selected_revision = active_revision if requested is None else requested
    selected = next((row for row in rows if row['revision'] == selected_revision), None)
    if selected is None:
        raise ValueError('unknown budget revision')
    plan = json.loads(selected['payload'])
    return {
        "revision": selected["revision"],
        "latest_revision": rows[-1]['revision'],
        "active_revision": active_revision,
        "base_revision": selected['base_revision'],
        "plan": plan,
        "calculation": json.loads(selected['calculation']) if selected['calculation'] is not None else calculate(store, {'plan': plan}),
        "calculation_status": 'saved' if selected['calculation'] is not None else 'recalculated_legacy',
        "history": [
            {"revision": row["revision"], "created_at": row["created_at"],
             "title": json.loads(row['payload'])['title'],
             "start_month": json.loads(row['payload'])['start_month'],
             "horizon_months": json.loads(row['payload']).get('horizon_months', 12),
             "base_revision": row['base_revision'],
             "active": row["revision"] == active_revision} for row in rows
        ],
    }


def save(store, data):
    """Append a snapshot if the caller's revision is still current."""
    if (not isinstance(data, dict) or not {'revision', 'plan'} <= set(data)
            or set(data) - {'revision', 'plan', 'base_revision', 'activate', 'active_revision'}):
        raise ValueError('invalid budget save request')
    if type(data["revision"]) is not int or data["revision"] < 0:
        raise ValueError("revision must be a nonnegative integer")
    plan = _validate_plan(data["plan"], store)
    result = calculate(store, {'plan': plan})
    activate_saved = data.get('activate', False)
    if type(activate_saved) is not bool:
        raise ValueError('activate must be boolean')
    expected_active = data.get('active_revision')
    if activate_saved and (type(expected_active) is not int or expected_active < 0):
        raise ValueError('active_revision is required when activating a save')
    base = data.get('base_revision', data['revision'] or None)
    if base is not None:
        _revision(base)
    db = store.db
    db.execute("BEGIN IMMEDIATE")
    try:
        latest = _latest_revision(db)
        if data["revision"] != latest:
            raise ValueError("stale budget revision")
        active = _active_revision(db, latest)
        if activate_saved and expected_active != active:
            raise ValueError('stale active budget revision')
        if base is not None and not db.execute('SELECT 1 FROM budget_snapshots WHERE revision=?', (base,)).fetchone():
            raise ValueError('unknown base revision')
        historic_sources = {}
        for row in db.execute("SELECT payload FROM budget_snapshots ORDER BY revision"):
            for item in json.loads(row["payload"])["items"]:
                historic_sources.setdefault(item["id"], item["source"])
        for item in plan["items"]:
            previous = historic_sources.get(item["id"])
            if previous is not None and item["source"] != previous:
                raise ValueError("item source is immutable; create a new item instead")
        db.execute(
            "INSERT INTO budget_snapshots(revision, created_at, payload, calculation, base_revision) VALUES (?, ?, ?, ?, ?)",
            (latest + 1, datetime.now(UTC).isoformat(),
             json.dumps(plan, ensure_ascii=False, separators=(",", ":")),
             json.dumps(result, ensure_ascii=False), base),
        )
        if _explicit_active_revision(db) is None and not activate_saved:
            # A fresh database activates its first saved plan. For a legacy
            # database, freeze the pre-save latest revision as the active one.
            initial_active = latest + 1 if latest == 0 else latest
            db.execute(
                "INSERT INTO budget_activations(revision,activated_at) VALUES (?,?)",
                (initial_active, datetime.now(UTC).isoformat()),
            )
        if activate_saved:
            db.execute(
                "INSERT INTO budget_activations(revision,activated_at) VALUES (?,?)",
                (latest + 1, datetime.now(UTC).isoformat()),
            )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return load(store, {'revision': latest + 1})


def activate(store, data):
    """Append an audited selection of the active immutable plan revision."""
    _object(data, {'revision', 'active_revision'}, 'budget activation request')
    revision = _revision(data['revision'])
    expected_active = data['active_revision']
    if type(expected_active) is not int or expected_active < 0:
        raise ValueError('active_revision must be a nonnegative integer')
    db = store.db
    db.execute("BEGIN IMMEDIATE")
    try:
        latest = _latest_revision(db)
        if _active_revision(db, latest) != expected_active:
            raise ValueError('stale active budget revision')
        if not db.execute(
                "SELECT 1 FROM budget_snapshots WHERE revision=?", (revision,)).fetchone():
            raise ValueError('unknown budget revision')
        if _explicit_active_revision(db) != revision:
            db.execute(
                "INSERT INTO budget_activations(revision,activated_at) VALUES (?,?)",
                (revision, datetime.now(UTC).isoformat()),
            )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return load(store)


def _revision(value):
    if type(value) is not int or not 1 <= value <= 2147483647:
        raise ValueError('invalid budget revision')
    return value


def compare(store, data):
    _object(data, {'first', 'second'}, 'compare request')
    first = load(store, {'revision': _revision(data['first'])})
    second = load(store, {'revision': _revision(data['second'])})
    if first['plan']['start_month'] != second['plan']['start_month']:
        raise ValueError('Vergleich benötigt denselben ersten Planmonat.')
    first_periods = [row['period'] for row in first['calculation']['rows']]
    second_periods = [row['period'] for row in second['calculation']['rows']]
    if first_periods != second_periods:
        raise ValueError('Vergleich benötigt denselben Planungshorizont und dieselben Monatszeilen.')
    rows = []
    for a, b in zip(first['calculation']['rows'], second['calculation']['rows'], strict=True):
        if a['period'] != b['period']:
            raise ValueError('Incompatible budget periods')
        rows.append({'period': a['period'], 'first': a['cumulative'], 'second': b['cumulative'],
                     'difference': format(Decimal(b['cumulative']) - Decimal(a['cumulative']), '.2f'),
                     'cashflow_difference': format(Decimal(b['cashflow']) - Decimal(a['cashflow']), '.2f')})
    a_items = {i['id']: i for i in first['plan']['items']}
    b_items = {i['id']: i for i in second['plan']['items']}
    changes = [{'id': key, 'before': a_items.get(key), 'after': b_items.get(key)}
               for key in sorted(a_items.keys() | b_items.keys()) if a_items.get(key) != b_items.get(key)]
    return {'first': first, 'second': second, 'rows': rows, 'changes': changes,
            'notes_changed': first['plan']['notes'] != second['plan']['notes']}


def export_snapshot(store, data):
    _object(data, {'revision'}, 'export request')
    snapshot = load(store, {'revision': _revision(data['revision'])})
    # Export only this snapshot, never unrelated history or the current draft.
    snapshot = {k: v for k, v in snapshot.items() if k not in {'history', 'latest_revision'}}
    snapshot['format_version'] = 1
    output = io.StringIO(newline='')
    writer = csv.writer(output, delimiter=';', lineterminator='\r\n')
    writer.writerow(['Monat', 'Einnahmen_EUR', 'Fixkosten_EUR', 'Variabel_EUR', 'Cashflow_EUR', 'Kumulierte_Veraenderung_EUR'])
    for row in snapshot['calculation']['rows']:
        writer.writerow([row['period']] + [row[k].replace('.', ',') for k in ('income', 'fixed', 'variable', 'cashflow', 'cumulative')])
    return {'snapshot': snapshot, 'csv': output.getvalue()}


def _shift_month(value, offset):
    index = int(value[:4]) * 12 + int(value[5:]) - 1 + offset
    year, month = divmod(index, 12)
    return f"{year:04d}-{month + 1:02d}"


def _month_distance(first, second):
    return ((int(second[:4]) - int(first[:4])) * 12
            + int(second[5:]) - int(first[5:]))


def _item_active(item, period):
    """Return whether a recurring plan item belongs to one month."""
    return (item["start_month"] <= period
            and (item["end_month"] is None or period <= item["end_month"])
            and _month_distance(item["start_month"], period)
            % item.get("interval_months", 1) == 0)


def calculate(store, data):
    """Calculate a cent-exact plan projection without reading the ledger."""
    _object(data, {"plan"}, "calculate request")
    plan = _validate_plan(data["plan"], store)
    rows = []
    cumulative = Decimal("0.00")
    total_income = Decimal("0.00")
    total_expenses = Decimal("0.00")
    confirmed_cumulative = Decimal("0.00")
    estimated_cumulative = Decimal("0.00")
    confirmed_totals = {key: Decimal("0.00") for key in ("income", "expenses")}
    estimated_totals = {key: Decimal("0.00") for key in ("income", "expenses")}
    horizon = plan.get("horizon_months", 12)
    first_period = plan["start_month"]
    last_period = _shift_month(first_period, horizon - 1)
    for offset in range(horizon):
        period = _shift_month(plan["start_month"], offset)
        buckets = {kind: Decimal("0.00") for kind in ("income", "fixed", "variable")}
        confirmed = {key: Decimal("0.00") for key in ("income", "expenses")}
        estimated = {key: Decimal("0.00") for key in ("income", "expenses")}
        active_items = []
        for item in plan["items"]:
            if _item_active(item, period):
                buckets[item["kind"]] += Decimal(item["amount"])
                target = confirmed if item["confirmed"] else estimated
                target["income" if item["kind"] == "income" else "expenses"] += Decimal(item["amount"])
                active_items.append(item.copy())
        expenses = buckets["fixed"] + buckets["variable"]
        cashflow = buckets["income"] - expenses
        confirmed_cashflow = confirmed["income"] - confirmed["expenses"]
        estimated_cashflow = estimated["income"] - estimated["expenses"]
        cumulative += cashflow
        confirmed_cumulative += confirmed_cashflow
        estimated_cumulative += estimated_cashflow
        total_income += buckets["income"]
        total_expenses += expenses
        for key in confirmed_totals:
            confirmed_totals[key] += confirmed[key]
            estimated_totals[key] += estimated[key]
        rows.append({
            "period": period,
            "income": format(buckets["income"], ".2f"),
            "fixed": format(buckets["fixed"], ".2f"),
            "variable": format(buckets["variable"], ".2f"),
            "cashflow": format(cashflow, ".2f"),
            "cumulative": format(cumulative, ".2f"),
            "confirmed": {
                "income": format(confirmed["income"], ".2f"),
                "expenses": format(confirmed["expenses"], ".2f"),
                "cashflow": format(confirmed_cashflow, ".2f"),
                "cumulative": format(confirmed_cumulative, ".2f"),
            },
            "estimated": {
                "income": format(estimated["income"], ".2f"),
                "expenses": format(estimated["expenses"], ".2f"),
                "cashflow": format(estimated_cashflow, ".2f"),
                "cumulative": format(estimated_cumulative, ".2f"),
            },
            "items": active_items,
        })
    warnings = [f"Nicht bestätigt: {item['label']}" for item in plan["items"]
                if not item["confirmed"]]
    return {
        "rows": rows,
        "outside_horizon": [item.copy() for item in plan["items"]
                            if (item["end_month"] is not None and item["end_month"] < first_period)
                            or item["start_month"] > last_period],
        "warnings": warnings,
        "totals": {
            "income": format(total_income, ".2f"),
            "expenses": format(total_expenses, ".2f"),
            "cashflow": format(total_income - total_expenses, ".2f"),
            "confirmed": {
                "income": format(confirmed_totals["income"], ".2f"),
                "expenses": format(confirmed_totals["expenses"], ".2f"),
                "cashflow": format(confirmed_totals["income"] - confirmed_totals["expenses"], ".2f"),
                "cumulative": format(confirmed_cumulative, ".2f"),
            },
            "estimated": {
                "income": format(estimated_totals["income"], ".2f"),
                "expenses": format(estimated_totals["expenses"], ".2f"),
                "cashflow": format(estimated_totals["income"] - estimated_totals["expenses"], ".2f"),
                "cumulative": format(estimated_cumulative, ".2f"),
            },
        },
    }
