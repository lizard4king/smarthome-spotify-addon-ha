"""Disabled-by-default app entrypoint. Persistent writes only under /data.

No configuration/credential output, token discovery, reconnect loop or fallback.
Deployment and the broad homeassistant_api permission require separate approval.
"""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import signal
import time

import controller

WEEK = timedelta(days=7)
MAX_SLEEP = 3600


class StopRequested(BaseException):
    """Allow stack unwinding through controller's mutation cleanup on SIGTERM."""


def options_validate(options):
    keys = {"enabled", "inspect_only", "initialize_ui", "reconcile_pending", "forget_deleted_backup_id", "run_once_now", "apply_retention", "retained_backups", "addon_slug", "local_agent_id", "drive_agent_id"}
    optional = {"retained_backups", "forget_deleted_backup_id"}
    if not isinstance(options, dict) or not keys - optional <= set(options) <= keys:
        raise controller.SafetyStop("Invalid app option keys")
    if any(type(options[flag]) is not bool for flag in ("enabled", "inspect_only", "initialize_ui", "reconcile_pending", "run_once_now", "apply_retention")):
        raise controller.SafetyStop("App flags must be boolean")
    retained_backups = options.get("retained_backups", 2)
    if type(retained_backups) is not int or not 2 <= retained_backups <= 100:
        raise controller.SafetyStop("Retained backup count must be from 2 to 100")
    forget_id = options.get("forget_deleted_backup_id", "")
    if not isinstance(forget_id, str) or forget_id != forget_id.strip():
        raise controller.SafetyStop("Invalid deleted backup ID option")
    if sum(options[flag] for flag in ("inspect_only", "initialize_ui", "reconcile_pending", "run_once_now")) + bool(forget_id) > 1:
        raise controller.SafetyStop("Setup modes must not be combined")
    if options["inspect_only"] or options["initialize_ui"]:
        return None  # Configured source/destination placeholders are unused.
    config = {k: options[k] for k in ("addon_slug", "local_agent_id", "drive_agent_id")}
    controller.config_validate(config)
    return config


def reconcile_once(config, state_path, now, *, socket_factory=None, environ=None):
    """Explicit one-shot operator action; remote reads, one verified local save."""
    from recovery import reconcile
    token = (os.environ if environ is None else environ).get("SUPERVISOR_TOKEN")
    if not isinstance(token, str) or not token or not Path(state_path).is_file():
        raise controller.SafetyStop("Pending reconciliation prerequisites unavailable")
    socket = None
    try:
        with controller.lock(state_path):
            state = controller.State(state_path, config)
            if socket_factory is None:
                import websocket
                websocket.enableTrace(False)
                socket_factory = websocket.create_connection
            socket = socket_factory("ws://supervisor/core/websocket", timeout=30)
            try:
                api = controller.HA(socket)
                api.authenticate(token)
                token = None
                result = reconcile(api, state, now)
            finally:
                if socket is not None:
                    socket.close()
        return result
    except Exception:
        raise controller.SafetyStop("Pending reconciliation stopped; inspect existing state without retry") from None


def forget_deleted_once(config, state_path, backup_id, *, socket_factory=None, environ=None):
    """One read-only remote check followed by one local state update."""
    from recovery import forget_deleted_backup, FORGET_STOP
    token = (os.environ if environ is None else environ).get("SUPERVISOR_TOKEN")
    if not isinstance(token, str) or not token or not Path(state_path).is_file():
        raise controller.SafetyStop(FORGET_STOP)
    socket = None
    try:
        with controller.lock(state_path):
            state = controller.State(state_path, config)
            if socket_factory is None:
                import websocket
                websocket.enableTrace(False)
                socket_factory = websocket.create_connection
            socket = socket_factory("ws://supervisor/core/websocket", timeout=30)
            try:
                api = controller.HA(socket)
                api.authenticate(token)
                token = None
                result = forget_deleted_backup(api, state, backup_id)
            finally:
                if socket is not None:
                    socket.close()
        return result
    except Exception:
        raise controller.SafetyStop(FORGET_STOP) from None


def next_delay(state, now):
    """UTC cadence from last successful backup, never from missed calendar slots."""
    state.require_clear()
    if now.tzinfo is None:
        raise controller.SafetyStop("Scheduler UTC time required")
    owned = state.data["owned"]
    if not owned:
        return 0
    last = max(controller.utc(o["created_utc"]) for o in owned)
    if last > now:
        raise controller.SafetyStop("Clock precedes successful backup")
    return min(MAX_SLEEP, max(0, (last + WEEK - now).total_seconds()))


def serve(data_dir=Path("/data"), *, clock=None, sleep=None, invoke=None, inspect=None, initialize=None, reconcile=None, forget=None):
    """Read options every hour at most; a failed one-shot is never retried here."""
    clock = clock or (lambda: datetime.now(timezone.utc))
    sleep = sleep or time.sleep
    invoke = invoke or controller.main
    inspect = inspect or controller.inspect_configuration
    initialize = initialize or controller.initialize_native_ui
    reconcile = reconcile or reconcile_once
    forget = forget or forget_deleted_once
    data_dir = Path(data_dir)
    options_path = data_dir / "options.json"
    state_path = data_dir / "controller-state.json"
    config_path = data_dir / "controller-config.json"
    # No network nor state/config writes on the disabled branch.
    while True:
        options = json.loads(options_path.read_text(encoding="utf-8"))
        config = options_validate(options)
        if not options["enabled"]:
            print("Disabled; no connection attempted")
            return 0
        if options["inspect_only"]:
            print(json.dumps(inspect(), sort_keys=True))
            return 0
        if options["initialize_ui"]:
            initialize()
            print("Native backup UI initialized; no backup created")
            return 0
        if options["reconcile_pending"]:
            print(reconcile(config, state_path, clock()))
            return 0  # Never generate, prune or schedule after reconciliation.
        if options.get("forget_deleted_backup_id", ""):
            print(forget(config, state_path, options["forget_deleted_backup_id"]))
            return 0  # Never generate, prune or schedule after this one-shot.
        state = controller.State(state_path, config)
        previous_ids = {record["backup_id"] for record in state.data["owned"]}
        delay = next_delay(state, clock())  # Also validates uncertainty and clock.
        if options["run_once_now"]:
            delay = 0
        if delay:
            sleep(delay)
            continue
        # IDs only. This file never contains the Supervisor token or backup key.
        # All writes happen only after explicit enabling, under private /data.
        config_path.write_text(json.dumps(config, sort_keys=True), encoding="utf-8")
        args = ["--config", str(config_path), "--state", str(state_path), "--run-once"]
        if options["apply_retention"]:
            args.extend(["--apply-retention", "--retained-backups",
                         str(options.get("retained_backups", 2))])
        if options["run_once_now"]:
            args.append("--run-now")
        if invoke(args) != 0:
            print("Stopped after failed one-shot; operator action required")
            return 2
        if options["run_once_now"]:
            after = controller.State(state_path, config)
            new_ids = {record["backup_id"] for record in after.data["owned"]} - previous_ids
            if len(new_ids) != 1 or next_delay(after, clock()) <= 0:
                raise controller.SafetyStop("No durable new success; one-shot stopped")
            return 0
        # A successful return without a persisted success must never form a loop.
        after = controller.State(state_path, config)
        if next_delay(after, clock()) <= 0:
            raise controller.SafetyStop("No durable new success; scheduler stopped")


def main():
    def stop(_signal, _frame):
        raise StopRequested()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        return serve()
    except StopRequested:
        print("Stopped")
        return 0
    except controller.SafetyStop as error:
        print("STOP: " + str(error))
        return 2
    except Exception:
        print("STOP: invalid options/state or uncertain operation; inspect locally")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
