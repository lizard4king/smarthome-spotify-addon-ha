"""Synthetic tests only: no HA installation, credentials, imports, or network."""
import copy
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import controller as c

CFG = {"addon_slug": "fake_finance", "local_agent_id": "fake.local", "drive_agent_id": "fake.drive"}
NOW = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)


def settings():
    return {"config": {"create_backup": {"password": "SYNTHETIC_RUNTIME_ONLY"},
                       "retention": {"copies": None},
                       "agents": {a: {"protected": True, "retention": None} for a in c.agents(CFG)}}}


def backup(identifier="new", name="finance-only-new", date=NOW):
    return {"backup_id": identifier, "name": name, "date": date.isoformat(),
            "homeassistant_included": False, "database_included": False,
            "homeassistant_version": None, "folders": [],
            "addons": [{"slug": CFG["addon_slug"], "name": "Synthetic", "version": "1"}],
            "failed_addons": [], "failed_folders": [], "failed_agent_ids": [],
            "agents": {a: {"protected": True, "size": 123} for a in c.agents(CFG)}}


def owned(b):
    return {"backup_id": b["backup_id"], "name": b["name"], "created_utc": b["date"]}


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.backups = []
        self.config = settings()
        self.fail_at = None
        self.mutate_new = lambda b: None
        self.on_delete = lambda: None
        self.finance_info = {"result": "ok", "data": {"version": "0.2.69", "hostname": "fake-finance", "options": {}}}
        self.backup_guard = {"schema_version": 1, "backup_safe": True, "addon_version": "0.2.69"}

    def verify_addon(self, config):
        c.validate_finance_info(self.finance_info, config)
        c.validate_backup_guard(self.backup_guard, self.finance_info["data"]["version"])

    def command(self, payload):
        self.calls.append(copy.deepcopy(payload))
        kind = payload["type"]
        if kind == self.fail_at:
            raise ConnectionError("synthetic connection dropped")
        if kind == "backup/agents/info":
            return {"agents": [{"agent_id": a} for a in c.agents(CFG)]}
        if kind == "backup/config/info":
            return copy.deepcopy(self.config)
        if kind == "backup/info":
            return {"agent_errors": {}, "state": "idle", "backups": copy.deepcopy(self.backups)}
        if kind == "backup/generate":
            b = backup(name=payload["name"])
            self.mutate_new(b)
            self.backups.append(b)
            return {"backup_job_id": "job-is-not-backup-id"}
        if kind == "backup/details":
            found = next((b for b in self.backups if b["backup_id"] == payload["backup_id"]), None)
            return {"agent_errors": {}, "backup": copy.deepcopy(found)}
        if kind == "backup/delete":
            self.on_delete()
            self.backups = [b for b in self.backups if b["backup_id"] != payload["backup_id"]]
            return {"agent_errors": {}}
        raise AssertionError(kind)

    def subscribe(self):
        pass

    def wait_completed(self):
        if self.fail_at == "completion":
            raise ConnectionError("synthetic interruption")


class FakeSocket:
    def __init__(self, messages):
        self.messages = iter(messages)
        self.sent = []

    def send(self, value):
        self.sent.append(json.loads(value))

    def recv(self):
        value = next(self.messages)
        if isinstance(value, Exception):
            raise value
        return json.dumps(value)

    def settimeout(self, value):
        assert 0 < value <= 30

    def abort(self):
        self.aborted = True

    def close(self):
        self.closed = True


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.path = Path(self.temp.name) / "state.json"
        self.state = c.State(self.path, CFG)
        self.api = FakeAPI()

    def tearDown(self):
        self.temp.cleanup()

    def deletes(self):
        return [p for p in self.api.calls if p["type"] == "backup/delete"]

    def test_exact_content_and_success_without_retention(self):
        self.assertEqual(c.run(self.api, self.state, NOW), "completed")
        request = next(p for p in self.api.calls if p["type"] == "backup/generate")
        self.assertEqual(set(request), {"type", "agent_ids", "include_addons", "include_all_addons",
                                       "include_homeassistant", "include_database", "include_folders", "name", "password"})
        self.assertEqual(request["include_addons"], [CFG["addon_slug"]])
        self.assertEqual(set(request["agent_ids"]), c.agents(CFG))
        for key in ("include_homeassistant", "include_database", "include_all_addons"):
            self.assertIs(request[key], False)
        self.assertEqual(request["include_folders"], [])
        self.assertNotIn("SYNTHETIC_RUNTIME_ONLY", self.path.read_text())
        self.assertEqual(self.state.data["owned"][0]["backup_id"], "new")
        self.assertFalse(self.deletes())

    def test_no_key_no_encryption_or_native_copy_limit_blocks_generation(self):
        for variant in ("missing", "unprotected", "globalcopies", "foreigncopies"):
            self.api.config = settings()
            if variant == "missing": self.api.config["config"]["create_backup"]["password"] = None
            if variant == "unprotected": self.api.config["config"]["agents"][CFG["drive_agent_id"]]["protected"] = False
            if variant == "globalcopies": self.api.config["config"]["retention"]["copies"] = 99
            if variant == "foreigncopies": self.api.config["config"]["agents"]["foreign"] = {"retention": {"copies": 1}}
            self.api.calls.clear()
            with self.subTest(variant=variant), self.assertRaises(c.SafetyStop):
                c.run(self.api, self.state, NOW, True)
            self.assertFalse(any(p["type"] == "backup/generate" for p in self.api.calls))

    def test_interrupted_generate_or_completion_stays_pending_and_cannot_retry_or_delete(self):
        for where in ("backup/generate", "completion"):
            self.state = c.State(self.path, CFG)
            self.state.data["pending"] = None
            self.api.fail_at = where
            with self.assertRaises(ConnectionError): c.run(self.api, self.state, NOW, True)
            reloaded = c.State(self.path, CFG)
            self.assertIsNotNone(reloaded.data["pending"])
            self.api.calls.clear()
            with self.assertRaises(c.SafetyStop): c.run(self.api, reloaded, NOW, True)
            self.assertFalse(self.api.calls)

    def test_partial_or_wrong_content_never_claimed_and_never_pruned(self):
        changes = [lambda b: b["agents"].pop(CFG["drive_agent_id"]),
                   lambda b: b.update(homeassistant_included=True),
                   lambda b: b["failed_addons"].append({"slug": "failed"}),
                   lambda b: b["agents"][CFG["drive_agent_id"]].update(protected=False)]
        for change in changes:
            self.state.data["pending"] = None
            self.api.backups.clear()
            self.api.mutate_new = change
            with self.assertRaises(c.SafetyStop): c.run(self.api, self.state, NOW, True)
            self.assertFalse(self.state.data["owned"])
            self.assertFalse(self.deletes())

    def test_retention_keeps_two_newest_owned_and_foreign(self):
        old = backup("old", "finance-only-old", NOW - timedelta(days=85))
        edge = backup("edge", "finance-only-edge", NOW - timedelta(days=84))
        latest = backup("latest", "finance-only-latest", NOW - timedelta(days=83))
        foreign = backup("foreign", "finance-only-foreign", NOW - timedelta(days=200))
        self.api.backups = [old, edge, latest, foreign]
        self.state.data["owned"] = [owned(b) for b in [old, edge, latest]]
        def check_intent():
            self.assertEqual(json.loads(self.path.read_text())["delete_intent"]["backup_id"], "old")
        self.api.on_delete = check_intent
        c.prune(self.api, self.state)
        self.assertEqual(self.deletes(), [{"type": "backup/delete", "backup_id": "old"}])
        self.assertEqual({b["backup_id"] for b in self.api.backups}, {"edge", "latest", "foreign"})

    def test_retention_rotates_only_after_new_verified_backup(self):
        old = backup("old", "finance-only-old", NOW - timedelta(days=15))
        previous = backup("previous", "finance-only-previous", NOW - timedelta(days=8))
        self.api.backups = [old, previous]
        self.state.data["owned"] = [owned(old), owned(previous)]
        self.assertEqual(c.run(self.api, self.state, NOW, True), "completed")
        self.assertEqual({b["backup_id"] for b in self.api.backups}, {"previous", "new"})
        self.assertEqual({b["backup_id"] for b in self.state.data["owned"]}, {"previous", "new"})

    def test_retention_requires_both_recovery_points_and_valid_count(self):
        old = backup("old", "finance-only-old", NOW - timedelta(days=15))
        previous = backup("previous", "finance-only-previous", NOW - timedelta(days=8))
        latest = backup("latest", "finance-only-latest", NOW)
        self.state.data["owned"] = [owned(old), owned(previous), owned(latest)]
        self.api.backups = [old, previous, latest]
        for count in (False, 1, 0, 101, "2"):
            with self.subTest(count=count), self.assertRaises(c.SafetyStop):
                c.prune(self.api, self.state, count)
        self.api.backups.remove(previous)
        with self.assertRaises(c.SafetyStop):
            c.prune(self.api, self.state)
        self.assertFalse(self.deletes())

    def test_configured_count_three_keeps_three_complete_backups(self):
        records = [backup(str(i), "finance-only-" + str(i), NOW - timedelta(days=4-i))
                   for i in range(4)]
        self.api.backups = records.copy()
        self.state.data["owned"] = [owned(b) for b in records]
        c.prune(self.api, self.state, 3)
        self.assertEqual({b["backup_id"] for b in self.api.backups}, {"1", "2", "3"})
        self.assertEqual([p["backup_id"] for p in self.deletes()], ["0"])

    def test_stale_foreign_or_extra_agent_metadata_blocks_deletion(self):
        for field in ("name", "date", "addon", "extraagent", "missing"):
            old = backup("old", "finance-only-old", NOW - timedelta(days=90))
            middle = backup("middle", "finance-only-middle", NOW - timedelta(days=1))
            latest = backup("latest", "finance-only-latest", NOW)
            self.state.data["owned"] = [owned(old), owned(middle), owned(latest)]
            if field == "name": old["name"] = "foreign"
            if field == "date": old["date"] = NOW.isoformat()
            if field == "addon": old["addons"] = [{"slug": "other"}]
            if field == "extraagent": old["agents"]["third"] = {"protected": True, "size": 5}
            self.api.backups = [middle, latest] if field == "missing" else [old, middle, latest]
            with self.subTest(field=field), self.assertRaises(c.SafetyStop): c.prune(self.api, self.state)
            self.assertFalse(self.deletes())

    def test_delete_interruption_journals_and_blocks_future_mutation(self):
        old = backup("old", "finance-only-old", NOW - timedelta(days=90))
        middle = backup("middle", "finance-only-middle", NOW - timedelta(days=1))
        latest = backup("latest", "finance-only-latest", NOW)
        self.api.backups = [old, middle, latest]
        self.state.data["owned"] = [owned(old), owned(middle), owned(latest)]
        self.api.fail_at = "backup/delete"
        with self.assertRaises(ConnectionError): c.prune(self.api, self.state)
        reloaded = c.State(self.path, CFG)
        self.assertEqual(reloaded.data["delete_intent"], owned(old))
        with self.assertRaises(c.SafetyStop): reloaded.require_clear()

    def test_default_and_dry_run_never_network_or_write_state(self):
        cfg = Path(self.temp.name) / "config.json"
        cfg.write_text(json.dumps(CFG))
        for switches in ([], ["--run-once", "--dry-run", "--apply-retention"]):
            with patch.dict("sys.modules", {"websocket": None}), redirect_stdout(io.StringIO()):
                self.assertEqual(c.main(["--config", str(cfg), "--state", str(self.path)] + switches), 0)
        self.assertFalse(self.path.exists())

    def test_ws_auth_version_and_failure_redaction(self):
        socket = FakeSocket([{"type": "auth_required"}, {"type": "auth_ok", "ha_version": "2026.9.4"},
                             {"type": "result", "id": 1, "success": False,
                              "error": {"message": "SYNTHETIC_SECRET"}}])
        api = c.HA(socket)
        api.authenticate("SYNTHETIC_TOKEN")
        with self.assertRaises(c.SafetyStop) as result: api.command({"type": "backup/info"})
        self.assertNotIn("SYNTHETIC_SECRET", str(result.exception))
        with self.assertRaises(c.SafetyStop):
            c.HA(FakeSocket([{"type": "auth_required"}, {"type": "auth_ok", "ha_version": "other"}])).authenticate("fake")

    def test_ws_subscription_discards_stale_completion_and_waits_actual_event(self):
        old = {"type": "event", "id": 1, "event": {"manager_state": "create_backup", "state": "completed"}}
        socket = FakeSocket([old, {"type": "result", "id": 1, "success": True},
                             {"type": "event", "id": 1, "event": {"manager_state": "create_backup", "state": "failed"}}])
        api = c.HA(socket)
        api.subscribe()
        with self.assertRaises(c.SafetyStop): api.wait_completed()

    def test_ws_drop_no_reconnect_or_mutation_retry(self):
        socket = FakeSocket([ConnectionError("drop")])
        with self.assertRaises(ConnectionError): c.HA(socket).command({"type": "backup/delete", "backup_id": "fake"})
        self.assertEqual(len(socket.sent), 1)

    def test_weekly_gate_and_latest_always_kept(self):
        b = backup(date=NOW - timedelta(days=6))
        self.state.data["owned"] = [owned(b)]
        self.assertEqual(c.run(self.api, self.state, NOW, True), "not_due")
        self.assertFalse(any(p["type"] == "backup/generate" for p in self.api.calls))
        self.api.backups = [b]
        c.prune(self.api, self.state)
        self.assertFalse(self.deletes())

    def test_explicit_run_now_bypasses_only_interval_and_keeps_prior_ownership(self):
        old = backup(identifier="prior", date=NOW - timedelta(hours=1))
        self.state.data["owned"] = [owned(old)]
        self.api.backups = [old]
        self.assertEqual(c.run(self.api, self.state, NOW, run_now=True), "completed")
        self.assertEqual({record["backup_id"] for record in self.state.data["owned"]}, {"prior", "new"})
        self.assertEqual(len([p for p in self.api.calls if p["type"] == "backup/generate"]), 1)
        self.assertFalse(self.deletes())
        self.state.data["pending"] = {"name": "uncertain"}
        calls = len(self.api.calls)
        with self.assertRaises(c.SafetyStop): c.run(self.api, self.state, NOW, run_now=True)
        self.assertEqual(len(self.api.calls), calls)

    def test_approved_versions_require_exact_supervisor_and_guard_agreement(self):
        class Response(io.BytesIO):
            def getcode(self): return 200
        versions = ("0.2.69", "0.2.70", "0.2.71", "0.2.72", "0.2.73", "0.2.74", "0.2.75", "0.2.76")
        for installed in versions:
            for reported in versions:
                self.api.finance_info["data"]["version"] = installed
                self.api.backup_guard["addon_version"] = reported
                count = []
                def opener(request, timeout):
                    count.append(request.full_url)
                    payload = self.api.finance_info if len(count) == 1 else self.api.backup_guard
                    return Response(json.dumps(payload).encode())
                with self.subTest(installed=installed, reported=reported):
                    if installed == reported and installed != "0.2.76":
                        c.supervisor_guard(CFG, "SYNTHETIC_TOKEN", opener)
                    else:
                        with self.assertRaises(c.SafetyStop): c.supervisor_guard(CFG, "SYNTHETIC_TOKEN", opener)
                self.assertEqual(len(count), 1 if installed == "0.2.76" else 2)

    def test_lock_and_invalid_config_fail_closed(self):
        with c.lock(self.path):
            with self.assertRaises(c.SafetyStop):
                with c.lock(self.path): pass
        for config in ({}, CFG | {"drive_agent_id": CFG["local_agent_id"]}, CFG | {"unexpected": "value"}):
            with self.assertRaises(c.SafetyStop): c.config_validate(config)

    def test_journal_write_failure_prevents_generate_and_delete(self):
        with patch.object(self.state, "save", side_effect=OSError("synthetic disk full")):
            with self.assertRaises(OSError): c.run(self.api, self.state, NOW, True)
        self.assertFalse(any(p["type"] == "backup/generate" for p in self.api.calls))
        self.state.data["pending"] = None
        old = backup("old", "finance-only-old", NOW - timedelta(days=90))
        middle = backup("middle", "finance-only-middle", NOW - timedelta(days=1))
        latest = backup("latest", "finance-only-latest", NOW)
        self.api.backups = [old, middle, latest]
        self.state.data["owned"] = [owned(old), owned(middle), owned(latest)]
        with patch.object(self.state, "save", side_effect=OSError("synthetic disk full")):
            with self.assertRaises(OSError): c.prune(self.api, self.state)
        self.assertFalse(self.deletes())

    def test_latest_backup_missing_blocks_all_deletion(self):
        old = backup("old", "finance-only-old", NOW - timedelta(days=90))
        middle = backup("middle", "finance-only-middle", NOW - timedelta(days=1))
        latest = backup("latest", "finance-only-latest", NOW)
        self.api.backups = [old, middle]
        self.state.data["owned"] = [owned(old), owned(middle), owned(latest)]
        with self.assertRaises(c.SafetyStop): c.prune(self.api, self.state)
        self.assertFalse(self.deletes())

    def test_unavailable_agent_and_inventory_error_block_generation(self):
        original = self.api.command
        def absent(payload):
            if payload["type"] == "backup/agents/info": return {"agents": []}
            return original(payload)
        with patch.object(self.api, "command", side_effect=absent):
            with self.assertRaises(c.SafetyStop): c.run(self.api, self.state, NOW, True)
        def unavailable(payload):
            if payload["type"] == "backup/info":
                return {"state": "idle", "agent_errors": {"fake": "offline"}, "backups": []}
            return original(payload)
        with patch.object(self.api, "command", side_effect=unavailable):
            with self.assertRaises(c.SafetyStop): c.run(self.api, self.state, NOW, True)
        self.assertFalse(any(p["type"] == "backup/generate" for p in self.api.calls))

    def test_finance_guard_before_state_or_backup_mutation(self):
        variants = [{}, {"result": "error"},
                    {"result": "ok", "data": {"version": "0.2.68", "hostname": "fake-finance"}},
                    {"result": "ok", "data": {"version": "0.2.71", "hostname": "fake-finance"}},
                    {"result": "ok", "data": {"version": "0.2.69"}},
                    {"result": "ok", "data": {"version": "0.2.69", "hostname": "other-finance"}},
                    {"result": "ok", "data": {"version": "0.2.69", "hostname": "evil/path"}}]
        for info in variants:
            self.api.finance_info = info
            with self.subTest(info=info), self.assertRaises(c.SafetyStop) as error:
                c.run(self.api, self.state, NOW, True)
            self.assertNotIn("SYNTHETIC_SECRET", str(error.exception))
            self.assertEqual(self.api.calls, [])
            self.assertIsNone(self.state.data["pending"])
            self.assertFalse(self.path.exists())

    def test_supervisor_guard_fixed_get_and_redacted_failures(self):
        class Response:
            def __init__(self, data, status=200): self.data, self.status = data, status
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def getcode(self): return self.status
            def read(self, limit): return self.data[:limit]
        captured = []
        def opener(request, timeout):
            captured.append((request, timeout))
            data = self.api.finance_info if len(captured) == 1 else self.api.backup_guard
            return Response(json.dumps(data).encode())
        c.supervisor_guard(CFG, "SYNTHETIC_TOKEN", opener)
        self.assertEqual(len(captured), 2)
        request, timeout = captured[0]
        self.assertEqual(request.full_url, "http://supervisor/addons/fake_finance/info")
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Authorization"), "Bearer SYNTHETIC_TOKEN")
        self.assertEqual(timeout, 20)
        finance_request, finance_timeout = captured[1]
        self.assertEqual(finance_request.full_url, "http://fake-finance:8785/health/backup-guard")
        self.assertEqual(finance_request.get_method(), "GET")
        self.assertEqual(finance_request.header_items(), [])
        self.assertEqual(finance_timeout, 20)
        for response in (Response(b"not-json"), Response(b"{}", 503), Response(b"x" * 262145)):
            with self.assertRaises(c.SafetyStop) as error:
                c.supervisor_guard(CFG, "SYNTHETIC_TOKEN", lambda *a, **k: response)
            self.assertNotIn("SYNTHETIC_TOKEN", str(error.exception))
        def failed(*args, **kwargs): raise OSError("SYNTHETIC_TOKEN SYNTHETIC_SECRET")
        with self.assertRaises(c.SafetyStop) as error: c.supervisor_guard(CFG, "SYNTHETIC_TOKEN", failed)
        self.assertNotIn("SYNTHETIC", str(error.exception))

    def test_finance_public_guard_exact_contract_blocks_before_any_mutation(self):
        good = dict(self.api.backup_guard)
        variants = [{}, good | {"backup_safe": False}, good | {"backup_safe": 1},
                    good | {"schema_version": True}, good | {"schema_version": 2},
                    good | {"addon_version": "0.2.68"}, good | {"extra": "forbidden"}]
        for value in variants:
            self.api.backup_guard = value
            with self.subTest(value=value), self.assertRaises(c.SafetyStop):
                c.run(self.api, self.state, NOW, True)
            self.assertFalse(self.api.calls)
            self.assertFalse(self.path.exists())

    def test_supervisor_unapproved_hostname_never_receives_token_free_followup(self):
        calls = []
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def getcode(self): return 200
            def read(self, limit):
                return json.dumps({"result": "ok", "data": {"version": "0.2.69", "hostname": "evil.example"}}).encode()
        def opener(request, timeout): calls.append(request); return Response()
        with self.assertRaises(c.SafetyStop): c.supervisor_guard(CFG, "SYNTHETIC_TOKEN", opener)
        self.assertEqual(len(calls), 1)

    def test_slug_path_escape_rejected_before_transport(self):
        for slug in ("../other", "one/two", "one\\two", "fake?x", "fake#x", "fake%2fother", "fake.other"):
            with self.subTest(slug=slug), self.assertRaises(c.SafetyStop):
                c.supervisor_guard(CFG | {"addon_slug": slug}, "SYNTHETIC_TOKEN",
                                   lambda *a, **k: self.fail("Transport reached with invalid slug"))

    def test_all_non_null_intents_including_falsy_values_block_run_and_prune(self):
        for field in ("pending", "delete_intent"):
            for value in ({}, 0, False, "", [], True):
                self.state.data["pending"] = None
                self.state.data["delete_intent"] = None
                self.state.data[field] = value
                self.state.save()
                loaded = c.State(self.path, CFG)
                with self.subTest(field=field, value=value):
                    with self.assertRaises(c.SafetyStop): c.run(self.api, loaded, NOW, True)
                    with self.assertRaises(c.SafetyStop): c.prune(self.api, loaded)
                self.assertFalse(self.api.calls)

    def test_temp_created_exclusive_private_and_failed_write_preserves_old_state(self):
        self.state.save()
        original = self.path.read_bytes()
        real_open = os.open
        observed = []
        def private_open(path, flags, mode=0o777):
            if str(path).endswith(".tmp"):
                self.assertFalse(Path(path).exists())
                self.assertEqual(mode, 0o600)
                self.assertTrue(flags & os.O_EXCL)
                self.assertTrue(flags & os.O_CREAT)
                observed.append(Path(path))
            return real_open(path, flags, mode)
        self.state.data["pending"] = {"name": "synthetic"}
        with patch.object(c.os, "open", side_effect=private_open), patch.object(c.json, "dump", side_effect=OSError("disk full")):
            with self.assertRaises(OSError): self.state.save()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(len(observed), 1)
        self.assertFalse(observed[0].exists())
        # Subsequent writes use different private paths, not a deterministic slot.
        with patch.object(c.os, "open", side_effect=private_open): self.state.save()
        self.assertNotEqual(observed[0], observed[1])

    def test_existing_temp_collision_is_neither_overwritten_nor_removed(self):
        self.state.save()
        original = self.path.read_bytes()
        collision = self.path.with_name("." + self.path.name + ".fixed.tmp")
        collision.write_text("synthetic foreign entry")
        with patch.object(c.uuid, "uuid4", return_value=SimpleNamespace(hex="fixed")):
            with self.assertRaises(FileExistsError): self.state.save()
        self.assertEqual(collision.read_text(), "synthetic foreign entry")
        self.assertEqual(self.path.read_bytes(), original)

    def test_native_days_limits_global_selected_and_foreign_agent_block_generation(self):
        for target in ("global", CFG["local_agent_id"], CFG["drive_agent_id"], "foreign.agent"):
            for limit in (1, 84, 0, False, ""):
                self.api.config = settings()
                self.api.calls.clear()
                if target == "global":
                    self.api.config["config"]["retention"]["days"] = limit
                else:
                    self.api.config["config"]["agents"].setdefault(target, {})["retention"] = {"days": limit}
                with self.subTest(target=target, limit=limit), self.assertRaises(c.SafetyStop):
                    c.run(self.api, self.state, NOW, True)
                self.assertFalse(any(p["type"] == "backup/generate" for p in self.api.calls))
                self.assertFalse(self.path.exists())

    def test_inspection_auth_once_exact_two_reads_and_whitelisted_redacted_output(self):
        config = settings()
        config["config"]["agents"]["foreign"] = {"retention": {"days": 84}}
        socket = FakeSocket([{"type": "auth_required"}, {"type": "auth_ok", "ha_version": "2026.9.4"},
                             {"type": "result", "id": 1, "success": True,
                              "result": {"agents": [{"agent_id": a, "name": "private@example.invalid"} for a in c.agents(CFG)]}},
                             {"type": "result", "id": 2, "success": True, "result": config}])
        connections = []
        def factory(url, timeout): connections.append((url, timeout)); return socket
        result = c.inspect_configuration(socket_factory=factory, environ={"SUPERVISOR_TOKEN": "SYNTHETIC_TOKEN"})
        self.assertEqual(len(connections), 1)
        self.assertEqual([p["type"] for p in socket.sent], ["auth", "backup/agents/info", "backup/config/info"])
        self.assertEqual(set(result), {"agent_ids", "ha_version", "key_configured", "native_pruning_configured", "agent_protected"})
        self.assertEqual(result["agent_ids"], sorted(c.agents(CFG)))
        self.assertIs(result["key_configured"], True)
        self.assertIs(result["native_pruning_configured"], True)
        self.assertEqual(result["agent_protected"], {a: True for a in c.agents(CFG)})
        self.assertNotIn("SYNTHETIC", json.dumps(result))
        self.assertNotIn("private@example.invalid", json.dumps(result))
        self.assertTrue(socket.closed)
        self.assertFalse(self.path.exists())

    def test_inspection_auth_failure_never_reads_or_reveals_error_body(self):
        socket = FakeSocket([{"type": "auth_required"}, {"type": "auth_invalid", "message": "SYNTHETIC_TOKEN"}])
        with self.assertRaises(c.SafetyStop) as error:
            c.inspect_configuration(socket_factory=lambda *a, **k: socket, environ={"SUPERVISOR_TOKEN": "SYNTHETIC_TOKEN"})
        self.assertEqual([p["type"] for p in socket.sent], ["auth"])
        self.assertNotIn("SYNTHETIC_TOKEN", str(error.exception))
        self.assertTrue(socket.closed)


class InitializationTests(unittest.TestCase):
    def config(self):
        return {"automatic_backups_configured": False,
                "create_backup": {"password": "SYNTHETIC_KEY", "agent_ids": []},
                "schedule": {"recurrence": "never", "days": [], "time": None},
                "retention": {"copies": None, "days": None},
                "agents": {"foreign": {"protected": False, "retention": {"copies": None, "days": None}}},
                "future_field": {"preserve": [True, 1, None]}}

    def socket(self, config, after=None, update_result=None):
        messages = [{"type": "auth_required"}, {"type": "auth_ok", "ha_version": "2026.9.4"},
                    {"type": "result", "id": 1, "success": True, "result": {"config": config}}]
        if not config["automatic_backups_configured"]:
            messages.append(update_result or {"type": "result", "id": 2, "success": True, "result": None})
        messages.append({"type": "result", "id": len(messages) - 1, "success": True,
                         "result": {"config": after if after is not None else config | {"automatic_backups_configured": True}}})
        return FakeSocket(messages)

    def initialize(self, socket):
        with patch.object(c.Path, "write_text", side_effect=AssertionError("file write")), \
                patch.object(c.os, "open", side_effect=AssertionError("file open")):
            return c.initialize_native_ui(socket_factory=lambda *a, **k: socket,
                                          environ={"SUPERVISOR_TOKEN": "SYNTHETIC_TOKEN"})

    def test_initialize_exact_commands_and_already_initialized_readback(self):
        for initialized in (False, True):
            config = self.config() | {"automatic_backups_configured": initialized}
            socket = self.socket(config)
            with redirect_stdout(io.StringIO()) as output:
                self.assertIsNone(self.initialize(socket))
            expected = ["auth", "backup/config/info"]
            if not initialized:
                expected.append("backup/config/update")
                self.assertEqual(socket.sent[2], {"type": "backup/config/update", "id": 2,
                                                 "automatic_backups_configured": True})
            self.assertEqual([p["type"] for p in socket.sent], expected + ["backup/config/info"])
            self.assertEqual(output.getvalue(), "")
            self.assertTrue(socket.closed)

    def test_initialize_unsafe_schedule_retention_or_missing_key_prevents_update(self):
        unsafe = []
        for key in (None, "", "   "):
            config = self.config(); config["create_backup"]["password"] = key; unsafe.append(config)
        config = self.config(); config["schedule"]["recurrence"] = "daily"; unsafe.append(config)
        for limit in ("copies", "days"):
            for value in (1, 0, False):
                for where in ("global", "foreign"):
                    config = self.config()
                    target = config["retention"] if where == "global" else config["agents"]["foreign"]["retention"]
                    target[limit] = value; unsafe.append(config)
        for config in unsafe:
            socket = self.socket(config)
            with self.assertRaises(c.SafetyStop): self.initialize(socket)
            self.assertEqual([p["type"] for p in socket.sent], ["auth", "backup/config/info"])
            self.assertTrue(socket.closed)

    def test_initialize_any_readback_change_or_failed_update_stops_without_retry(self):
        for field in self.config():
            after = self.config() | {"automatic_backups_configured": True}
            after[field] = "SYNTHETIC_PRIVATE_RESPONSE"
            socket = self.socket(self.config(), after)
            with self.assertRaises(c.SafetyStop) as error: self.initialize(socket)
            self.assertNotIn("SYNTHETIC", str(error.exception))
            self.assertEqual(len([p for p in socket.sent if p["type"] == "backup/config/update"]), 1)
            self.assertTrue(socket.closed)
        failed = {"type": "result", "id": 2, "success": False, "error": {"message": "SYNTHETIC_KEY"}}
        socket = self.socket(self.config(), update_result=failed)
        with self.assertRaises(c.SafetyStop) as error: self.initialize(socket)
        self.assertNotIn("SYNTHETIC", str(error.exception))
        self.assertEqual([p["type"] for p in socket.sent], ["auth", "backup/config/info", "backup/config/update"])

    def test_initialize_auth_failure_has_no_commands_or_private_diagnostics(self):
        socket = FakeSocket([{"type": "auth_required"}, {"type": "auth_invalid", "message": "SYNTHETIC_TOKEN"}])
        with self.assertRaises(c.SafetyStop) as error: self.initialize(socket)
        self.assertEqual([p["type"] for p in socket.sent], ["auth"])
        self.assertNotIn("SYNTHETIC", str(error.exception))
        self.assertTrue(socket.closed)


class CompletionReadTimeoutTests(unittest.TestCase):
    @staticmethod
    def completed():
        return {"manager_state": "create_backup", "state": "completed"}

    def test_python_read_timeouts_continue_on_same_connection_without_commands(self):
        socket = FakeSocket([TimeoutError(), TimeoutError(), self.completed()])
        api = c.HA(socket)
        deadline = api.deadline
        api.wait_completed()
        self.assertIs(api.socket, socket)
        self.assertEqual(api.deadline, deadline)
        self.assertEqual(socket.sent, [])

    def test_actual_websocket_client_timeout_continues(self):
        try:
            from websocket import WebSocketTimeoutException
        except ImportError:
            self.skipTest("websocket-client unavailable; run with runtime dependency for this case")
        socket = FakeSocket([WebSocketTimeoutException("synthetic idle"), self.completed()])
        api = c.HA(socket)
        api.wait_completed()
        self.assertIs(api.socket, socket)
        self.assertEqual(socket.sent, [])

    def test_repeated_read_timeouts_stop_at_original_overall_deadline(self):
        clock = [0]

        class TimedSocket(FakeSocket):
            def recv(self):
                clock[0] += 5
                return super().recv()

        socket = TimedSocket([TimeoutError(), TimeoutError()])
        api = c.HA(socket)
        api.deadline = 10
        with patch.object(c.time, "monotonic", side_effect=lambda: clock[0]), patch.object(c.threading, "Timer"):
            with self.assertRaisesRegex(c.SafetyStop, "Overall timeout"):
                api.wait_completed()
        self.assertEqual(api.deadline, 10)
        self.assertEqual(socket.sent, [])

    def test_other_errors_and_same_named_nonlibrary_error_propagate(self):
        imitation = type("WebSocketTimeoutException", (Exception,), {})
        for error in (ConnectionError("synthetic disconnect"), ValueError("synthetic invalid"), imitation()):
            socket = FakeSocket([error, self.completed()])
            with self.assertRaises(type(error)) as caught:
                c.HA(socket).wait_completed()
            self.assertIs(caught.exception, error)
            self.assertEqual(socket.sent, [])

    def test_failed_and_idle_still_stop_after_timeout(self):
        for event in ({"manager_state": "create_backup", "state": "failed"}, {"manager_state": "idle"}):
            socket = FakeSocket([TimeoutError(), event])
            with self.assertRaises(c.SafetyStop):
                c.HA(socket).wait_completed()
            self.assertEqual(socket.sent, [])

    def test_generate_is_sent_once_before_completion_timeouts(self):
        socket = FakeSocket([
            {"type": "result", "id": 1, "success": True, "result": {"backup_job_id": "synthetic-job"}},
            TimeoutError(), TimeoutError(),
            {"type": "event", "id": 99, "event": self.completed()},
        ])
        api = c.HA(socket)
        api.subscription = 99
        api.command({"type": "backup/generate", "name": "synthetic"})
        api.wait_completed()
        self.assertEqual(socket.sent, [{"type": "backup/generate", "name": "synthetic", "id": 1}])
        self.assertIs(api.socket, socket)

    def test_authentication_read_timeout_is_not_retried(self):
        socket = FakeSocket([{"type": "auth_required"}, TimeoutError()])
        with self.assertRaises(TimeoutError):
            c.HA(socket).authenticate("synthetic-token")
        self.assertEqual(socket.sent, [{"type": "auth", "access_token": "synthetic-token"}])

    def test_command_read_timeout_is_not_retried(self):
        socket = FakeSocket([TimeoutError(), {"type": "result", "id": 1, "success": True}])
        with self.assertRaises(TimeoutError):
            c.HA(socket).command({"type": "backup/generate", "name": "synthetic"})
        self.assertEqual(len(socket.sent), 1)


class AbsoluteReceiveDeadlineTests(unittest.TestCase):
    class BlockingSocket(FakeSocket):
        def __init__(self, control_frames=False, abort_raises=False):
            super().__init__([])
            self.released = threading.Event()
            self.abort_count = 0
            self.control_frames = control_frames
            self.frames = 0
            self.abort_raises = abort_raises

        def recv(self):
            if self.control_frames:
                while not self.released.wait(0.002):
                    self.frames += 1  # Simulate control frames consumed inside recv().
            elif not self.released.wait(2):
                raise AssertionError("Deadline guard failed to unblock recv")
            return json.dumps({"manager_state": "create_backup", "state": "completed"})

        def abort(self):
            self.abort_count += 1
            self.released.set()
            if self.abort_raises:
                raise ConnectionError("SYNTHETIC_PRIVATE_ABORT_DETAIL")

    def test_blocking_receive_and_internal_control_frames_aborted_at_deadline(self):
        for controls in (False, True):
            own = self.BlockingSocket(control_frames=controls)
            other = self.BlockingSocket()
            api = c.HA(own, timeout=0.06)
            before = time.monotonic()
            with self.assertRaisesRegex(c.SafetyStop, "Overall timeout"):
                api.receive()
            elapsed = time.monotonic() - before
            self.assertLess(elapsed, 1)
            self.assertGreaterEqual(elapsed, 0.04)
            self.assertEqual(own.abort_count, 1)
            self.assertEqual(other.abort_count, 0)
            self.assertEqual(own.sent, [])
            if controls:
                self.assertGreater(own.frames, 0)

    def test_late_completed_rejected_before_json_parsing_and_timer_cancelled(self):
        clock = [0]

        class LateSocket(FakeSocket):
            def recv(self):
                clock[0] = 11
                return "invalid JSON deliberately never parsed"

        api = c.HA(LateSocket([])); api.deadline = 10
        with patch.object(c.time, "monotonic", side_effect=lambda: clock[0]), patch.object(c.threading, "Timer") as timer:
            with self.assertRaisesRegex(c.SafetyStop, "Overall timeout"):
                api.wait_completed()
            timer.return_value.cancel.assert_called_once_with()
            self.assertTrue(timer.return_value.daemon)

    def test_timer_cancelled_for_success_transport_and_parse_errors(self):
        for value in ({"valid": True}, ConnectionError("synthetic"), []):
            api = c.HA(FakeSocket([value]))
            with patch.object(c.threading, "Timer") as timer:
                if isinstance(value, Exception):
                    with self.assertRaises(ConnectionError): api.receive()
                elif isinstance(value, list):
                    with self.assertRaises(c.SafetyStop): api.receive()
                else:
                    self.assertEqual(api.receive(), value)
                timer.return_value.start.assert_called_once_with()
                timer.return_value.cancel.assert_called_once_with()
                self.assertTrue(timer.return_value.daemon)

    def test_timer_cancelled_after_invalid_json(self):
        class InvalidJSONSocket(FakeSocket):
            def recv(self):
                return "{"

        with patch.object(c.threading, "Timer") as timer:
            with self.assertRaises(json.JSONDecodeError):
                c.HA(InvalidJSONSocket([])).receive()
            timer.return_value.cancel.assert_called_once_with()

    def test_abort_exception_is_silent_and_deadline_still_stops(self):
        socket = self.BlockingSocket(abort_raises=True)
        output = io.StringIO()
        with redirect_stdout(output), patch("sys.stderr", output):
            with self.assertRaises(c.SafetyStop) as error:
                c.HA(socket, timeout=0.03).receive()
        self.assertEqual(output.getvalue(), "")
        self.assertNotIn("SYNTHETIC", str(error.exception))
        self.assertEqual(socket.sent, [])


if __name__ == "__main__": unittest.main()
