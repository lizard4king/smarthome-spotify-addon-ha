"""Disabled-by-default app entrypoint. Persistent writes only under /data.

No configuration/credential output, token discovery, reconnect loop or fallback.
Deployment and the broad homeassistant_api permission require separate approval.
"""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import signal
import time

import controller

WEEK = timedelta(days=7)
MAX_SLEEP = 3600


class StopRequested(BaseException):
    """Allow stack unwinding through controller's mutation cleanup on SIGTERM."""


def options_validate(options):
    keys = {"enabled", "inspect_only", "initialize_ui", "apply_retention", "addon_slug", "local_agent_id", "drive_agent_id"}
    if not isinstance(options, dict) or set(options) != keys:
        raise controller.SafetyStop("Invalid app option keys")
    if any(type(options[flag]) is not bool for flag in ("enabled", "inspect_only", "initialize_ui", "apply_retention")):
        raise controller.SafetyStop("App flags must be boolean")
    if options["inspect_only"] and options["initialize_ui"]:
        raise controller.SafetyStop("Setup modes must not be combined")
    if options["inspect_only"] or options["initialize_ui"]:
        return None  # Configured source/destination placeholders are unused.
    config = {k: options[k] for k in ("addon_slug", "local_agent_id", "drive_agent_id")}
    controller.config_validate(config)
    return config


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


def serve(data_dir=Path("/data"), *, clock=None, sleep=None, invoke=None, inspect=None, initialize=None):
    """Read options every hour at most; a failed one-shot is never retried here."""
    clock = clock or (lambda: datetime.now(timezone.utc))
    sleep = sleep or time.sleep
    invoke = invoke or controller.main
    inspect = inspect or controller.inspect_configuration
    initialize = initialize or controller.initialize_native_ui
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
        state = controller.State(state_path, config)
        delay = next_delay(state, clock())
        if delay:
            sleep(delay)
            continue
        # IDs only. This file never contains the Supervisor token or backup key.
        # All writes happen only after explicit enabling, under private /data.
        config_path.write_text(json.dumps(config, sort_keys=True), encoding="utf-8")
        args = ["--config", str(config_path), "--state", str(state_path), "--run-once"]
        if options["apply_retention"]:
            args.append("--apply-retention")
        if invoke(args) != 0:
            print("Stopped after failed one-shot; operator action required")
            return 2
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
