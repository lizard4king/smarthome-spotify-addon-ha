"""Synthetic options/scheduling/packaging tests; never build or access HA."""
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest

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
        for forbidden in ("ports", "map", "host_network", "docker_api", "privileged", "devices"):
            self.assertNotIn(forbidden, MANIFEST)
        dockerfile = (HERE / "Dockerfile").read_text()
        self.assertIn("apk add --no-cache python3 py3-websocket-client", dockerfile)
        self.assertNotIn("pip", dockerfile)
        for image in json.loads((HERE / "build.json").read_text())["build_from"].values():
            self.assertTrue(image.startswith("ghcr.io/home-assistant/"))

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
