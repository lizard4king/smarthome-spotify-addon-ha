"""Bounded HA 2026.9.4/2026.10.2 prototype. Default execution is offline.

Secrets only cross in-memory internal API boundaries; exception texts from remote
or transport layers must never be printed. No automatic retry/reconciliation.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import time
import threading
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import uuid


class SafetyStop(Exception):
    """Safe, fixed diagnostic text only."""


APPROVED_FINANCE_VERSIONS = frozenset({"0.2.69", "0.2.70", "0.2.71", "0.2.72"})


def utc(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise SafetyStop("Timezone missing")
    return parsed.astimezone(timezone.utc)


def config_validate(config):
    keys = {"addon_slug", "local_agent_id", "drive_agent_id"}
    if set(config) != keys or any(
        not isinstance(config[k], str) or not config[k].strip()
        or config[k] != config[k].strip() for k in keys
    ):
        raise SafetyStop("Explicit addon and agent IDs required")
    if config["local_agent_id"] == config["drive_agent_id"]:
        raise SafetyStop("Agent IDs must differ")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", config["addon_slug"]) is None:
        raise SafetyStop("Addon slug must be a single explicit identifier")


def validate_finance_info(response, config):
    """Default Supervisor role exposes version/hostname, not foreign options."""
    try:
        valid = (response["result"] == "ok"
                 and response["data"]["version"] in APPROVED_FINANCE_VERSIONS
                 and response["data"]["hostname"] == config["addon_slug"].replace("_", "-"))
    except (KeyError, TypeError):
        valid = False
    if not valid:
        raise SafetyStop("Finance version/hostname guard failed; no backup attempted")
    return response["data"]["hostname"]


def validate_backup_guard(response, expected_version="0.2.69"):
    if (not isinstance(response, dict)
            or set(response) != {"schema_version", "backup_safe", "addon_version"}
            or type(response["schema_version"]) is not int or response["schema_version"] != 1
            or response["backup_safe"] is not True
            or not isinstance(expected_version, str) or expected_version not in APPROVED_FINANCE_VERSIONS
            or response["addon_version"] != expected_version):
        raise SafetyStop("Finance public backup guard failed; no backup attempted")


def _guard_get(url, headers, opener=None):
    """Internal bounded GET only, with no redirects or environment proxies."""
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise SafetyStop("Supervisor safety guard refused redirect")
    if opener is None:
        opener = build_opener(ProxyHandler({}), NoRedirect()).open
    request = Request(url, headers=headers, method="GET")
    with opener(request, timeout=20) as response:
        if response.getcode() != 200:
            raise SafetyStop("Finance safety guard unavailable")
        raw = response.read(262145)
        if len(raw) > 262144:
            raise SafetyStop("Finance safety guard response exceeds bound")
    return json.loads(raw)


def supervisor_guard(config, token, opener=None):
    """Version via default info, actual options via Finance's token-free guard."""
    config_validate(config)
    if not isinstance(token, str) or not token:
        raise SafetyStop("Supervisor token unavailable for Finance safety guard")
    try:
        info = _guard_get("http://supervisor/addons/" + config["addon_slug"] + "/info",
                          {"Authorization": "Bearer " + token}, opener)
        hostname = validate_finance_info(info, config)
        # Never send the Supervisor bearer token to the Finance process.
        guard = _guard_get("http://" + hostname + ":8785/health/backup-guard", {}, opener)
        validate_backup_guard(guard, info["data"]["version"])
    except Exception:
        # Includes parser and HTTP errors; never forward option/credential bodies.
        raise SafetyStop("Finance safety guard unavailable or unapproved metadata") from None


def agents(config):
    return {config["local_agent_id"], config["drive_agent_id"]}


def generation(config, name):
    return {"type": "backup/generate", "agent_ids": sorted(agents(config)),
            "include_addons": [config["addon_slug"]], "include_all_addons": False,
            "include_homeassistant": False, "include_database": False,
            "include_folders": [], "name": name}


def runtime_secret(config, response):
    """Do not return/configure any fallback secret or invoke automatic settings."""
    settings = response["config"]
    secret = settings["create_backup"].get("password")
    if not isinstance(secret, str) or not secret.strip():
        raise SafetyStop("HA backup encryption key must already exist")
    # All agents: native finish and the independent days timer can prune backups,
    # including foreign automatic backups even when the native schedule is Never.
    retention = settings["retention"]
    if any(retention.get(limit) is not None for limit in ("copies", "days")) or any(
        (a.get("retention") or {}).get(limit) is not None
        for a in settings["agents"].values() for limit in ("copies", "days")
    ):
        raise SafetyStop("Native retention could prune unrelated backups")
    if any(settings["agents"].get(a, {}).get("protected") is not True
           for a in agents(config)):
        raise SafetyStop("Both configured agents must require encryption")
    return secret


def inspect_configuration(*, socket_factory=None, environ=None):
    """One authentication, exactly two reads, whitelisted output, no files."""
    token = (os.environ if environ is None else environ).get("SUPERVISOR_TOKEN")
    if not isinstance(token, str) or not token:
        raise SafetyStop("Supervisor token unavailable for inspection")
    socket = None
    try:
        if socket_factory is None:
            import websocket
            websocket.enableTrace(False)
            socket_factory = websocket.create_connection
        socket = socket_factory("ws://supervisor/core/websocket", timeout=30)
        api = HA(socket)
        api.authenticate(token)
        token = None
        listed = api.command({"type": "backup/agents/info"})["agents"]
        settings = api.command({"type": "backup/config/info"})["config"]
        ids = [a["agent_id"] for a in listed]
        if (any(not isinstance(a, str) or not a for a in ids)
                or len(set(ids)) != len(ids)):
            raise SafetyStop("Invalid backup agent metadata")
        configured_key = settings["create_backup"].get("password")
        pruning = any(settings["retention"].get(k) is not None for k in ("copies", "days")) or any(
            (a.get("retention") or {}).get(k) is not None
            for a in settings["agents"].values() for k in ("copies", "days"))
        protected = {}
        for agent_id in sorted(ids):
            value = settings["agents"].get(agent_id, {}).get("protected")
            protected[agent_id] = value if type(value) is bool else None
        return {"agent_ids": sorted(ids), "ha_version": api.ha_version,
                "key_configured": isinstance(configured_key, str) and bool(configured_key.strip()),
                "native_pruning_configured": pruning, "agent_protected": protected}
    except Exception:
        raise SafetyStop("Inspection failed; no configuration or credential details emitted") from None
    finally:
        if socket is not None:
            try:
                socket.close()
            except Exception:
                raise SafetyStop("Inspection connection cleanup failed; no details emitted") from None


def initialize_native_ui(*, socket_factory=None, environ=None):
    """One bounded flag update, verified against a complete in-memory snapshot."""
    token = (os.environ if environ is None else environ).get("SUPERVISOR_TOKEN")
    if not isinstance(token, str) or not token:
        raise SafetyStop("Native UI initialization stopped; outcome uncertain")
    socket = None
    try:
        if socket_factory is None:
            import websocket
            websocket.enableTrace(False)
            socket_factory = websocket.create_connection
        socket = socket_factory("ws://supervisor/core/websocket", timeout=30)
        api = HA(socket)
        api.authenticate(token)
        token = None
        before = deepcopy(api.command({"type": "backup/config/info"})["config"])
        key = before["create_backup"]["password"]
        if (not isinstance(key, str) or not key.strip()
                or before["schedule"]["recurrence"] != "never"
                or type(before["automatic_backups_configured"]) is not bool
                or any(before["retention"][limit] is not None for limit in ("copies", "days"))
                or any(a["retention"] is not None and any(
                    a["retention"][limit] is not None for limit in ("copies", "days"))
                    for a in before["agents"].values())):
            raise SafetyStop("Native UI initialization stopped; outcome uncertain")
        key = None
        if not before["automatic_backups_configured"]:
            api.command({"type": "backup/config/update", "automatic_backups_configured": True})
        after = api.command({"type": "backup/config/info"})["config"]
        expected = before | {"automatic_backups_configured": True}
        if json.dumps(after, sort_keys=True) != json.dumps(expected, sort_keys=True):
            raise SafetyStop("Native UI initialization stopped; outcome uncertain")
    except Exception:
        raise SafetyStop("Native UI initialization stopped; outcome uncertain") from None
    finally:
        if socket is not None:
            try:
                socket.close()
            except Exception:
                raise SafetyStop("Native UI initialization stopped; outcome uncertain") from None


def matches(backup, owned, config):
    """Require every safety field; omissions never mean False/empty by default."""
    try:
        return (
            backup["backup_id"] == owned["backup_id"]
            and backup["name"] == owned["name"]
            and utc(backup["date"]) == utc(owned["created_utc"])
            and backup["homeassistant_included"] is False
            and backup["database_included"] is False
            and backup["homeassistant_version"] is None
            and backup["folders"] == []
            and [a["slug"] for a in backup["addons"]] == [config["addon_slug"]]
            and backup["failed_addons"] == [] and backup["failed_folders"] == []
            and backup["failed_agent_ids"] == []
            and set(backup["agents"]) == agents(config)
            and all(a["protected"] is True and type(a["size"]) is int
                    and a["size"] > 0 for a in backup["agents"].values())
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


class State:
    def __init__(self, path, config):
        self.path = Path(path)
        self.config = config
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
            if (not isinstance(self.data, dict)
                    or set(self.data) != {"version", "config", "owned", "pending", "delete_intent"}
                    or type(self.data["version"]) is not int or self.data["version"] != 1
                    or self.data["config"] != config
                    or not isinstance(self.data["owned"], list)):
                raise SafetyStop("State schema/config mismatch")
            ids = []
            for owned in self.data["owned"]:
                if (set(owned) != {"backup_id", "created_utc", "name"}
                        or not isinstance(owned["backup_id"], str)
                        or not owned["backup_id"] or not isinstance(owned["name"], str)
                        or not owned["name"].startswith("finance-only-")):
                    raise SafetyStop("Invalid ownership state")
                utc(owned["created_utc"])
                ids.append(owned["backup_id"])
            if len(ids) != len(set(ids)):
                raise SafetyStop("Duplicate ownership state")
        else:
            self.data = {"version": 1, "config": config, "owned": [],
                         "pending": None, "delete_intent": None}

    def save(self):
        temp = self.path.with_name("." + self.path.name + "." + uuid.uuid4().hex + ".tmp")
        # Parent must already exist in the operator-controlled local state path.
        # O_EXCL refuses an existing file or symlink; 0600 applies at creation.
        # If creation fails, do not remove an existing entry that we did not own.
        descriptor = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = None  # fdopen now owns and closes the descriptor.
                json.dump(self.data, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
            if os.name == "posix":
                directory = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                temp.unlink()
            except OSError:
                pass  # After replace it is absent; cleanup must preserve errors.

    def require_clear(self):
        if self.data["pending"] is not None or self.data["delete_intent"] is not None:
            raise SafetyStop("Uncertain prior operation: manual reconciliation required")


@contextmanager
def lock(path):
    """Exclusive file lock; crash leaves a lock requiring operator inspection."""
    lock_path = Path(str(path) + ".lock")
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise SafetyStop("Controller locked: inspect earlier operation") from None
    try:
        os.write(descriptor, str(os.getpid()).encode("ascii"))
        os.fsync(descriptor)
        yield
    finally:
        os.close(descriptor)
        lock_path.unlink()


class HA:
    """Tiny strict WS client. Never reconnect or retry a mutation."""
    def __init__(self, socket, timeout=1800):
        self.socket = socket
        self.deadline = time.monotonic() + timeout
        self.sequence = 0
        self.events = []
        self.subscription = None

    def receive(self):
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise SafetyStop("Overall timeout; inspect pending operation")
        socket = self.socket

        def abort_receive():
            try:
                socket.abort()
            except Exception:
                # Transport details may contain secrets; never emit them.
                pass

        timer = threading.Timer(left, abort_receive)
        timer.daemon = True
        try:
            timer.start()
            socket.settimeout(min(left, 30))
            try:
                raw = socket.recv()
            except Exception:
                if time.monotonic() >= self.deadline:
                    raise SafetyStop("Overall timeout; inspect pending operation") from None
                raise
            if time.monotonic() >= self.deadline:
                raise SafetyStop("Overall timeout; inspect pending operation")
        finally:
            timer.cancel()
        message = json.loads(raw)
        if not isinstance(message, dict):
            raise SafetyStop("Invalid WS message")
        return message

    def verify_addon(self, config):
        supervisor_guard(config, os.environ.get("SUPERVISOR_TOKEN"))

    def authenticate(self, token):
        if self.receive().get("type") != "auth_required":
            raise SafetyStop("Unexpected WS authentication")
        self.socket.send(json.dumps({"type": "auth", "access_token": token}))
        result = self.receive()
        if result.get("type") != "auth_ok":
            raise SafetyStop("WS authentication failed")
        if result.get("ha_version") not in {"2026.9.4", "2026.10.2"}:
            raise SafetyStop("Unreviewed HA version")
        self.ha_version = result["ha_version"]

    def command(self, payload):
        self.sequence += 1
        command_id = self.sequence
        self.socket.send(json.dumps(payload | {"id": command_id}))
        while True:
            message = self.receive()
            if message.get("type") == "event":
                if message.get("id") != self.subscription:
                    raise SafetyStop("Unexpected subscription event")
                self.events.append(message["event"])
                continue
            if message.get("id") != command_id or message.get("type") != "result":
                raise SafetyStop("Unexpected WS response")
            if message.get("success") is not True:
                raise SafetyStop("HA command failed; inspect locally")
            return message.get("result")

    def subscribe(self):
        self.subscription = self.sequence + 1
        self.command({"type": "backup/subscribe_events"})
        # Subscription immediately emits last_event; it is NOT our completion.
        self.events.clear()

    def wait_completed(self):
        try:
            from websocket import WebSocketTimeoutException
        except ImportError:
            read_timeouts = (TimeoutError,)
        else:
            read_timeouts = (TimeoutError, WebSocketTimeoutException)
        while True:
            if time.monotonic() >= self.deadline:
                raise SafetyStop("Overall timeout; inspect pending operation")
            try:
                event = self.events.pop(0) if self.events else self.receive()
            except read_timeouts:
                # Keep waiting on this exact connection. No command/auth retry.
                continue
            if event.get("type") == "event":
                if event.get("id") != self.subscription:
                    raise SafetyStop("Unexpected completion subscription")
                event = event["event"]
            if event.get("manager_state") == "create_backup":
                if event.get("state") == "failed":
                    raise SafetyStop("Backup failed; no pruning")
                if event.get("state") == "completed":
                    return
            elif event.get("manager_state") == "idle":
                raise SafetyStop("Idle without observed completion")


def inventory(api):
    result = api.command({"type": "backup/info"})
    if result["agent_errors"] or result["state"] != "idle":
        raise SafetyStop("Backup inventory unavailable or manager busy")
    return result["backups"]


def run(api, state, now, apply_retention=False, *, run_now=False, retained_backups=2):
    state.require_clear()
    if type(retained_backups) is not int or not 2 <= retained_backups <= 100:
        raise SafetyStop("Retained backup count must be an integer from 2 to 100")
    config = state.config
    # Fresh installed version + actual runtime options, not a one-time approval.
    # Must precede pending state and every generate/delete operation in this run.
    api.verify_addon(config)
    available = api.command({"type": "backup/agents/info"})["agents"]
    listed = [a["agent_id"] for a in available]
    if len(listed) != len(set(listed)) or not agents(config).issubset(listed):
        raise SafetyStop("Configured agents unavailable or ambiguous")
    secret = runtime_secret(config, api.command({"type": "backup/config/info"}))
    before = inventory(api)
    before_ids = {b["backup_id"] for b in before}
    owned = state.data["owned"]
    if any(utc(o["created_utc"]) > now for o in owned):
        raise SafetyStop("Clock precedes owned backup")
    if owned and not run_now and now - max(utc(o["created_utc"]) for o in owned) < timedelta(days=7):
        return "not_due"
    api.subscribe()
    name = "finance-only-" + uuid.uuid4().hex
    state.data["pending"] = {"name": name, "started_utc": now.isoformat()}
    state.save()  # Persist intent BEFORE send, including unknown response cases.
    request = generation(config, name)
    request["password"] = secret
    try:
        response = api.command(request)
    finally:
        request.pop("password", None)
        secret = None
    if not isinstance(response, dict) or not response.get("backup_job_id"):
        raise SafetyStop("Missing job ID; backup ID must not be guessed")
    state.data["pending"]["backup_job_id"] = response["backup_job_id"]
    state.save()
    api.wait_completed()
    after = inventory(api)
    candidates = [b for b in after if b["backup_id"] not in before_ids and b["name"] == name]
    if len(candidates) != 1:
        raise SafetyStop("Cannot identify exactly one new backup")
    backup = candidates[0]
    record = {"backup_id": backup["backup_id"], "name": name, "created_utc": backup["date"]}
    if (not now - timedelta(minutes=5) <= utc(backup["date"]) <= now + timedelta(minutes=35)
            or not matches(backup, record, config)):
        raise SafetyStop("Completed backup metadata/content/encryption mismatch")
    details = api.command({"type": "backup/details", "backup_id": record["backup_id"]})
    if details["agent_errors"] or not matches(details["backup"], record, config):
        raise SafetyStop("Backup detail verification failed")
    owned.append(record)
    state.data["pending"] = None
    state.save()
    if apply_retention:
        prune(api, state, retained_backups)
    return "completed"


def prune(api, state, retained_backups=2):
    state.require_clear()
    if type(retained_backups) is not int or not 2 <= retained_backups <= 100:
        raise SafetyStop("Retained backup count must be an integer from 2 to 100")
    owned = state.data["owned"]
    if len(owned) <= retained_backups:
        return
    ordered = sorted(owned, key=lambda o: (utc(o["created_utc"]), o["backup_id"]), reverse=True)
    # Verify all retained recovery points and deletion candidates before the
    # first mutation. Each point must still have both encrypted physical copies.
    for record in ordered:
        details = api.command({"type": "backup/details", "backup_id": record["backup_id"]})
        if details["agent_errors"] or not matches(details["backup"], record, state.config):
            raise SafetyStop("Owned backup changed or unavailable; no deletion")
    for old in ordered[retained_backups:]:
        api.verify_addon(state.config)
        state.data["delete_intent"] = dict(old)
        state.save()  # Keep on interrupted/partial deletion: no automatic retry.
        result = api.command({"type": "backup/delete", "backup_id": old["backup_id"]})
        if result["agent_errors"]:
            raise SafetyStop("Partial deletion; manual reconciliation required")
        remaining = inventory(api)
        if any(b["backup_id"] == old["backup_id"] for b in remaining):
            raise SafetyStop("Deletion not verified")
        owned.remove(old)
        state.data["delete_intent"] = None
        state.save()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--run-once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply-retention", action="store_true")
    parser.add_argument("--retained-backups", type=int, default=2)
    parser.add_argument("--run-now", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        config_validate(config)
        if not 2 <= args.retained_backups <= 100:
            raise SafetyStop("Retained backup count must be from 2 to 100")
        if not args.run_once or args.dry_run:
            print(json.dumps({"mode": "offline-plan", "generate": generation(config, "finance-only-<random>"),
                              "retained_backups": args.retained_backups, "retention_enabled": False}))
            return 0
        token = os.environ.get("SUPERVISOR_TOKEN")
        if not token:
            raise SafetyStop("SUPERVISOR_TOKEN unavailable")
        # Import/network are unreachable for default/dry-run and unit tests.
        import websocket
        websocket.enableTrace(False)
        with lock(args.state):
            state = State(args.state, config)
            state.require_clear()
            socket = websocket.create_connection("ws://supervisor/core/websocket", timeout=30)
            try:
                api = HA(socket)
                api.authenticate(token)
                token = None
                print(run(api, state, datetime.now(timezone.utc), args.apply_retention,
                          run_now=args.run_now, retained_backups=args.retained_backups))
            finally:
                socket.close()
        return 0
    except SafetyStop as error:
        print("STOP: " + str(error))
        return 2
    except Exception:
        # Transport exception bodies can echo headers/tokens or remote data.
        print("STOP: operation uncertain; inspect local state without exposing secrets")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
