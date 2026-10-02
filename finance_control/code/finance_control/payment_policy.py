"""Validated, local-only and revisioned payment-policy assumptions."""

import json
import re
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

_POLICY_FIELDS = {"title", "person_a_label", "person_b_label", "principles", "phases"}
_PHASE_FIELDS = {
    "name", "active", "valid_from", "valid_to", "trigger", "person_a_share", "person_b_share",
    "joint_buffer", "person_a_contribution", "person_b_contribution", "person_a_free",
    "person_b_free", "special_load", "source", "status",
}
_CENT = Decimal("0.01")
_SHARE = Decimal("0.0001")
_MAX_AMOUNT = Decimal("999999999999.99")
_MAX_PAYLOAD_BYTES = 64 * 1024
_STATUSES = {"draft", "confirmed", "review_needed"}
_V2_POLICY_FIELDS = {"schema_version", "title", "participants", "principles", "phases"}
_V2_PHASE_FIELDS = {"name", "active", "valid_from", "valid_to", "trigger", "joint_buffer",
                    "special_load", "source", "status", "allocations"}


def _object(value, fields, name):
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"{name} has invalid fields")


def _text(value, name, minimum, maximum):
    if (not isinstance(value, str) or not minimum <= len(value) <= maximum
            or (minimum and not value.strip())):
        raise ValueError(f"{name} has invalid length")
    return value


def _date(value, name):
    if not isinstance(value, str):
        raise ValueError(f"{name} must use YYYY-MM-DD")  # noqa: TRY004
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} must use YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{name} must use YYYY-MM-DD")
    return value


def _decimal_text(value, name, quantum, maximum):
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be decimal text")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{name} must be decimal text") from exc
    if (not parsed.is_finite() or parsed < 0 or parsed > maximum
            or parsed != parsed.quantize(quantum)):
        raise ValueError(f"{name} is outside the supported range")
    return format(parsed, ".4f" if quantum == _SHARE else ".2f")


def _amount(value, name):
    return _decimal_text(value, name, _CENT, _MAX_AMOUNT)


def _share(value, name):
    return _decimal_text(value, name, _SHARE, Decimal("1.0000"))


def _validate_policy(value):
    try:
        payload_size = len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise ValueError("policy must be JSON-compatible") from exc
    if payload_size > _MAX_PAYLOAD_BYTES:
        raise ValueError("policy exceeds the maximum payload size")
    if isinstance(value, dict) and value.get("schema_version") == 2:
        return _validate_policy_v2(value)
    _object(value, _POLICY_FIELDS, "payment policy")
    phases = value["phases"]
    if not isinstance(phases, list) or not 1 <= len(phases) <= 24:
        raise ValueError("payment policy supports one to 24 phases")
    principles = value["principles"]
    if not isinstance(principles, list) or len(principles) > 12:
        raise ValueError("principles supports at most 12 entries")
    policy = {
        "title": _text(value["title"], "title", 1, 120),
        "person_a_label": _text(value["person_a_label"], "person_a_label", 1, 80),
        "person_b_label": _text(value["person_b_label"], "person_b_label", 1, 80),
        "principles": [_text(item, "principle", 1, 240) for item in principles],
        "phases": [],
    }
    names = set()
    for raw in phases:
        _object(raw, _PHASE_FIELDS, "payment phase")
        name = _text(raw["name"], "phase name", 1, 120)
        if name in names:
            raise ValueError("phase names must be unique")
        names.add(name)
        valid_from = _date(raw["valid_from"], "valid_from")
        valid_to = raw["valid_to"]
        if valid_to is not None:
            valid_to = _date(valid_to, "valid_to")
            if valid_to < valid_from:
                raise ValueError("valid_to precedes valid_from")
        first_share = _share(raw["person_a_share"], "person_a_share")
        second_share = _share(raw["person_b_share"], "person_b_share")
        if Decimal(first_share) + Decimal(second_share) != Decimal("1.0000"):
            raise ValueError("person shares must sum to 1.0000")
        status = raw["status"]
        if status not in _STATUSES:
            raise ValueError("invalid payment phase status")
        if type(raw["active"]) is not bool:
            raise ValueError("active must be boolean")
        special_load = raw["special_load"]
        if special_load is not None:
            special_load = _amount(special_load, "special_load")
        policy["phases"].append({
            "name": name,
            "active": raw["active"],
            "valid_from": valid_from,
            "valid_to": valid_to,
            "trigger": _text(raw["trigger"], "trigger", 1, 240),
            "person_a_share": first_share,
            "person_b_share": second_share,
            "joint_buffer": _amount(raw["joint_buffer"], "joint_buffer"),
            "person_a_contribution": _amount(raw["person_a_contribution"], "person_a_contribution"),
            "person_b_contribution": _amount(raw["person_b_contribution"], "person_b_contribution"),
            "person_a_free": _amount(raw["person_a_free"], "person_a_free"),
            "person_b_free": _amount(raw["person_b_free"], "person_b_free"),
            "special_load": special_load,
            "source": _text(raw["source"], "source", 1, 240),
            "status": status,
        })
    if sum(phase["active"] for phase in policy["phases"]) != 1:
        raise ValueError("payment policy requires exactly one active phase")
    return policy


def _validate_policy_v2(value):
    _object(value, _V2_POLICY_FIELDS, "payment policy v2")
    participants = value["participants"]
    if not isinstance(participants, list) or not 1 <= len(participants) <= 10:
        raise ValueError("participants must contain 1 to 10 people")
    checked_participants, person_ids, labels = [], set(), set()
    for raw in participants:
        _object(raw, {"id", "label"}, "participant")
        person_id = _text(raw["id"], "participant id", 1, 64)
        label = _text(raw["label"], "participant label", 1, 80)
        if (person_id.casefold() == "joint"
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", person_id, flags=re.ASCII)
                or person_id in person_ids or label.casefold() in labels):
            raise ValueError("participant ids and labels must be unique stable values")
        person_ids.add(person_id)
        labels.add(label.casefold())
        checked_participants.append({"id": person_id, "label": label})
    phases = value["phases"]
    if not isinstance(phases, list) or not 1 <= len(phases) <= 24:
        raise ValueError("payment policy supports one to 24 phases")
    principles = value["principles"]
    if not isinstance(principles, list) or len(principles) > 12:
        raise ValueError("principles supports at most 12 entries")
    policy = {"schema_version": 2, "title": _text(value["title"], "title", 1, 120),
              "participants": checked_participants,
              "principles": [_text(item, "principle", 1, 240) for item in principles],
              "phases": []}
    names = set()
    for raw in phases:
        _object(raw, _V2_PHASE_FIELDS, "payment phase v2")
        name = _text(raw["name"], "phase name", 1, 120)
        if name in names:
            raise ValueError("phase names must be unique")
        names.add(name)
        valid_from = _date(raw["valid_from"], "valid_from")
        valid_to = raw["valid_to"]
        if valid_to is not None:
            valid_to = _date(valid_to, "valid_to")
            if valid_to < valid_from:
                raise ValueError("valid_to precedes valid_from")
        status = raw["status"]
        if status not in _STATUSES or type(raw["active"]) is not bool:
            raise ValueError("invalid phase status or active flag")
        allocations = raw["allocations"]
        if not isinstance(allocations, list) or len(allocations) != len(person_ids):
            raise ValueError("allocations must include every participant exactly once")
        checked_allocations, allocation_ids = [], set()
        share_total = Decimal("0.0000")
        for allocation in allocations:
            _object(allocation, {"person_id", "share", "contribution", "free"}, "allocation")
            person_id = allocation["person_id"]
            if person_id not in person_ids or person_id in allocation_ids:
                raise ValueError("allocation references an unknown or duplicate participant")
            allocation_ids.add(person_id)
            share = _share(allocation["share"], "allocation share")
            if Decimal(share) <= 0:
                raise ValueError("allocation shares must be positive")
            share_total += Decimal(share)
            checked_allocations.append({"person_id": person_id, "share": share,
                                        "contribution": _amount(allocation["contribution"], "contribution"),
                                        "free": _amount(allocation["free"], "free")})
        if allocation_ids != person_ids or share_total != Decimal("1.0000"):
            raise ValueError("allocation shares must cover participants and sum to 1.0000")
        special_load = raw["special_load"]
        if special_load is not None:
            special_load = _amount(special_load, "special_load")
        policy["phases"].append({
            "name": name, "active": raw["active"], "valid_from": valid_from,
            "valid_to": valid_to, "trigger": _text(raw["trigger"], "trigger", 1, 240),
            "joint_buffer": _amount(raw["joint_buffer"], "joint_buffer"),
            "special_load": special_load, "source": _text(raw["source"], "source", 1, 240),
            "status": status, "allocations": checked_allocations,
        })
    if sum(phase["active"] for phase in policy["phases"]) != 1:
        raise ValueError("payment policy requires exactly one active phase")
    return policy


def _revision(value):
    if type(value) is not int or not 1 <= value <= 2147483647:
        raise ValueError("invalid payment policy revision")
    return value


def _latest_revision(db):
    return db.execute("SELECT COALESCE(MAX(revision), 0) FROM payment_policy_snapshots").fetchone()[0]


def load(store, data=None):
    """Return a local payment-policy snapshot and compact immutable history."""
    if data is None:
        data = {}
    if not isinstance(data, dict) or set(data) - {"revision"}:
        raise ValueError("invalid payment policy load request")
    requested = data.get("revision")
    if "revision" in data:
        _revision(requested)
    rows = store.db.execute("SELECT * FROM payment_policy_snapshots ORDER BY revision").fetchall()
    if not rows:
        if requested is not None:
            raise ValueError("unknown payment policy revision")
        return {"revision": 0, "latest_revision": 0, "base_revision": None,
                "policy": None, "history": []}
    current = rows[-1] if requested is None else next((row for row in rows if row["revision"] == requested), None)
    if current is None:
        raise ValueError("unknown payment policy revision")
    policy = json.loads(current["payload"])
    return {
        "revision": current["revision"],
        "latest_revision": rows[-1]["revision"],
        "base_revision": current["base_revision"],
        "policy": policy,
        "history": [
            {"revision": row["revision"], "created_at": row["created_at"],
             "title": json.loads(row["payload"])["title"], "base_revision": row["base_revision"]}
            for row in rows
        ],
    }


def save(store, data):
    """Append one immutable, validated payment-policy revision locally."""
    if (not isinstance(data, dict) or not {"revision", "policy"} <= set(data)
            or set(data) - {"revision", "policy", "base_revision"}):
        raise ValueError("invalid payment policy save request")
    if type(data["revision"]) is not int or data["revision"] < 0:
        raise ValueError("revision must be a nonnegative integer")
    policy = _validate_policy(data["policy"])
    base = data.get("base_revision", data["revision"] or None)
    if base is not None:
        _revision(base)
    db = store.db
    db.execute("BEGIN IMMEDIATE")
    try:
        latest = _latest_revision(db)
        if data["revision"] != latest:
            raise ValueError("stale payment policy revision")
        if base is not None and not db.execute(
                "SELECT 1 FROM payment_policy_snapshots WHERE revision=?", (base,)).fetchone():
            raise ValueError("unknown payment policy base revision")
        db.execute(
            "INSERT INTO payment_policy_snapshots(revision,created_at,payload,base_revision) VALUES (?,?,?,?)",
            (latest + 1, datetime.now(UTC).isoformat(),
             json.dumps(policy, ensure_ascii=False, separators=(",", ":")), base),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return load(store)
