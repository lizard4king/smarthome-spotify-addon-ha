"""Immutable wealth snapshots with explicit, non-derived goal progress."""

import hashlib
import json
import re
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from .core import money, parse_money_input

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z", re.ASCII)
_KINDS = {"asset", "liability", "investment"}
_MAX_AMOUNT = Decimal("999999999999.99")


def _calendar_day(value, name):
    if not isinstance(value, str):
        raise TypeError(f"{name} must use YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{name} must use YYYY-MM-DD") from error
    if parsed.isoformat() != value:
        raise ValueError(f"{name} must use YYYY-MM-DD")
    return parsed


def _amount(value, name, *, positive):
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be decimal text")
    try:
        parsed = parse_money_input(value)
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"{name} must be decimal text") from error
    if (not parsed.is_finite() or parsed < 0 or (positive and parsed == 0)
            or parsed > _MAX_AMOUNT or parsed != parsed.quantize(Decimal("0.01"))):
        raise ValueError(f"{name} must be a supported cent amount")
    return format(parsed, ".2f")


def _label(value, name):
    if (not isinstance(value, str) or not value.strip() or len(value.strip()) > 120
            or any(ord(character) < 32 for character in value)):
        raise ValueError(f"{name} is invalid")
    return value.strip()


def _known_people(store, people):
    if people is None:
        identifiers = {row[0] for row in store.db.execute("SELECT id FROM persons")}
    else:
        if not isinstance(people, (list, tuple)):
            raise ValueError("profile people are invalid")
        identifiers = {
            entry.get("id") for entry in people
            if isinstance(entry, dict) and isinstance(entry.get("id"), str)
        }
    if not identifiers:
        raise ValueError("wealth snapshot requires at least one owner")
    return identifiers


def _manual_positions(raw_positions, *, as_of, owners):
    if not isinstance(raw_positions, list) or len(raw_positions) > 500:
        raise ValueError("manual_positions must be a list with at most 500 entries")
    result = []
    seen = set()
    for raw in raw_positions:
        if not isinstance(raw, dict) or set(raw) != {
                "id", "label", "kind", "amount", "currency", "valued_on", "owner"}:
            raise ValueError("manual position has invalid fields")
        identifier = raw["id"]
        if not isinstance(identifier, str) or _ID.fullmatch(identifier) is None:
            raise ValueError("manual position id must be a stable ASCII identifier")
        if identifier in seen:
            raise ValueError("manual position ids must be unique")
        seen.add(identifier)
        kind = raw["kind"]
        if kind not in _KINDS:
            raise ValueError("manual position kind is unsupported")
        if raw["currency"] != "EUR":
            raise ValueError("manual position currency must be EUR")
        valued_on = _calendar_day(raw["valued_on"], "valued_on")
        if valued_on > as_of:
            raise ValueError("manual position valuation cannot follow the snapshot date")
        owner = raw["owner"]
        if owner not in owners and not (owner == "JOINT" and len(owners) > 1):
            raise ValueError("manual position owner is unavailable")
        result.append({
            "id": identifier,
            "label": _label(raw["label"], "manual position label"),
            "kind": kind,
            "amount": _amount(raw["amount"], "manual position amount", positive=True),
            "currency": "EUR",
            "valued_on": valued_on.isoformat(),
            "owner": owner,
            "source": "manual",
        })
    return sorted(result, key=lambda entry: entry["id"])


def _goal_progress(raw_entries, *, as_of, goals):
    if not isinstance(raw_entries, list) or len(raw_entries) > 100:
        raise ValueError("goal_progress must be a list with at most 100 entries")
    goal_map = {}
    for raw in goals or ():
        if isinstance(raw, dict) and isinstance(raw.get("id"), str):
            goal_map[raw["id"]] = raw
    result = []
    seen = set()
    for raw in raw_entries:
        if not isinstance(raw, dict) or set(raw) != {"goal_id", "current_amount", "valued_on"}:
            raise ValueError("goal progress has invalid fields")
        goal_id = raw["goal_id"]
        if goal_id in seen or goal_id not in goal_map:
            raise ValueError("goal progress must reference each available profile goal at most once")
        seen.add(goal_id)
        valued_on = _calendar_day(raw["valued_on"], "goal progress valued_on")
        if valued_on > as_of:
            raise ValueError("goal progress valuation cannot follow the snapshot date")
        goal = goal_map[goal_id]
        result.append({
            "goal_id": goal_id,
            "label": goal["label"],
            "target_amount": _amount(goal["target_amount"], "goal target_amount", positive=True),
            "current_amount": _amount(
                raw["current_amount"], "goal current_amount", positive=False),
            "currency": goal["currency"],
            "valued_on": valued_on.isoformat(),
            "source": "explicit",
        })
    return sorted(result, key=lambda entry: entry["goal_id"])


def _ledger_positions(store, as_of, *, owners):
    accounts = store.accounts()
    if not accounts:
        return [], []
    status = store.status(as_of.isoformat())
    positions, excluded = [], []
    for account in accounts:
        owner = account["owner"]
        if owner not in owners and not (owner == "JOINT" and len(owners) > 1):
            raise ValueError("ledger account owner is unavailable in the active household")
        if account["kind"] == "DEPOT":
            excluded.append({
                "account_id": account["id"],
                "label": account.get("display_name") or account["id"],
                "reason": "market_value_required",
            })
            continue
        if account["kind"] not in {"CHECKING", "SAVINGS", "CREDIT_CARD"}:
            continue
        balance = money(status["balances"][account["id"]])
        positions.append({
            "id": f"account:{account['id']}",
            "account_id": account["id"],
            "label": account.get("display_name") or account["id"],
            "kind": "liability" if balance < 0 else "asset",
            "amount": format(abs(balance), ".2f"),
            "currency": account["currency"],
            "valued_on": as_of.isoformat(),
            "owner": owner,
            "source": "ledger",
        })
    return (sorted(positions, key=lambda entry: entry["account_id"]),
            sorted(excluded, key=lambda entry: entry["account_id"]))


def _totals(positions):
    investments = sum(
        (money(entry["amount"]) for entry in positions if entry["kind"] == "investment"),
        Decimal(0),
    )
    assets = sum(
        (money(entry["amount"]) for entry in positions
         if entry["kind"] in {"asset", "investment"}),
        Decimal(0),
    )
    liabilities = sum(
        (money(entry["amount"]) for entry in positions if entry["kind"] == "liability"),
        Decimal(0),
    )
    return {
        "assets": format(assets, ".2f"),
        "liabilities": format(liabilities, ".2f"),
        "net_worth": format(assets - liabilities, ".2f"),
        "investments": format(investments, ".2f"),
    }


def _latest_row(store):
    return store.db.execute(
        "SELECT * FROM wealth_snapshots ORDER BY revision DESC LIMIT 1"
    ).fetchone()


def _canonical_payload(store, data, *, goals, people):
    if not isinstance(data, dict):
        raise TypeError("wealth request must be an object")
    required = {"as_of", "manual_positions", "goal_progress"}
    if set(data) - (required | {"latest_revision", "confirmed", "review_token"}) or not required <= set(data):
        raise ValueError("wealth request has invalid fields")
    as_of = _calendar_day(data["as_of"], "as_of")
    owners = _known_people(store, people)
    manual = _manual_positions(data["manual_positions"], as_of=as_of, owners=owners)
    progress = _goal_progress(data["goal_progress"], as_of=as_of, goals=goals)
    ledger, excluded = _ledger_positions(store, as_of, owners=owners)
    positions = ledger + manual
    return {
        "version": 1,
        "as_of": as_of.isoformat(),
        "ledger_positions": ledger,
        "manual_positions": manual,
        "positions": positions,
        "goal_progress": progress,
        "totals": _totals(positions),
        "excluded_depots": excluded,
    }


def _digest(payload):
    return hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")).hexdigest()


def _preview(store, data, *, goals, people):
    latest = _latest_row(store)
    latest_revision = 0 if latest is None else latest["revision"]
    supplied_revision = data.get("latest_revision", latest_revision)
    if type(supplied_revision) is not int or supplied_revision < 0:
        raise ValueError("latest_revision must be a nonnegative integer")
    payload = _canonical_payload(store, data, goals=goals, people=people)
    source_digest = _digest(payload)
    review_token = _digest({
        "version": 1,
        "latest_revision": supplied_revision,
        "source_digest": source_digest,
    })
    return {
        **payload,
        "latest_revision": latest_revision,
        "next_revision": latest_revision + 1,
        "source_digest": source_digest,
        "review_token": review_token,
    }


def preview(store, data, *, goals=(), people=None):
    """Return a stable read snapshot without changing the database."""
    if store.db.in_transaction:
        return _preview(store, data, goals=goals, people=people)
    store.db.execute("BEGIN")
    try:
        return _preview(store, data, goals=goals, people=people)
    finally:
        store.db.rollback()


def _stored(row):
    return {
        "id": row["id"],
        "revision": row["revision"],
        "created_at": row["created_at"],
        "source_digest": row["source_digest"],
        **json.loads(row["payload"]),
    }


def load(store, data):
    """Load one immutable revision, or the latest revision for an empty request."""
    if not isinstance(data, dict) or set(data) - {"revision"}:
        raise ValueError("wealth load request has invalid fields")
    revision = data.get("revision")
    if revision is None:
        row = _latest_row(store)
    else:
        if type(revision) is not int or revision < 1:
            raise ValueError("revision must be a positive integer")
        row = store.db.execute(
            "SELECT * FROM wealth_snapshots WHERE revision=?", (revision,)
        ).fetchone()
    history = [{
        "revision": item["revision"],
        "as_of": item["as_of"],
        "created_at": item["created_at"],
        "net_worth": json.loads(item["payload"])["totals"]["net_worth"],
    } for item in store.db.execute(
        "SELECT revision,as_of,created_at,payload FROM wealth_snapshots ORDER BY revision DESC"
    )]
    return {"snapshot": None if row is None else _stored(row), "history": history}


def records(store):
    return [_stored(row) for row in store.db.execute(
        "SELECT * FROM wealth_snapshots ORDER BY revision"
    )]


def save(store, data, *, goals=(), people=None):
    """Recheck and atomically save a complete immutable wealth revision."""
    if not isinstance(data, dict) or data.get("confirmed") is not True:
        raise ValueError("explicit confirmation is required")
    if not isinstance(data.get("review_token"), str):
        raise TypeError("review_token is required")
    supplied_revision = data.get("latest_revision")
    if type(supplied_revision) is not int or supplied_revision < 0:
        raise ValueError("latest_revision must be a nonnegative integer")
    store.db.execute("BEGIN IMMEDIATE")
    try:
        latest = _latest_row(store)
        latest_revision = 0 if latest is None else latest["revision"]
        current = _preview(store, data, goals=goals, people=people)
        if data["review_token"] != current["review_token"]:
            raise ValueError("wealth review token is stale")
        if latest is not None and latest["source_digest"] == current["source_digest"]:
            store.db.rollback()
            return {**_stored(latest), "saved": False, "unchanged": True}
        if supplied_revision != latest_revision:
            raise ValueError("wealth revision is stale")
        revision = latest_revision + 1
        created_at = datetime.now(UTC).isoformat()
        payload = {key: value for key, value in current.items() if key not in {
            "latest_revision", "next_revision", "source_digest", "review_token"}}
        row_id = store.db.execute(
            "INSERT INTO wealth_snapshots(revision,as_of,created_at,source_digest,payload) "
            "VALUES (?,?,?,?,?)",
            (revision, payload["as_of"], created_at, current["source_digest"],
             json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)),
        ).lastrowid
        store.db.commit()
    except Exception:
        if store.db.in_transaction:
            store.db.rollback()
        raise
    return {
        "id": row_id,
        "revision": revision,
        "created_at": created_at,
        "source_digest": current["source_digest"],
        **payload,
        "saved": True,
        "unchanged": False,
    }
