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
from smarthome.spotify_cockpit import SpotifyCockpitService, SpotifyCockpitError, SpotifyPlayerStateClient
from smarthome.spotify_connect import SpotifyConnectDevice, SpotifyDeviceCatalog
from smarthome.spotify_routing import SpotifyProfile, SpotifyProfileRegistry, SpotifyRoutingStatus
from smarthome.spotify_targets import SpotifyPlaybackTarget, SpotifyTargetRegistry
from smarthome.spotify_search import SpotifyMediaSearchClient
from smarthome.spotify_playback import SpotifyPlaybackError
from smarthome.spotify_bridge import SpotifyBridge, SpotifyBridgeError, STATUS_PATH, ASSIGNMENTS_PATH, make_server
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

    def test_missing_configured_allowed_target_rejected_before_network(self):
        registry = SpotifyProfileRegistry([SpotifyProfile("a", "Alice", "conn", allowed_targets=("unknown",))])
        with self.assertRaises(SpotifyCockpitError):
            SpotifyCockpitService(registry, self.service.targets, None, None)

    def test_expired_preflight_does_not_start_a_late_command(self):
        with patch("smarthome.spotify_cockpit.time.monotonic", side_effect=[0, 41, 41]):
            self.reject(self.requests)


class PlayerStateTests(unittest.TestCase):
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
            play_assignments=lambda assignments, **kw: {"status": "accepted", "assignments": assignments})
        self.bridge = SpotifyBridge(self.dispatcher, self.secret, clock=lambda: 123)

    def request(self, path, payload):
        body = json.dumps(payload).encode()
        headers = {"X-SmartHome-Timestamp": "123", "X-SmartHome-Signature":
                   hmac.new(self.secret.encode(), b"123." + body, hashlib.sha256).hexdigest()}
        return self.bridge.handle_cockpit(method="POST", path=path, headers=headers, body=body)

    def test_authenticated_status_route(self):
        self.assertTrue(self.request(STATUS_PATH, {})["available"])

    def test_authenticated_assignments_route(self):
        self.assertEqual(self.request(ASSIGNMENTS_PATH, {"assignments": []})["status"], "accepted")

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


if __name__ == "__main__":
    unittest.main()
