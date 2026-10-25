"""Synthetic options/scheduling/packaging tests; never build or access HA."""
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import controller
import scheduler

HERE = Path(__file__).parent
MANIFEST = json.loads((HERE / "config.json").read_text())
OPTIONS = MANIFEST["options"]
CONFIG = scheduler.options_validate(OPTIONS)
NOW = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=HERE)
        self.data = Path(self.temp.name)
        self.options_path = self.data / "options.json"
        self.state_path = self.data / "controller-state.json"
        self.options_path.write_text(json.dumps(OPTIONS))

    def tearDown(self):
        self.temp.cleanup()

    def state(self, age):
        state = controller.State(self.state_path, CONFIG)
        state.data["owned"] = [{"backup_id": "synthetic", "name": "finance-only-synthetic",
                                 "created_utc": (NOW - age).isoformat()}]
        state.save()
        return state

    def enable(self, retention=False):
        self.options_path.write_text(json.dumps(OPTIONS | {"enabled": True, "apply_retention": retention}))

    def test_disabled_is_offline_and_does_not_write(self):
        def forbidden(*args): self.fail("Disabled branch called execution/sleep")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(scheduler.serve(self.data, clock=forbidden, sleep=forbidden, invoke=forbidden), 0)
        self.assertEqual([p.name for p in self.data.iterdir()], ["options.json"])

    def test_reconciliation_is_explicit_single_shot_without_scheduling(self):
        self.options_path.write_text(json.dumps(OPTIONS | {"enabled": True, "reconcile_pending": True}))
        self.state_path.write_text("preserved-pending-state")
        before = {p.name: p.read_bytes() for p in self.data.iterdir()}
        calls = []
        def recover(config, state_path, now):
            calls.append((config, state_path, now))
            return "Pending backup reconciled; no backup created or deleted"
        def forbidden(*args): self.fail("Reconciliation entered another operation")
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(scheduler.serve(self.data, clock=lambda: NOW, sleep=forbidden,
                                             invoke=forbidden, inspect=forbidden,
                                             initialize=forbidden, reconcile=recover), 0)
        self.assertEqual(calls, [(CONFIG, self.state_path, NOW)])
        self.assertEqual(output.getvalue(), "Pending backup reconciled; no backup created or deleted\n")
        self.assertEqual({p.name: p.read_bytes() for p in self.data.iterdir()}, before)

    def test_reconciliation_disabled_conflicting_or_failed_never_retries(self):
        self.assertIs(OPTIONS["reconcile_pending"], False)
        self.options_path.write_text(json.dumps(OPTIONS | {"reconcile_pending": True}))
        def forbidden(*args): self.fail("Disabled reconciliation attempted work")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(scheduler.serve(self.data, reconcile=forbidden, clock=forbidden), 0)
        for other in ("inspect_only", "initialize_ui"):
            with self.assertRaises(controller.SafetyStop):
                scheduler.options_validate(OPTIONS | {"reconcile_pending": True, other: True})
        self.options_path.write_text(json.dumps(OPTIONS | {"enabled": True, "reconcile_pending": True}))
        calls = []
        def fail(*args):
            calls.append(True)
            raise controller.SafetyStop("Synthetic uncertain recovery")
        with redirect_stdout(io.StringIO()) as output, self.assertRaises(controller.SafetyStop):
            scheduler.serve(self.data, reconcile=fail, clock=lambda: NOW, invoke=forbidden, sleep=forbidden)
        self.assertEqual(calls, [True])
        self.assertEqual(output.getvalue(), "")

    def test_reconciliation_wrapper_uses_one_socket_and_closes_before_success(self):
        controller.State(self.state_path, CONFIG).save()
        class Socket:
            def __init__(self):
                self.frames = [json.dumps({"type": "auth_required"}),
                               json.dumps({"type": "auth_ok", "ha_version": "2026.9.4"})]
                self.sent, self.closed = [], 0
            def settimeout(self, value): pass
            def recv(self): return self.frames.pop(0)
            def send(self, value): self.sent.append(json.loads(value))
            def close(self): self.closed += 1
        socket = Socket()
        calls = []
        def factory(url, timeout): calls.append((url, timeout)); return socket
        with patch("recovery.reconcile", return_value="verified-existing-only") as recover:
            result = scheduler.reconcile_once(CONFIG, self.state_path, NOW,
                       socket_factory=factory, environ={"SUPERVISOR_TOKEN": "synthetic-token"})
        self.assertEqual(result, "verified-existing-only")
        self.assertEqual(calls, [("ws://supervisor/core/websocket", 30)])
        self.assertEqual([message["type"] for message in socket.sent], ["auth"])
        self.assertEqual(socket.closed, 1)
        self.assertEqual(recover.call_count, 1)
        self.assertEqual(recover.call_args.args[1].path, self.state_path)
        self.assertEqual(recover.call_args.args[2], NOW)

    def test_reconciliation_wrapper_missing_prerequisite_or_existing_lock_never_connects(self):
        def forbidden(*args, **kwargs): self.fail("Blocked reconciliation connected")
        with self.assertRaises(controller.SafetyStop):
            scheduler.reconcile_once(CONFIG, self.state_path, NOW, socket_factory=forbidden, environ={})
        controller.State(self.state_path, CONFIG).save()
        lock_path = Path(str(self.state_path) + ".lock")
        lock_path.write_text("synthetic-existing-lock")
        with self.assertRaises(controller.SafetyStop):
            scheduler.reconcile_once(CONFIG, self.state_path, NOW, socket_factory=forbidden,
                                     environ={"SUPERVISOR_TOKEN": "synthetic-token"})
        self.assertEqual(lock_path.read_text(), "synthetic-existing-lock")

    def test_reconciliation_wrapper_redacts_transport_and_cleanup_failure(self):
        controller.State(self.state_path, CONFIG).save()
        class Socket:
            def __init__(self, fail_close):
                self.frames = [json.dumps({"type": "auth_required"}),
                               json.dumps({"type": "auth_ok", "ha_version": "2026.9.4"})]
                self.closed, self.fail_close = 0, fail_close
            def settimeout(self, value): pass
            def recv(self): return self.frames.pop(0)
            def send(self, value): pass
            def close(self):
                self.closed += 1
                if self.fail_close: raise RuntimeError("synthetic-secret-body")
        for failure in ("recover", "close"):
            socket = Socket(failure == "close")
            with self.subTest(failure=failure), patch("recovery.reconcile") as recover:
                if failure == "recover": recover.side_effect = RuntimeError("synthetic-secret-body")
                else: recover.return_value = "must-not-report-success"
                with self.assertRaises(controller.SafetyStop) as caught:
                    scheduler.reconcile_once(CONFIG, self.state_path, NOW,
                        socket_factory=lambda *args, **kwargs: socket,
                        environ={"SUPERVISOR_TOKEN": "synthetic-token"})
                self.assertNotIn("synthetic-secret", str(caught.exception))
                self.assertEqual(socket.closed, 1)
                self.assertFalse(Path(str(self.state_path) + ".lock").exists())

    def test_explicit_run_once_now_preserves_state_and_never_enters_weekly_loop(self):
        self.options_path.write_text(json.dumps(OPTIONS | {"enabled": True, "run_once_now": True}))
        self.state(timedelta(hours=1))
        calls = []
        def execute(args):
            calls.append(args)
            saved = controller.State(self.state_path, CONFIG)
            saved.data["owned"].append({"backup_id": "verified-new", "name": "finance-only-verified-new",
                                        "created_utc": NOW.isoformat()})
            saved.save()
            return 0
        def forbidden(*args): self.fail("One-shot entered periodic scheduling")
        self.assertEqual(scheduler.serve(self.data, clock=lambda: NOW, sleep=forbidden, invoke=execute), 0)
        self.assertEqual(len(calls), 1)
        self.assertIn("--run-now", calls[0])
        self.assertEqual(len(controller.State(self.state_path, CONFIG).data["owned"]), 2)

    def test_run_once_now_cannot_recover_uncertainty_or_accept_old_success(self):
        self.options_path.write_text(json.dumps(OPTIONS | {"enabled": True, "run_once_now": True}))
        saved = self.state(timedelta(hours=1))
        with self.assertRaises(controller.SafetyStop):
            scheduler.serve(self.data, clock=lambda: NOW, invoke=lambda args: 0)
        saved.data["pending"] = {"uncertain": True}
        saved.save()
        def forbidden(*args): self.fail("Forced one-shot bypassed pending state")
        with self.assertRaises(controller.SafetyStop):
            scheduler.serve(self.data, clock=lambda: NOW, invoke=forbidden, sleep=forbidden)
        for mode in ("inspect_only", "initialize_ui", "reconcile_pending"):
            with self.assertRaises(controller.SafetyStop):
                scheduler.options_validate(OPTIONS | {"run_once_now": True, mode: True})

    def test_strict_options_and_distinct_explicit_agents(self):
        bad_options = [OPTIONS | {"enabled": "false"}, OPTIONS | {"apply_retention": 1},
                       OPTIONS | {"backup_key": "forbidden"}, OPTIONS | {"addon_slug": ""},
                       OPTIONS | {"drive_agent_id": OPTIONS["local_agent_id"]}]
        for bad in bad_options:
            with self.subTest(options=bad), self.assertRaises(controller.SafetyStop): scheduler.options_validate(bad)

    def test_weekly_utc_delay_and_one_hour_maximum(self):
        self.assertEqual(scheduler.next_delay(controller.State(self.state_path, CONFIG), NOW), 0)
        self.assertEqual(scheduler.next_delay(self.state(timedelta(days=6)), NOW), 3600)
        self.assertEqual(scheduler.next_delay(self.state(timedelta(days=7) - timedelta(seconds=90)), NOW), 90)
        self.assertEqual(scheduler.next_delay(self.state(timedelta(days=7)), NOW), 0)
        self.assertEqual(scheduler.next_delay(self.state(timedelta(days=50)), NOW), 0)

    def test_uncertain_state_and_backward_clock_stop_before_execution(self):
        self.enable()
        state = self.state(timedelta(days=8))
        state.data["pending"] = {"name": "synthetic-uncertain"}
        state.save()
        def forbidden(*args): self.fail("Uncertain operation retried")
        with self.assertRaises(controller.SafetyStop): scheduler.serve(self.data, clock=lambda: NOW, invoke=forbidden)
        state.data["pending"] = None
        state.save()
        with self.assertRaises(controller.SafetyStop): scheduler.next_delay(state, NOW - timedelta(days=9))

    def test_failure_stops_after_single_attempt_no_sleep_or_retry(self):
        self.enable()
        calls = []
        def fail(args): calls.append(args); return 2
        with redirect_stdout(io.StringIO()):
            self.assertEqual(scheduler.serve(self.data, clock=lambda: NOW, invoke=fail,
                                             sleep=lambda value: self.fail("Retry sleep")), 2)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("--apply-retention", calls[0])

    def test_reboot_due_runs_once_no_catchup_series_then_sleeps(self):
        self.enable(retention=True)
        self.state(timedelta(days=100))
        calls, waits = [], []
        def succeed(args):
            calls.append(args)
            state = controller.State(self.state_path, CONFIG)
            state.data["owned"].append({"backup_id": "new", "name": "finance-only-new", "created_utc": NOW.isoformat()})
            state.save()
            return 0
        def sleep(value):
            waits.append(value)
            self.options_path.write_text(json.dumps(OPTIONS))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(scheduler.serve(self.data, clock=lambda: NOW, invoke=succeed, sleep=sleep), 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(waits, [3600])
        self.assertIn("--apply-retention", calls[0])
        self.assertEqual(json.loads((self.data / "controller-config.json").read_text()), CONFIG)

    def test_restart_not_due_sleeps_without_network_attempt(self):
        self.enable()
        self.state(timedelta(days=1))
        waits = []
        def sleep(value): waits.append(value); self.options_path.write_text(json.dumps(OPTIONS))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(scheduler.serve(self.data, clock=lambda: NOW, sleep=sleep,
                                             invoke=lambda args: self.fail("Too early")), 0)
        self.assertEqual(waits, [3600])

    def test_false_success_without_durable_backup_stops(self):
        self.enable()
        with self.assertRaises(controller.SafetyStop):
            scheduler.serve(self.data, clock=lambda: NOW, invoke=lambda args: 0)

    def test_manifest_minimal_privileges_and_official_dependency(self):
        self.assertIs(MANIFEST["homeassistant_api"], True)
        self.assertIs(MANIFEST["hassio_api"], True)
        self.assertEqual(MANIFEST["hassio_role"], "default")
        self.assertIs(OPTIONS["enabled"], False)
        self.assertIs(OPTIONS["inspect_only"], False)
        self.assertIs(OPTIONS["apply_retention"], False)
        self.assertEqual(OPTIONS["retained_backups"], 2)
        for forbidden in ("ports", "map", "host_network", "docker_api", "privileged", "devices"):
            self.assertNotIn(forbidden, MANIFEST)
        dockerfile = (HERE / "Dockerfile").read_text()
        self.assertIn("apk add --no-cache python3 py3-websocket-client", dockerfile)
        self.assertNotIn("pip", dockerfile)
        for image in json.loads((HERE / "build.json").read_text())["build_from"].values():
            self.assertTrue(image.startswith("ghcr.io/home-assistant/"))

    def test_retention_option_is_bounded_and_passed_only_when_enabled(self):
        legacy = {key: value for key, value in OPTIONS.items() if key != "retained_backups"}
        self.assertEqual(scheduler.options_validate(legacy), CONFIG)
        for value in (False, 1, 0, 101, "2"):
            with self.subTest(value=value), self.assertRaises(controller.SafetyStop):
                scheduler.options_validate(OPTIONS | {"retained_backups": value})
        self.enable(retention=True)
        calls = []
        def invoke(args):
            calls.append(args)
            state = controller.State(self.state_path, CONFIG)
            state.data["owned"].append({"backup_id": "new", "name": "finance-only-new",
                                        "created_utc": NOW.isoformat()})
            state.save()
            return 0
        self.options_path.write_text(json.dumps(OPTIONS | {"enabled": True, "apply_retention": True,
                                                    "retained_backups": 3, "run_once_now": True}))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(scheduler.serve(self.data, clock=lambda: NOW, invoke=invoke), 0)
        self.assertEqual(calls[0][-3:-1], ["--retained-backups", "3"])

    def test_falsy_non_null_intents_block_scheduler_without_execution(self):
        self.enable()
        state = self.state(timedelta(days=8))
        def forbidden(*args): self.fail("Invalid intent triggered an execution or wait")
        for field in ("pending", "delete_intent"):
            for value in ({}, 0, False, "", []):
                state.data["pending"] = None
                state.data["delete_intent"] = None
                state.data[field] = value
                state.save()
                with self.subTest(field=field, value=value), self.assertRaises(controller.SafetyStop):
                    scheduler.serve(self.data, clock=lambda: NOW, invoke=forbidden, sleep=forbidden)

    def test_inspect_only_never_uses_state_or_source_destination_config(self):
        self.options_path.write_text(json.dumps(OPTIONS | {"enabled": True, "inspect_only": True,
                                                           "addon_slug": "../unused", "local_agent_id": "", "drive_agent_id": ""}))
        self.state_path.write_text("invalid-state-must-not-be-read")
        before = {p.name: p.read_bytes() for p in self.data.iterdir()}
        calls = []
        def inspector(): calls.append(True); return {"agent_ids": ["synthetic"], "key_configured": False}
        def forbidden(*args): self.fail("Inspection attempted scheduling or mutation")
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(scheduler.serve(self.data, clock=forbidden, sleep=forbidden,
                                             invoke=forbidden, inspect=inspector), 0)
        self.assertEqual(calls, [True])
        self.assertEqual(json.loads(output.getvalue())["agent_ids"], ["synthetic"])
        self.assertEqual({p.name: p.read_bytes() for p in self.data.iterdir()}, before)

    def test_disabled_inspect_mode_does_not_authenticate(self):
        self.options_path.write_text(json.dumps(OPTIONS | {"inspect_only": True}))
        def forbidden(*args): self.fail("Disabled inspection attempted network")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(scheduler.serve(self.data, inspect=forbidden, invoke=forbidden), 0)


    def test_initialize_ui_is_one_shot_fixed_output_and_no_files(self):
        self.options_path.write_text(json.dumps(OPTIONS | {"enabled": True, "initialize_ui": True,
                                                           "addon_slug": "../unused", "local_agent_id": "", "drive_agent_id": ""}))
        self.state_path.write_text("invalid-state-must-not-be-read")
        before = {p.name: p.read_bytes() for p in self.data.iterdir()}
        calls = []
        def initialize(): calls.append(True)
        def forbidden(*args): self.fail("Initialization attempted another mode")
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(scheduler.serve(self.data, initialize=initialize, inspect=forbidden,
                                             invoke=forbidden, clock=forbidden, sleep=forbidden), 0)
        self.assertEqual(calls, [True])
        self.assertEqual(output.getvalue(), "Native backup UI initialized; no backup created\n")
        self.assertEqual({p.name: p.read_bytes() for p in self.data.iterdir()}, before)

    def test_initialize_disabled_conflicts_and_failure_never_retry_or_report_success(self):
        def forbidden(*args): self.fail("Disabled setup called network")
        self.assertIs(OPTIONS["initialize_ui"], False)
        self.options_path.write_text(json.dumps(OPTIONS | {"initialize_ui": True}))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(scheduler.serve(self.data, initialize=forbidden), 0)
        for bad in (OPTIONS | {"initialize_ui": True, "inspect_only": True},
                    OPTIONS | {"initialize_ui": 1}):
            with self.assertRaises(controller.SafetyStop): scheduler.options_validate(bad)
        self.options_path.write_text(json.dumps(OPTIONS | {"enabled": True, "initialize_ui": True}))
        calls = []
        def fail(): calls.append(True); raise controller.SafetyStop("Uncertain")
        with redirect_stdout(io.StringIO()) as output, self.assertRaises(controller.SafetyStop):
            scheduler.serve(self.data, initialize=fail)
        self.assertEqual(calls, [True])
        self.assertEqual(output.getvalue(), "")


if __name__ == "__main__": unittest.main()
