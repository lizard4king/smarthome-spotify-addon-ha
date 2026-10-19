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
