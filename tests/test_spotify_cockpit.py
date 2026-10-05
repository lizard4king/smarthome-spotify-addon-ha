"""Offline playback isolation tests. All Spotify and device I/O is synthetic."""
import io
import json
import sys
import threading
import unittest
from datetime import datetime, UTC
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

BRIDGE = Path(__file__).resolve().parents[1] / "smarthome_spotify_bridge"
sys.path.insert(0, str(BRIDGE))
from smarthome.spotify_cockpit import (SpotifyCockpitService, SpotifyCockpitError, SpotifyPlayerStateClient,
                                      SpotifyControlRejected, SpotifyControlUnknown)
from smarthome.spotify_connect import SpotifyConnectDevice, SpotifyDeviceCatalog
from smarthome.spotify_routing import SpotifyProfile, SpotifyProfileRegistry, SpotifyRoutingStatus
from smarthome.spotify_targets import SpotifyPlaybackTarget, SpotifyTargetRegistry
from smarthome.spotify_search import SpotifyMediaSearchClient
from smarthome.spotify_playback import SpotifyPlaybackError
from smarthome.spotify_bridge import SpotifyBridge, SpotifyBridgeError, STATUS_PATH, ASSIGNMENTS_PATH, CONTROL_PATH, make_server
from urllib.request import Request, urlopen
import hashlib
import hmac

NOW = datetime(2026, 10, 5, tzinfo=UTC)


class Response(io.BytesIO):
    def __init__(self, data, status=200):
        super().__init__(json.dumps(data).encode())
        self.status = status

    def getcode(self):
        return self.status


class FakePlayer:
    def __init__(self):
        self.accounts = {"a": "private-account-a", "b": "private-account-b"}
        self.values = {key: {"status": "idle", "is_playing": False, "active_device_name": None,
                             "track": None, "device_id": None} for key in ("a", "b")}

    def state(self, token):
        return self.values[token]

    def account_id(self, token):
        return self.accounts[token]


class FakeDevices:
    def __init__(self):
        self.ids = {"living": "device-living", "office": "device-office"}

    def devices(self, token):
        return SpotifyDeviceCatalog(tuple(SpotifyConnectDevice(name, "Speaker", False, False, 20, self.ids[key])
            for key, name in (("living", "Echo Living"), ("office", "Echo Office"))))


class FakeCommands:
    def __init__(self):
        self.calls = []
        self.barrier = None
        self.fail_profile = None

    def play(self, command, **kwargs):
        self.calls.append((command.profile_alias, command.target_alias, command.media_query))
        if self.barrier:
            self.barrier.wait(timeout=2)
        if command.profile_alias == self.fail_profile:
            raise RuntimeError("secret should not escape")
        return (SimpleNamespace(status=SpotifyRoutingStatus.ROUTED), None)


class PlaybackIsolation(unittest.TestCase):
    def setUp(self):
        registry = SpotifyProfileRegistry([
            SpotifyProfile("a", "Alice", "conn_a", allowed_targets=("living", "office")),
            SpotifyProfile("b", "Bob", "conn_b", allowed_targets=("living", "office"))])
        targets = SpotifyTargetRegistry([
            SpotifyPlaybackTarget("living", "Echo Living", ("Living Room",)),
            SpotifyPlaybackTarget("office", "Echo Office", ("Office",))])
        self.player, self.devices, self.commands = FakePlayer(), FakeDevices(), FakeCommands()
        tokens = SimpleNamespace(access_token=lambda connection_id, **kw: {"conn_a": "a", "conn_b": "b"}[connection_id])
        self.service = SpotifyCockpitService(registry, targets, tokens, self.commands, player=self.player, devices=self.devices)
        self.requests = [{"profile": "Alice", "target": "Living Room", "track": "spotify:track:AAAA"},
                         {"profile": "Bob", "target": "Echo Office", "track": "spotify:track:BBBB"}]

    def reject(self, requests):
        with self.assertRaises(SpotifyCockpitError):
            self.service.play(requests, now=NOW)
        self.assertEqual(self.commands.calls, [])

    def test_independent_profiles_start_independently_and_remain_unverified(self):
        self.commands.barrier = threading.Barrier(2)
        result = self.service.play(self.requests, now=NOW)
        self.assertEqual(result["status"], "accepted")
        self.assertCountEqual(self.commands.calls, [("a", "living", "spotify:track:AAAA"),
                                                    ("b", "office", "spotify:track:BBBB")])
        self.assertTrue(all(not item["playback_verified"] for item in result["assignments"]))
        self.assertEqual(set(self.service.sessions), {"a", "b"})

    def test_same_profile_alias_different_targets_is_not_fake_parallel(self):
        self.requests[1]["profile"] = "a"
        self.reject(self.requests)

    def test_different_profiles_same_logical_target_alias(self):
        self.requests[1]["target"] = "living"
        self.reject(self.requests)

    def test_different_labels_same_spotify_account(self):
        self.player.accounts["b"] = self.player.accounts["a"]
        self.reject(self.requests)

    def test_different_logical_targets_same_physical_device(self):
        self.devices.ids["office"] = self.devices.ids["living"]
        self.reject(self.requests)

    def test_existing_other_profile_stream_is_not_taken_over(self):
        self.player.values["b"].update(status="playing", is_playing=True, active_device_name="Echo Living",
                                       device_id="device-living")
        self.reject(self.requests[:1])

    def test_unknown_other_profile_is_not_assumed_idle(self):
        def broken(token):
            if token == "b":
                raise RuntimeError("private failure")
            return self.player.values[token]
        self.player.state = broken
        self.reject(self.requests[:1])

    def test_existing_other_stream_on_different_target_is_preserved(self):
        self.player.values["b"].update(status="playing", is_playing=True, active_device_name="Echo Office",
                                       device_id="device-office")
        self.assertEqual(self.service.play(self.requests[:1], now=NOW)["status"], "accepted")
        self.assertEqual(self.commands.calls, [("a", "living", "spotify:track:AAAA")])

    def test_unknown_profile_or_target_never_falls_back(self):
        for field in ("profile", "target"):
            item = dict(self.requests[0], **{field: "missing"})
            self.reject([item])

    def test_pending_accepted_start_protects_target_during_status_delay(self):
        self.service.play(self.requests[:1], now=NOW)
        before = list(self.commands.calls)
        with self.assertRaises(SpotifyCockpitError):
            self.service.play([{**self.requests[1], "target": "living"}], now=NOW)
        self.assertEqual(self.commands.calls, before)

    def test_pending_start_protects_same_account_across_separate_batches(self):
        self.player.accounts["b"] = self.player.accounts["a"]
        self.service.play(self.requests[:1], now=NOW)
        before = list(self.commands.calls)
        with self.assertRaises(SpotifyCockpitError):
            self.service.play(self.requests[1:], now=NOW)
        self.assertEqual(self.commands.calls, before)

    def test_unknown_start_cannot_be_erased_by_changing_the_same_profile_target(self):
        self.commands.fail_profile = "a"
        self.assertEqual(self.service.play(self.requests[:1], now=NOW)["status"], "failed")
        before = list(self.commands.calls)
        for request in ({**self.requests[0], "target": "office"},
                        {**self.requests[1], "target": "living"}):
            with self.assertRaises(SpotifyCockpitError):
                self.service.play([request], now=NOW)
        self.assertEqual(self.commands.calls, before)
        self.assertEqual({pending["target_id"] for pending in self.service.pending.values()}, {"living"})

    def test_expired_unknown_reservation_allows_an_explicit_new_target(self):
        self.commands.fail_profile = "a"
        self.service.play(self.requests[:1], now=NOW)
        for pending in self.service.pending.values():
            pending["expires"] = 0
        self.commands.fail_profile = None
        result = self.service.play([{**self.requests[0], "target": "office"}], now=NOW)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual({pending["target_id"] for pending in self.service.pending.values()}, {"office"})

    def test_explicit_same_device_new_title_keeps_its_target_reserved(self):
        self.service.play(self.requests[:1], now=NOW)
        result = self.service.play([{**self.requests[0], "track": "spotify:track:CCCC"}], now=NOW)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(len(self.service.pending), 1)
        before = list(self.commands.calls)
        with self.assertRaises(SpotifyCockpitError):
            self.service.play([{**self.requests[1], "target": "living"}], now=NOW)
        self.assertEqual(self.commands.calls, before)

    def test_cache_invalidation_during_age_check_keeps_a_safe_local_snapshot(self):
        cached = {"available": True, "profiles": [], "targets": [], "sessions": []}
        self.service.status_cache = (100, cached)
        def invalidate():
            self.service.status_cache = None
            return 101
        with patch("smarthome.spotify_cockpit.time.monotonic", side_effect=invalidate):
            self.assertIs(self.service.status(now=NOW), cached)
        self.assertIsNone(self.service.status_cache)

    def test_command_history_is_not_public_observed_state(self):
        self.service.play(self.requests[:1], now=NOW)
        self.assertEqual(self.service.status(now=NOW)["sessions"], [])

    def test_status_targets_are_actually_visible_not_just_configured(self):
        self.devices.devices = lambda token: SpotifyDeviceCatalog((SpotifyConnectDevice(
            "Echo Office", "Speaker", False, False, 20, "private-device-office"),))
        self.player.account_id = lambda token: self.fail("Read-only status must not read account identity")
        result = self.service.status(now=NOW)
        self.assertEqual(result["profiles"][0]["targets"], ["living", "office"])
        self.assertEqual(result["profiles"][0]["available_targets"], ["office"])
        self.assertNotIn("private-device-office", json.dumps(result))

    def test_partial_failure_is_not_returned_as_success_and_is_redacted(self):
        self.commands.fail_profile = "b"
        result = self.service.play(self.requests, now=NOW)
        self.assertEqual(result["status"], "partial")
        self.assertEqual([r["status"] for r in result["assignments"]], ["accepted", "failed"])
        self.assertNotIn("secret", json.dumps(result))
        self.assertEqual(set(self.service.sessions), {"a"})

    def test_status_is_observed_and_never_exposes_account_tokens_or_ids(self):
        self.service.play(self.requests, now=NOW)
        self.player.values["b"].update(status="playing", is_playing=True, active_device_name="Echo Office",
                                       device_id="device-office", track={"title": "Observed Other Song"})
        result = self.service.status(now=NOW)
        self.assertEqual(result["profiles"][0]["status"], "idle")
        self.assertEqual(result["profiles"][1]["track"]["title"], "Observed Other Song")
        self.assertEqual(result["profiles"][1]["active_target_id"], "office")
        raw = json.dumps(result)
        self.assertNotIn("private-account", raw)
        self.assertNotIn("device-office", raw)
        self.assertNotIn("conn_", raw)

    def test_shape_and_length_rejections(self):
        self.reject([])
        self.reject(self.requests * 5)
        self.reject([{**self.requests[0], "access_token": "forbidden"}])
        self.reject([{**self.requests[0], "track": "x" * 513}])

    def legacy_targets(self, a_targets=("missing", "living", "office"), b_targets=("living", "office")):
        registry = SpotifyProfileRegistry([
            SpotifyProfile("a", "Alice", "conn_a", allowed_targets=a_targets),
            SpotifyProfile("b", "Bob", "conn_b", allowed_targets=b_targets)])
        self.service = SpotifyCockpitService(registry, self.service.targets, self.service.tokens,
            self.commands, player=self.player, devices=self.devices)
        return registry

    def test_missing_configured_allowed_target_does_not_abort_startup_or_call_network(self):
        registry = SpotifyProfileRegistry([SpotifyProfile("a", "Alice", "conn", allowed_targets=("unknown",))])
        service = SpotifyCockpitService(registry, self.service.targets, None, None)
        self.assertIs(service.registry, registry)
        self.assertEqual(registry.profiles["a"].allowed_targets, ("unknown",))

    def test_legacy_missing_target_is_diagnostic_not_an_available_target(self):
        registry = self.legacy_targets()
        self.player.values["a"].update(status="playing", is_playing=True, active_device_name="Echo Office",
                                       device_id="device-office", track={"title": "Observed Song"})
        status = self.service.status(now=NOW)
        profile = status["profiles"][0]
        self.assertTrue(status["available"])
        self.assertEqual(profile["status"], "playing")
        self.assertEqual(profile["active_target_id"], "office")
        self.assertEqual(profile["track"]["title"], "Observed Song")
        self.assertEqual(profile["targets"], ["living", "office"])
        self.assertEqual(profile["available_targets"], ["living", "office"])
        self.assertEqual(profile["unavailable_targets"], ["missing"])
        self.assertEqual(profile["configuration_error"],
                         "Ein freigegebenes Spotify-Ziel fehlt in der Zielkonfiguration.")
        self.assertEqual(registry.profiles["a"].allowed_targets, ("missing", "living", "office"))

    def test_missing_target_play_is_rejected_before_token_or_player_network(self):
        self.legacy_targets()
        self.service.tokens = SimpleNamespace(access_token=lambda *a, **kw: self.fail("Token call is forbidden"))
        self.player.state = lambda *a: self.fail("Player call is forbidden")
        self.devices.devices = lambda *a: self.fail("Device call is forbidden")
        self.reject([{**self.requests[0], "target": "missing"}])

    def test_valid_target_remains_usable_with_missing_legacy_target(self):
        self.legacy_targets(a_targets=("missing", "office"))
        result = self.service.play([{**self.requests[0], "target": "office"}], now=NOW)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(self.commands.calls, [("a", "office", "spotify:track:AAAA")])

    def test_profile_with_only_missing_target_still_protects_active_physical_device(self):
        self.legacy_targets(b_targets=("missing",))
        self.player.values["b"].update(status="playing", is_playing=True, active_device_name="Unmapped Echo",
                                       device_id="device-living")
        self.reject(self.requests[:1])

    def test_profile_with_only_missing_target_still_protects_active_device_name(self):
        self.legacy_targets(b_targets=("missing",))
        self.player.values["b"].update(status="playing", is_playing=True, active_device_name="Echo Living",
                                       device_id=None)
        self.reject(self.requests[:1])

    def test_profile_with_only_missing_target_still_protects_active_account(self):
        self.legacy_targets(b_targets=("missing",))
        self.player.accounts["b"] = self.player.accounts["a"]
        self.player.values["b"].update(status="playing", is_playing=True, active_device_name="Unmapped Echo",
                                       device_id="unmapped-device")
        self.reject(self.requests[:1])

    def test_unrelated_active_target_of_legacy_profile_is_not_assumed_a_collision(self):
        self.legacy_targets(b_targets=("missing",))
        self.player.values["b"].update(status="playing", is_playing=True, active_device_name="Unmapped Echo",
                                       device_id="unmapped-device")
        result = self.service.play(self.requests[:1], now=NOW)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(self.commands.calls, [("a", "living", "spotify:track:AAAA")])

    def test_expired_preflight_does_not_start_a_late_command(self):
        ticks = iter([0])
        with patch("smarthome.spotify_cockpit.time.monotonic", side_effect=lambda: next(ticks, 41)):
            self.reject(self.requests)

    def test_delayed_stale_preflight_cannot_take_over_after_pending_consumed(self):
        captured, release = threading.Event(), threading.Event()
        original_snapshots = self.service._snapshots
        stale_outcomes = []
        def delayed_snapshots(*args, **kwargs):
            states = original_snapshots(*args, **kwargs)
            if threading.current_thread().name == "stale-preflight":
                captured.set()
                if not release.wait(3):
                    raise RuntimeError("synthetic preflight delay")
            return states
        self.service._snapshots = delayed_snapshots
        def stale_start():
            try:
                stale_outcomes.append(self.service.play(
                    [{**self.requests[1], "target": "living"}], now=NOW))
            except Exception as exc:
                stale_outcomes.append(exc)
        worker = threading.Thread(target=stale_start, name="stale-preflight")
        worker.start()
        self.assertTrue(captured.wait(2))
        try:
            self.service.play(self.requests[:1], now=NOW)
            self.player.values["a"].update(status="playing", is_playing=True,
                active_device_name="Echo Living", device_id="device-living",
                track={"uri": self.requests[0]["track"], "duration_ms": 180000})
            self.player.control = lambda *args, **kwargs: None
            self.service.control({"profile": "a", "target": "living", "action": "pause"}, now=NOW)
            self.assertEqual(self.service.pending, {})
            self.assertEqual(self.service.inflight, {})
        finally:
            release.set()
            worker.join(3)
        self.assertIsInstance(stale_outcomes[0], SpotifyCockpitError)
        self.assertIn("Vorprüfung geändert", str(stale_outcomes[0]))
        self.assertEqual(self.commands.calls, [("a", "living", "spotify:track:AAAA")])


class TransportControls(unittest.TestCase):
    def setUp(self):
        PlaybackIsolation.setUp(self)
        self.controls = []
        self.player.control = lambda token, device, action, **kw: self.controls.append((token, device, action, kw))
        for profile, target in (("a", "living"), ("b", "office")):
            self.player.values[profile].update(status="playing", is_playing=True,
                active_device_name="Echo Living" if target == "living" else "Echo Office",
                device_id="device-" + target, progress_ms=5000,
                track={"uri": "spotify:track:AAAA" if profile == "a" else "spotify:track:BBBB", "duration_ms": 180000})
        self.control = {"profile": "a", "target": "living", "action": "pause"}

    def test_observed_accepted_start_allows_immediate_pause_but_unknown_start_does_not(self):
        for failed in (False, True):
            self.commands.fail_profile = "a" if failed else None
            self.service.play(self.requests[:1], now=NOW)
            self.player.values["a"]["track"]["uri"] = self.requests[0]["track"]
            if failed:
                with self.assertRaises(SpotifyCockpitError):
                    self.service.control(self.control, now=NOW)
            else:
                self.assertEqual(self.service.control(self.control, now=NOW)["status"], "accepted")
                self.assertEqual(self.service.pending, {})
        self.assertEqual(len(self.controls), 1)

    def test_context_and_text_starts_allow_immediate_controls_only_after_acceptance(self):
        for media in ("spotify:album:ABCD", "spotify:playlist:ABCD", "spotify:artist:ABCD", "a requested song"):
            for accepted in (True, False):
                self.setUp()
                self.commands.fail_profile = None if accepted else "a"
                self.service.play([{**self.requests[0], "track": media}], now=NOW)
                self.player.values["a"]["context_uri"] = media if media.startswith("spotify:") else None
                if accepted:
                    self.assertTrue(self.service.status(now=NOW)["profiles"][0]["controls_available"])
                    self.assertEqual(self.service.control(self.control, now=NOW)["status"], "accepted")
                else:
                    self.assertFalse(self.service.status(now=NOW)["profiles"][0]["controls_available"])
                    with self.assertRaises(SpotifyCockpitError):
                        self.service.control(self.control, now=NOW)
                    self.assertEqual(self.controls, [])

    def test_context_start_must_match_observed_context(self):
        self.service.play([{**self.requests[0], "track": "spotify:album:ABCD"}], now=NOW)
        self.player.values["a"]["context_uri"] = "spotify:album:OTHER"
        with self.assertRaises(SpotifyCockpitError):
            self.service.control(self.control, now=NOW)
        self.assertEqual(self.controls, [])

    def test_relinked_account_cannot_clear_old_pending_start_on_same_device(self):
        self.service.play([{**self.requests[0], "track": "a requested song"}], now=NOW)
        self.player.accounts["a"] = "new-account"
        with self.assertRaises(SpotifyCockpitError):
            self.service.control(self.control, now=NOW)
        self.assertEqual(self.controls, [])
        self.assertEqual(len(self.service.pending), 1)

    def test_recent_same_account_identity_still_blocks_unavailable_profile(self):
        self.player.accounts["b"] = self.player.accounts["a"]
        with self.assertRaises(SpotifyCockpitError):
            self.service.control(self.control, now=NOW)
        original = self.player.state
        self.player.state = lambda token: original(token) if token == "a" else (_ for _ in ()).throw(RuntimeError())
        with self.assertRaises(SpotifyCockpitError):
            self.service.control(self.control, now=NOW)
        self.assertEqual(self.controls, [])

    def test_account_identity_cache_expires(self):
        self.service.account_cache["b"] = (100, "old-account")
        with patch("smarthome.spotify_cockpit.time.monotonic", return_value=106):
            self.assertIsNone(self.service._known_account("b"))

    def test_unknown_transport_outcome_reserves_resource_but_4xx_does_not(self):
        for error_type in (SpotifyControlRejected, SpotifyControlUnknown):
            self.setUp()
            calls = []
            def failed(*args, **kwargs):
                calls.append(args)
                raise error_type("synthetic controlled error")
            self.player.control = failed
            with self.assertRaises(error_type) as failure:
                self.service.control(self.control, now=NOW)
            self.assertEqual(failure.exception.outcome, "unknown" if error_type is SpotifyControlUnknown else "not_sent")
            self.assertEqual(len(calls), 1)
            self.assertEqual(bool(self.service.pending), error_type is SpotifyControlUnknown)
            self.assertEqual(self.service.inflight, {})
            if error_type is SpotifyControlUnknown:
                with self.assertRaises(SpotifyCockpitError):
                    self.service.control(self.control, now=NOW)
                self.assertEqual(len(calls), 1)

    def test_exception_after_provider_write_cannot_claim_definite_rejection(self):
        from contextlib import contextmanager
        original_operation = self.service._operation
        @contextmanager
        def failed_cleanup(*args, **kwargs):
            with original_operation(*args, **kwargs):
                yield
            raise SpotifyCockpitError("synthetic local failure after write")
        self.service._operation = failed_cleanup
        with self.assertRaises(SpotifyControlUnknown) as failure:
            self.service.control(self.control, now=NOW)
        self.assertEqual(failure.exception.outcome, "unknown")
        self.assertEqual(len(self.controls), 1)
        self.assertEqual(len(self.service.pending), 1)

    def test_seek_rejects_stale_title_on_latest_recheck(self):
        original = self.player.state
        calls = []
        def changed(token):
            value = original(token)
            if token == "a":
                calls.append(token)
                if len(calls) > 1:
                    return {**value, "track": {"uri": "spotify:track:NEW", "duration_ms": 180000}}
            return value
        self.player.state = changed
        with self.assertRaisesRegex(SpotifyCockpitError, "Titel hat sich") as failure:
            self.service.control({**self.control, "action": "seek", "position_ms": 1000,
                                  "track_uri": "spotify:track:AAAA"}, now=NOW)
        self.assertEqual(failure.exception.outcome, "not_sent")
        self.assertEqual(self.controls, [])

    def test_seek_requires_observed_track_uri(self):
        for uri in (None, True, "spotify:album:AAAA", "", "x" * 513):
            with self.assertRaises(SpotifyCockpitError):
                self.service.control({**self.control, "action": "seek", "position_ms": 1000,
                                      "track_uri": uri}, now=NOW)
        self.assertEqual(self.controls, [])

    def test_controls_write_only_selected_profile_and_observed_device(self):
        for action in ("pause", "resume", "next", "previous", "seek"):
            payload = {**self.control, "action": action}
            if action == "seek":
                payload["position_ms"] = 25000
                payload["track_uri"] = "spotify:track:AAAA"
            result = self.service.control(payload, now=NOW)
            self.assertEqual(result["status"], "accepted")
            self.assertFalse(result["playback_verified"])
        self.assertTrue(all(call[0:2] == ("a", "device-living") for call in self.controls))
        self.assertEqual(self.player.values["b"]["progress_ms"], 5000)
        self.assertEqual(self.commands.calls, [])

    def test_controls_reject_malformed_shapes_and_seek_bounds_before_write(self):
        payloads = [[], {**self.control, "action": True}, {**self.control, "action": "stop"},
                    {**self.control, "token": "forbidden"}, {**self.control, "profile": False},
                    {**self.control, "action": "seek"}]
        payloads += [{**self.control, "action": "seek", "position_ms": p, "track_uri": "spotify:track:AAAA"}
                     for p in (True, -1, 1.5, "1", 180000, 2_147_483_648)]
        for payload in payloads:
            with self.subTest(payload=payload), self.assertRaises(SpotifyCockpitError):
                self.service.control(payload, now=NOW)
        self.assertEqual(self.controls, [])

    def test_controls_reject_active_mismatch_and_missing_id(self):
        for field, value in (("device_id", "stale-device"), ("device_id", None),
                             ("active_device_name", "Echo Office")):
            original = dict(self.player.values["a"])
            self.player.values["a"][field] = value
            with self.assertRaises(SpotifyCockpitError):
                self.service.control(self.control, now=NOW)
            self.player.values["a"] = original
        self.assertEqual(self.controls, [])

    def test_controls_reject_unknown_profile_target_or_disallowed_target(self):
        for field, value in (("profile", "missing"), ("target", "missing"), ("target", "office")):
            with self.assertRaises(SpotifyCockpitError):
                self.service.control({**self.control, field: value}, now=NOW)
        self.assertEqual(self.controls, [])

    def test_controls_reject_ambiguous_restricted_missing_catalog(self):
        for devices in ((), (SpotifyConnectDevice("Echo Living", "Speaker", True, True, 20, "device-living"),),
                        (SpotifyConnectDevice("Echo Living", "Speaker", True, False, 20, "device-living"),) * 2):
            self.devices.devices = lambda token: SpotifyDeviceCatalog(devices)
            with self.assertRaises(SpotifyCockpitError):
                self.service.control(self.control, now=NOW)
        self.assertEqual(self.controls, [])

    def test_controls_reject_same_account_or_other_profile_on_target(self):
        self.player.accounts["b"] = self.player.accounts["a"]
        with self.assertRaises(SpotifyCockpitError):
            self.service.control(self.control, now=NOW)
        self.player.accounts["b"] = "other-account"
        self.player.values["b"]["device_id"] = "device-living"
        with self.assertRaises(SpotifyCockpitError):
            self.service.control(self.control, now=NOW)
        self.assertEqual(self.controls, [])

    def test_paused_other_profile_does_not_block_pause_after_target_takeover(self):
        self.player.values["b"].update(status="paused", is_playing=False,
            active_device_name="Echo Living", device_id="device-living")
        status = self.service.status(now=NOW)
        self.assertTrue(status["profiles"][0]["controls_available"])
        self.assertTrue(status["profiles"][0]["seek_available"])
        self.assertFalse(status["profiles"][1]["controls_available"])
        self.assertEqual(self.service.control(self.control, now=NOW)["status"], "accepted")
        self.assertEqual(self.controls[0][0:3], ("a", "device-living", "pause"))

    def test_paused_profile_cannot_resume_over_other_playing_profile(self):
        self.player.values["a"].update(status="paused", is_playing=False)
        self.player.values["b"].update(active_device_name="Echo Living", device_id="device-living")
        with self.assertRaises(SpotifyCockpitError):
            self.service.control({**self.control, "action": "resume"}, now=NOW)
        status = self.service.status(now=NOW)
        self.assertFalse(status["profiles"][0]["controls_available"])
        self.assertFalse(status["profiles"][0]["seek_available"])
        self.assertEqual(self.controls, [])

    def test_same_account_stays_blocked_even_when_other_profile_paused(self):
        self.player.accounts["b"] = self.player.accounts["a"]
        self.player.values["b"].update(status="paused", is_playing=False)
        with self.assertRaises(SpotifyCockpitError):
            self.service.control(self.control, now=NOW)
        self.assertEqual(self.controls, [])

    def test_unavailable_other_profile_allows_verified_pause_seek_only(self):
        original = self.player.state
        def unavailable(token):
            if token == "b":
                raise RuntimeError("synthetic unavailable")
            return original(token)
        self.player.state = unavailable
        status = self.service.status(now=NOW)
        self.assertTrue(status["profiles"][0]["controls_available"])
        self.assertTrue(status["profiles"][0]["seek_available"])
        self.assertEqual(status["profiles"][0]["disallowed_actions"], ["next", "previous", "resume"])
        self.assertIn("degradation_note", status["profiles"][0])
        self.assertEqual(self.service.control(self.control, now=NOW)["status"], "accepted")
        self.assertEqual(self.service.control({**self.control, "action": "seek", "position_ms": 1000,
            "track_uri": "spotify:track:AAAA"}, now=NOW)["status"], "accepted")
        for action in ("resume", "next", "previous"):
            with self.assertRaises(SpotifyCockpitError):
                self.service.control({**self.control, "action": action}, now=NOW)
        self.assertEqual(len(self.controls), 2)

    def test_playing_target_conflict_disables_status_without_account_reads(self):
        self.player.values["b"].update(active_device_name="Echo Living", device_id="device-living")
        original_account_id = self.player.account_id
        self.player.account_id = lambda token: self.fail("Status must not read account identities")
        status = self.service.status(now=NOW)
        self.assertTrue(all(not profile["controls_available"] and not profile["seek_available"]
                            for profile in status["profiles"]))
        self.player.account_id = original_account_id
        with self.assertRaises(SpotifyCockpitError):
            self.service.control(self.control, now=NOW)
        self.assertEqual(self.controls, [])

    def test_controls_reject_target_change_during_preflight(self):
        calls = []
        def changed(token):
            if token == "a":
                calls.append(token)
                if len(calls) > 1:
                    return {**self.player.values[token], "device_id": "device-office"}
            return self.player.values[token]
        self.player.state = changed
        with self.assertRaises(SpotifyCockpitError):
            self.service.control(self.control, now=NOW)
        self.assertEqual(self.controls, [])

    def network_player(self):
        entered, release = threading.Event(), threading.Event()
        requests = []
        def requester(request, **kwargs):
            token = request.get_header("Authorization").split()[1]
            requests.append((token, request.get_method(), request.full_url))
            if request.get_method() != "GET":
                if token == "a":
                    entered.set()
                    if not release.wait(3):
                        raise TimeoutError("synthetic blocked requester")
                return Response(None, 204)
            if request.full_url.endswith("/me"):
                return Response({"id": "account-" + token})
            target, name = ("living", "Echo Living") if token == "a" else ("office", "Echo Office")
            return Response({"is_playing": True, "device": {"id": "device-" + target, "name": name},
                             "progress_ms": 5000, "item": {"uri": "spotify:track:AAAA", "duration_ms": 180000}})
        self.service.player = SpotifyPlayerStateClient(requester=requester)
        return entered, release, requests

    def start_control_worker(self, payload):
        outcomes, done = [], threading.Event()
        def run():
            try:
                outcomes.append(self.service.control(payload, now=NOW))
            except Exception as exc:
                outcomes.append(exc)
            finally:
                done.set()
        worker = threading.Thread(target=run)
        worker.start()
        return worker, done, outcomes

    def test_blocked_requester_does_not_block_independent_account_device(self):
        entered, release, requests = self.network_player()
        first, first_done, first_outcomes = self.start_control_worker(self.control)
        self.assertTrue(entered.wait(2))
        second, second_done, second_outcomes = self.start_control_worker(
            {"profile": "b", "target": "office", "action": "pause"})
        try:
            self.assertTrue(second_done.wait(1), "Independent write waited behind another account's requester")
            self.assertFalse(first_done.is_set())
            self.assertEqual(second_outcomes[0]["status"], "accepted")
            self.assertTrue(any(token == "b" and method == "PUT" for token, method, url in requests))
        finally:
            release.set()
            first.join(3)
            second.join(3)
        self.assertEqual(first_outcomes[0]["status"], "accepted")
        self.assertEqual(self.service.inflight, {})

    def test_blocked_requester_rejects_same_resources_and_play(self):
        entered, release, requests = self.network_player()
        first, done, outcomes = self.start_control_worker(self.control)
        self.assertTrue(entered.wait(2))
        try:
            with self.assertRaisesRegex(SpotifyCockpitError, "läuft bereits"):
                self.service.control(self.control, now=NOW)
            with self.assertRaisesRegex(SpotifyCockpitError, "läuft bereits"):
                self.service.play(self.requests[:1], now=NOW)
            self.assertFalse(done.is_set())
            self.assertEqual(sum(method != "GET" for token, method, url in requests), 1)
        finally:
            release.set()
            first.join(3)
        self.assertEqual(outcomes[0]["status"], "accepted")
        self.assertEqual(self.commands.calls, [])

    def test_status_returns_during_blocked_control_with_observed_state(self):
        entered, release, requests = self.network_player()
        first, done, outcomes = self.start_control_worker(self.control)
        self.assertTrue(entered.wait(2))
        status_done, statuses = threading.Event(), []
        def read_status():
            try:
                statuses.append(self.service.status(now=NOW))
            finally:
                status_done.set()
        status_worker = threading.Thread(target=read_status)
        status_worker.start()
        try:
            self.assertTrue(status_done.wait(1), "Status waited behind the write")
            self.assertFalse(done.is_set())
            self.assertEqual(statuses[0]["profiles"][0]["status"], "playing")
            self.assertFalse(statuses[0]["profiles"][0]["controls_available"])
            self.assertTrue(statuses[0]["profiles"][1]["controls_available"])
        finally:
            release.set()
            first.join(3)
            status_worker.join(3)
        self.assertEqual(outcomes[0]["status"], "accepted")

    def test_disallowed_action_and_unknown_duration_reject(self):
        self.player.values["a"]["disallowed_actions"] = ["pause"]
        with self.assertRaises(SpotifyCockpitError):
            self.service.control(self.control, now=NOW)
        self.player.values["a"]["track"]["duration_ms"] = None
        with self.assertRaises(SpotifyCockpitError):
            self.service.control({**self.control, "action": "seek", "position_ms": 0,
                                  "track_uri": "spotify:track:AAAA"}, now=NOW)
        self.assertEqual(self.controls, [])


class PlayerStateTests(unittest.TestCase):
    def test_4xx_is_explicit_rejection_5xx_is_unknown_no_automatic_retry(self):
        from urllib.error import HTTPError
        for code, expected in ((400, SpotifyControlRejected), (403, SpotifyControlRejected),
                               (429, SpotifyControlRejected), (500, SpotifyControlUnknown)):
            calls = []
            def requester(request, **kwargs):
                calls.append(request)
                raise HTTPError(request.full_url, code, "private-details", {}, None)
            with self.assertRaises(expected) as failure:
                SpotifyPlayerStateClient(requester=requester).control("private-token", "private-device", "pause")
            self.assertNotIn("private", str(failure.exception))
            self.assertEqual(failure.exception.outcome, "not_sent" if expected is SpotifyControlRejected else "unknown")
            self.assertEqual(len(calls), 1)

    def test_transport_failure_is_redacted_and_never_retried(self):
        calls = []
        def requester(request, **kwargs):
            calls.append(request)
            raise TimeoutError("private-token-details")
        client = SpotifyPlayerStateClient(requester=requester)
        with self.assertRaises(SpotifyCockpitError) as failure:
            client.control("private-token", "private-device", "pause")
        self.assertNotIn("private", str(failure.exception))
        self.assertEqual(len(calls), 1)

    def test_transport_methods_and_explicit_device_id(self):
        from urllib.parse import parse_qs, urlsplit
        requests = []
        def requester(request, **kwargs):
            requests.append(request)
            return Response(None, 204)
        client = SpotifyPlayerStateClient(requester=requester)
        for action, suffix, method in (("pause", "pause", "PUT"), ("resume", "play", "PUT"),
                                      ("next", "next", "POST"), ("previous", "previous", "POST"),
                                      ("seek", "seek", "PUT")):
            client.control("test-token", "private device &", action, position_ms=25000)
            request = requests[-1]
            self.assertEqual(request.get_method(), method)
            self.assertTrue(urlsplit(request.full_url).path.endswith("/" + suffix))
            self.assertEqual(parse_qs(urlsplit(request.full_url).query)["device_id"], ["private device &"])
            self.assertEqual(request.data, b"")
            self.assertEqual(request.get_header("Content-length"), "0")

    def test_progress_duration_and_disallowed_actions_are_observed(self):
        value = {"is_playing": True, "device": {"id": "opaque", "name": "Echo"},
                 "progress_ms": 1500, "item": {"duration_ms": 40000},
                 "actions": {"disallows": {"seeking": True}}}
        state = SpotifyPlayerStateClient(requester=lambda *a, **kw: Response(value)).state("test-token")
        self.assertEqual(state["progress_ms"], 1500)
        self.assertEqual(state["track"]["duration_ms"], 40000)
        self.assertEqual(state["disallowed_actions"], ["seek"])
    def test_selected_spotify_uri_is_played_without_a_second_search(self):
        def forbidden_network(*a, **kw):
            self.fail("Selected URI must not trigger a search")
        client = SpotifyMediaSearchClient(requester=forbidden_network)
        self.assertEqual(client.resolve("spotify:track:AAAA", access_token="test-token").track_uris,
                         ("spotify:track:AAAA",))
        self.assertEqual(client.resolve("spotify:album:BBBB", access_token="test-token").context_uri,
                         "spotify:album:BBBB")
        with self.assertRaises(SpotifyPlaybackError):
            client.resolve("spotify:evil:AAAA", access_token="test-token")

    def test_204_means_idle_and_no_device_action(self):
        client = SpotifyPlayerStateClient(requester=lambda *a, **kw: Response(None, 204))
        self.assertEqual(client.state("test-token")["status"], "idle")

    def test_account_id_is_separate_from_display_data(self):
        client = SpotifyPlayerStateClient(requester=lambda *a, **kw: Response({"id": "private-account"}))
        self.assertEqual(client.account_id("test-token"), "private-account")

    def test_cover_and_artist_metadata_are_bounded_and_https_only(self):
        value = {"is_playing": True, "device": {"id": "opaque", "name": "Echo"},
                 "item": {"uri": "spotify:track:A", "name": "Song", "artists": [{"name": "Artist"}],
                          "album": {"images": [{"url": "javascript:bad"}, {"url": "https://i.scdn.co/cover"}]}}}
        client = SpotifyPlayerStateClient(requester=lambda *a, **kw: Response(value))
        state = client.state("test-token")
        self.assertTrue(state["is_playing"])
        self.assertEqual(state["track"]["image_url"], "https://i.scdn.co/cover")

    def test_invalid_status_is_not_guessed_as_playing(self):
        client = SpotifyPlayerStateClient(requester=lambda *a, **kw: Response({"is_playing": "true"}))
        with self.assertRaises(SpotifyCockpitError):
            client.state("test-token")


class BridgeContractTests(unittest.TestCase):
    def setUp(self):
        self.secret = "synthetic-only-secret-" * 2
        self.dispatcher = SimpleNamespace(status=lambda **kw: {"available": True, "profiles": [], "targets": [], "sessions": []},
            play_assignments=lambda assignments, **kw: {"status": "accepted", "assignments": assignments},
            control=lambda payload, **kw: {"status": "accepted", "playback_verified": False, "action": payload["action"]})
        self.bridge = SpotifyBridge(self.dispatcher, self.secret, clock=lambda: 123)

    def request(self, path, payload):
        body = json.dumps(payload).encode()
        message = b"123." + body
        headers = {"X-SmartHome-Timestamp": "123", "X-SmartHome-Signature":
                   hmac.new(self.secret.encode(), b"123." + body, hashlib.sha256).hexdigest()}
        if path == CONTROL_PATH:
            import secrets
            nonce = secrets.token_hex(16)
            message = f"v2.POST.{path}.123.{nonce}.".encode("ascii") + body
            headers.update({"X-SmartHome-Signature-Version": "2", "X-SmartHome-Nonce": nonce,
                            "X-SmartHome-Signature": hmac.new(self.secret.encode(), message, hashlib.sha256).hexdigest()})
        return self.bridge.handle_cockpit(method="POST", path=path, headers=headers, body=body)

    def test_authenticated_status_route(self):
        self.assertTrue(self.request(STATUS_PATH, {})["available"])

    def test_authenticated_assignments_route(self):
        self.assertEqual(self.request(ASSIGNMENTS_PATH, {"assignments": []})["status"], "accepted")

    def test_authenticated_control_route_and_invalid_signature(self):
        value = self.request(CONTROL_PATH, {"profile": "a", "target": "living", "action": "pause"})
        self.assertEqual(value["status"], "accepted")
        self.assertFalse(value["playback_verified"])
        with self.assertRaises(SpotifyBridgeError):
            self.bridge.handle_cockpit(method="POST", path=CONTROL_PATH, headers={}, body=b"{}")
        with self.assertRaises(SpotifyBridgeError):
            self.bridge.handle_cockpit(method="GET", path=CONTROL_PATH, headers={}, body=b"{}")
        with self.assertRaises(SpotifyCockpitError):
            self.request(CONTROL_PATH, {"profile": "a", "target": "living", "action": "pause", "token": "x"})

    def test_status_does_not_accept_token_or_extra_fields(self):
        with self.assertRaises(SpotifyBridgeError):
            self.request(STATUS_PATH, {"access_token": "forbidden"})

    def test_status_without_signature_is_rejected(self):
        with self.assertRaises(SpotifyBridgeError):
            self.bridge.handle_cockpit(method="POST", path=STATUS_PATH, headers={}, body=b"{}")

    def test_real_http_handler_delivers_collision_error_to_client(self):
        def failed(*args, **kwargs):
            raise SpotifyCockpitError("This Alexa is already occupied")
        self.dispatcher.play_assignments = failed
        server = make_server(self.bridge, port=18877)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            body = json.dumps({"assignments": []}).encode()
            headers = {"X-SmartHome-Timestamp": "123", "X-SmartHome-Signature":
                       hmac.new(self.secret.encode(), b"123." + body, hashlib.sha256).hexdigest()}
            request = Request("http://127.0.0.1:18877" + ASSIGNMENTS_PATH, data=body, headers=headers)
            with urlopen(request, timeout=3) as response:
                result = json.loads(response.read())
                self.assertEqual(response.getcode(), 200)
            self.assertEqual(result, {"status": "failed", "error": "This Alexa is already occupied"})
        finally:
            server.shutdown()
            worker.join(timeout=3)
            server.server_close()

    def test_real_http_control_handler_preserves_outcome_and_redacts_raw_provider_errors(self):
        sys.path.insert(0, str(BRIDGE.parent / "smarthome_cockpit"))
        from dashboard.spotify import SpotifyCockpit as Proxy, SpotifyCockpitError as ProxyError
        server = make_server(self.bridge, port=18878)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            proxy = Proxy()
            proxy.url, proxy.secret = "http://127.0.0.1:18878", self.secret
            # The bridge clock must agree with the proxy's real signing clock.
            self.bridge._clock = __import__("time").time
            for error in (SpotifyCockpitError("Preflight rejected"), SpotifyControlRejected("Spotify rejected"),
                          SpotifyControlUnknown("Uncertain write"), RuntimeError("private-provider-token"),
                          SpotifyBridgeError("Invalid authenticated payload")):
                def failed(*args, **kwargs):
                    raise error
                self.dispatcher.control = failed
                with self.assertRaises(ProxyError) as failure:
                    proxy.control({"profile": "a", "target": "living", "action": "pause"})
                expected = (error.outcome if isinstance(error, SpotifyCockpitError) else
                            "not_sent" if isinstance(error, SpotifyBridgeError) else "unknown")
                self.assertEqual(failure.exception.outcome, expected)
                self.assertNotIn("private-provider-token", str(failure.exception))
        finally:
            server.shutdown()
            worker.join(3)
            server.server_close()


if __name__ == "__main__":
    unittest.main()
