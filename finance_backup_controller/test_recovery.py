"""Synthetic reconciliation only; no sockets, authentication or real archives."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import controller
import recovery


CONFIG = {"addon_slug": "synthetic_finance", "local_agent_id": "synthetic.local",
          "drive_agent_id": "synthetic.drive"}
START = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)
NAME = "finance-only-" + "a" * 32


def backup():
    return {"backup_id": "synthetic-backup", "name": NAME, "date": START.isoformat(),
            "homeassistant_included": False, "database_included": False,
            "homeassistant_version": None, "folders": [],
            "addons": [{"slug": CONFIG["addon_slug"]}],
            "failed_addons": [], "failed_folders": [], "failed_agent_ids": [],
            "agents": {a: {"protected": True, "size": 123}
                       for a in controller.agents(CONFIG)}}


class API:
    def __init__(self):
        self.calls = []
        self.backups = [backup()]
        self.state = "idle"
        self.errors = {}
        self.detail_errors = {}
        self.detail_change = lambda b: None
        self.after_first_inventory = lambda: None
        self.inventories = 0
        self.verify_failure = False

    def verify_addon(self, config):
        self.calls.append({"type": "verify_addon"})
        if self.verify_failure:
            raise ValueError("SYNTHETIC_PRIVATE_DIAGNOSTIC")
        assert config == CONFIG

    def command(self, request):
        self.calls.append(deepcopy(request))
        if request["type"] == "backup/info":
            self.inventories += 1
            if self.inventories == 2:
                self.after_first_inventory()
            return {"state": self.state, "agent_errors": deepcopy(self.errors),
                    "backups": deepcopy(self.backups)}
        if request["type"] == "backup/details":
            item = deepcopy(next(b for b in self.backups if b["backup_id"] == request["backup_id"]))
            self.detail_change(item)
            return {"agent_errors": deepcopy(self.detail_errors), "backup": item}
        raise AssertionError("Remote mutation or non-approved read attempted")


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "synthetic-state.json"
        self.state = controller.State(self.path, CONFIG)
        self.state.data["pending"] = {"name": NAME, "started_utc": START.isoformat(),
                                      "backup_job_id": "synthetic-job-not-backup-id"}
        self.state.save()
        self.api = API()

    def reject(self, now=START + timedelta(hours=1)):
        disk = self.path.read_bytes()
        memory = deepcopy(self.state.data)
        with self.assertRaises(controller.SafetyStop) as result:
            recovery.reconcile(self.api, self.state, now)
        self.assertEqual(str(result.exception), recovery.STOP)
        self.assertEqual(self.path.read_bytes(), disk)
        self.assertEqual(self.state.data, memory)
        self.assertTrue(all(call["type"] in {"verify_addon", "backup/info", "backup/details"}
                            for call in self.api.calls))

    def test_success_two_stable_snapshots_and_one_local_commit(self):
        with patch.object(self.state, "save", wraps=self.state.save) as save:
            self.assertEqual(recovery.reconcile(self.api, self.state, START + timedelta(days=1)), recovery.SUCCESS)
        save.assert_called_once()
        self.assertEqual([c["type"] for c in self.api.calls],
                         ["verify_addon", "backup/info", "backup/details", "backup/info", "backup/details"])
        loaded = controller.State(self.path, CONFIG)
        self.assertIsNone(loaded.data["pending"])
        self.assertIsNone(loaded.data["delete_intent"])
        self.assertEqual(loaded.data["owned"], [{"backup_id": "synthetic-backup", "name": NAME,
                                               "created_utc": START.isoformat()}])

    def test_busy_inventory_or_agent_errors_never_clear_pending(self):
        self.api.state = "create_backup"
        self.reject()
        self.api.state = "idle"
        self.api.errors = {"synthetic.drive": "synthetic unavailable"}
        self.reject()
        self.api.errors = {}
        self.api.detail_errors = {"synthetic.drive": "synthetic unavailable"}
        self.reject()

    def test_missing_multiple_and_already_owned_backup_rejected(self):
        self.api.backups = []
        self.reject()
        self.api.backups = [backup(), backup() | {"backup_id": "synthetic-second"}]
        self.reject()
        self.api.backups = [backup()]
        self.state.data["owned"] = [{"backup_id": "synthetic-backup", "name": NAME,
                                      "created_utc": START.isoformat()}]
        self.state.save()
        self.reject()

    def test_wrong_content_encryption_agents_and_failures_rejected(self):
        invalid = [{"homeassistant_included": True}, {"database_included": True},
                   {"homeassistant_version": "2026.10.2"}, {"folders": ["share"]},
                   {"addons": [{"slug": "synthetic_other"}]}, {"failed_addons": ["x"]},
                   {"failed_folders": ["share"]}, {"failed_agent_ids": ["synthetic.drive"]},
                   {"agents": {"synthetic.local": {"protected": True, "size": 123}}},
                   {"agents": {a: {"protected": False, "size": 123} for a in controller.agents(CONFIG)}},
                   {"agents": {a: {"protected": True, "size": 0} for a in controller.agents(CONFIG)}}]
        for change in invalid:
            with self.subTest(fields=sorted(change)):
                self.api = API()
                self.api.backups = [backup() | change]
                self.reject()

    def test_delete_intent_and_invalid_pending_never_call_api(self):
        good = deepcopy(self.state.data["pending"])
        bad = [None, {}, False, 0, "", good | {"extra": 1}, good | {"name": "finance-only-not-random"},
               good | {"started_utc": "2026-10-19T12:00:00"}, good | {"started_utc": "not-date"},
               good | {"backup_job_id": ""}, good | {"backup_job_id": " "},
               {"name": NAME, "started_utc": START.isoformat()}]
        for pending in bad:
            with self.subTest(pending_type=type(pending).__name__):
                self.state.data["pending"] = pending
                self.state.save()
                self.api = API()
                self.reject()
                self.assertEqual(self.api.calls, [])
        self.state.data["pending"] = good
        for intent in ({}, False, 0, "", {"backup_id": "synthetic-deletion"}):
            self.state.data["delete_intent"] = intent
            self.state.save()
            self.api = API()
            self.reject()
            self.assertEqual(self.api.calls, [])

    def test_dates_deadline_and_naive_or_backward_clock_rejected(self):
        for delta in (timedelta(minutes=-5, microseconds=-1), timedelta(minutes=35, microseconds=1),
                      timedelta(hours=2)):
            with self.subTest(delta=delta):
                self.api = API()
                self.api.backups[0]["date"] = (START + delta).isoformat()
                self.reject()
        self.api = API()
        self.api.backups[0]["date"] = (START + timedelta(minutes=30)).isoformat()
        self.reject(START + timedelta(minutes=29))
        self.api = API()
        self.reject(START.replace(tzinfo=None))
        self.reject(START - timedelta(seconds=1))

    def test_boundary_archive_dates_are_accepted(self):
        for minutes in (-5, 35):
            with self.subTest(minutes=minutes):
                self.setUp()
                self.api.backups[0]["date"] = (START + timedelta(minutes=minutes)).isoformat()
                self.assertEqual(recovery.reconcile(self.api, self.state, START + timedelta(hours=1)), recovery.SUCCESS)

    def test_second_inventory_or_details_change_stops(self):
        self.api.after_first_inventory = lambda: self.api.backups[0]["agents"]["synthetic.local"].update(size=456)
        self.reject()
        self.api = API()
        self.api.detail_change = lambda b: b["agents"]["synthetic.local"].update(size=456)
        self.reject()
        self.api = API()
        self.api.after_first_inventory = lambda: setattr(self.api, "state", "create_backup")
        self.reject()

    def test_fresh_guard_and_transport_errors_are_redacted(self):
        self.api.verify_failure = True
        self.reject()
        self.api = API()
        with patch.object(self.api, "command", side_effect=ConnectionError("SYNTHETIC_PRIVATE_DIAGNOSTIC")):
            self.reject()

    def test_save_failure_before_replace_preserves_disk_pending_and_memory(self):
        with patch.object(controller.os, "replace", side_effect=OSError("SYNTHETIC_PRIVATE_DIAGNOSTIC")):
            self.reject()
        self.assertEqual(len(list(self.path.parent.iterdir())), 1)
        self.assertIsNotNone(json.loads(self.path.read_text())["pending"])


if __name__ == "__main__":
    unittest.main()
