"""Explicit operator reconciliation only; never create, delete, or retry a backup.

The caller owns authentication, exclusive locking, and the operator opt-in.
An exception never causes a second save or remote mutation. A save failure after
the underlying atomic replacement has an uncertain durability outcome; inspect
the disk state rather than resetting it or automatically retrying reconciliation.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import re

from controller import SafetyStop, inventory, matches, utc


SUCCESS = "Pending backup reconciled; no backup created or deleted"
STOP = "Pending reconciliation stopped; inspect existing state without retry"
FORGET_SUCCESS = "Deleted backup ID reconciled; no backup created or deleted"
FORGET_STOP = "Deleted backup reconciliation stopped; inspect existing state without retry"


def forget_deleted_backup(api, state, backup_id):
    """Forget exactly one operator-selected, already absent owned backup ID."""
    original = None
    try:
        state.require_clear()
        if (not isinstance(backup_id, str) or not backup_id
                or backup_id != backup_id.strip()):
            raise SafetyStop(FORGET_STOP)
        owned = state.data["owned"]
        selected = [record for record in owned if record["backup_id"] == backup_id]
        remaining = [record for record in owned if record["backup_id"] != backup_id]
        if (len(selected) != 1 or len(remaining) < 2
                or sum(utc(record["created_utc"]) > utc(selected[0]["created_utc"])
                       for record in remaining) < 2):
            raise SafetyStop(FORGET_STOP)
        api.verify_addon(state.config)

        def snapshot():
            listed = inventory(api)  # Idle manager and empty agent errors required.
            if any(backup["backup_id"] == backup_id for backup in listed):
                raise SafetyStop(FORGET_STOP)
            by_id = {}
            for backup in listed:
                identifier = backup["backup_id"]
                if identifier in by_id:
                    raise SafetyStop(FORGET_STOP)
                by_id[identifier] = backup
            verified = {}
            for record in remaining:
                identifier = record["backup_id"]
                backup = by_id.get(identifier)
                if not matches(backup, record, state.config):
                    raise SafetyStop(FORGET_STOP)
                details = api.command({"type": "backup/details", "backup_id": identifier})
                if (details["agent_errors"] or not matches(details["backup"], record, state.config)
                        or details["backup"] != backup):
                    raise SafetyStop(FORGET_STOP)
                verified[identifier] = deepcopy(backup)
            return verified

        if snapshot() != snapshot():
            raise SafetyStop(FORGET_STOP)
        original = deepcopy(state.data)
        state.data = deepcopy(original)
        state.data["owned"] = deepcopy(remaining)
        state.save()
        return FORGET_SUCCESS
    except Exception:
        if original is not None:
            state.data = original
        raise SafetyStop(FORGET_STOP) from None


def reconcile(api, state, now):
    """Adopt one fully verified existing backup; all API calls are read-only.

    The archive timestamp must lie within the original operation's [-5,+35]
    minute window. There is no expiry for a later explicit operator inspection.
    Two inventory/detail snapshots must agree before the single local save.
    """
    original = None
    try:
        pending = state.data["pending"]
        if (state.data["delete_intent"] is not None
                or not isinstance(pending, dict)
                or set(pending) != {"name", "started_utc", "backup_job_id"}
                or not isinstance(pending["name"], str)
                or re.fullmatch(r"finance-only-[0-9a-f]{32}", pending["name"]) is None
                or not isinstance(pending["started_utc"], str)
                or not isinstance(pending["backup_job_id"], str)
                or not pending["backup_job_id"].strip()
                or not isinstance(now, datetime) or now.utcoffset() is None):
            raise SafetyStop(STOP)
        started = utc(pending["started_utc"])
        now = now.astimezone(timezone.utc)
        if now < started:
            raise SafetyStop(STOP)
        owned = state.data["owned"]
        if not isinstance(owned, list):
            raise SafetyStop(STOP)
        owned_ids = {record["backup_id"] for record in owned}
        # This verifies current installed Finance version/options without writes.
        api.verify_addon(state.config)

        def snapshot():
            candidates = [b for b in inventory(api) if b["name"] == pending["name"]]
            if len(candidates) != 1:
                raise SafetyStop(STOP)
            backup = candidates[0]
            identifier = backup["backup_id"]
            if not isinstance(identifier, str) or not identifier or identifier in owned_ids:
                raise SafetyStop(STOP)
            created = utc(backup["date"])
            if not started - timedelta(minutes=5) <= created <= started + timedelta(minutes=35):
                raise SafetyStop(STOP)
            if created > now:
                raise SafetyStop(STOP)
            record = {"backup_id": identifier, "name": pending["name"],
                      "created_utc": backup["date"]}
            if not matches(backup, record, state.config):
                raise SafetyStop(STOP)
            details = api.command({"type": "backup/details", "backup_id": identifier})
            if details["agent_errors"] or not matches(details["backup"], record, state.config):
                raise SafetyStop(STOP)
            # Include size/encryption and all other metadata in stability checks.
            if details["backup"] != backup:
                raise SafetyStop(STOP)
            return deepcopy(backup), record

        first, record = snapshot()
        second, second_record = snapshot()
        if first != second or record != second_record:
            raise SafetyStop(STOP)
        original = deepcopy(state.data)
        state.data = deepcopy(original)
        state.data["owned"].append(record)
        state.data["pending"] = None
        state.save()
        return SUCCESS
    except Exception:
        if original is not None:
            state.data = original
        # Never expose transport payloads, option values or filesystem paths.
        raise SafetyStop(STOP) from None
