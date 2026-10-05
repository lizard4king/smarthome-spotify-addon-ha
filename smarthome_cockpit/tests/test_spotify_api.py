"""Offline signed Spotify cockpit API contract checks."""
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dashboard.spotify import SpotifyCockpit, SpotifyCockpitError, NoRedirect
from dashboard.server import CockpitHandler
from types import SimpleNamespace


class Network:
    def __init__(self, value):
        self.value, self.requests = value, []

    def open(self, request, timeout):
        self.requests.append(request)
        return io.BytesIO(json.dumps(self.value).encode())


class Tests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict("os.environ", {"SPOTIFY_BRIDGE_URL": "http://bridge.test:8766",
                                                 "SPOTIFY_BRIDGE_SECRET": "test-secret-" * 4}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def test_legacy_play_becomes_single_explicit_assignment(self):
        client = SpotifyCockpit()
        client.opener = Network({"status": "accepted", "assignments": []})
        client.play({"profile": "Alice", "target": "Office", "track": "Song"})
        request = client.opener.requests[0]
        self.assertTrue(request.full_url.endswith("/api/spotify/assignments"))
        self.assertEqual(json.loads(request.data)["assignments"][0]["profile"], "Alice")
        self.assertNotIn("test-secret", request.data.decode())
        self.assertIsNotNone(request.get_header("X-smarthome-signature"))

    def test_bridge_failure_never_becomes_http_success_payload(self):
        client = SpotifyCockpit()
        client.opener = Network({"status": "failed", "error": "Target unavailable"})
        with self.assertRaises(SpotifyCockpitError):
            client.play({"profile": "Alice", "target": "Office", "track": "Song"})

    def test_unsupported_status_never_becomes_idle(self):
        client = SpotifyCockpit()
        client.opener = Network({"status": "ok"})
        with self.assertRaises(SpotifyCockpitError):
            client.status()

    def test_no_redirect_of_signed_private_request(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "https://elsewhere.invalid"))

    def test_extra_secret_or_conflicting_shape_rejected_without_network(self):
        client = SpotifyCockpit()
        client.opener = Network({})
        with self.assertRaises(ValueError):
            client.play({"assignments": [], "profile": "Alice"})
        self.assertEqual(client.opener.requests, [])

    def test_signed_control_contract_and_no_observed_success_invention(self):
        client = SpotifyCockpit()
        client.opener = Network({"status": "accepted", "playback_verified": False})
        payload = {"profile": "a", "target": "living", "action": "seek", "position_ms": 12000,
                   "track_uri": "spotify:track:AAAA"}
        self.assertEqual(client.control(payload)["status"], "accepted")
        request = client.opener.requests[0]
        self.assertTrue(request.full_url.endswith("/api/spotify/control"))
        self.assertEqual(json.loads(request.data), payload)
        self.assertIsNotNone(request.get_header("X-smarthome-signature"))
        client.opener.value = {"status": "accepted", "playback_verified": True}
        with self.assertRaises(SpotifyCockpitError):
            client.control(payload)

    def test_control_extra_fields_bool_and_negative_seek_rejected_without_network(self):
        client = SpotifyCockpit()
        client.opener = Network({})
        payload = {"profile": "a", "target": "living", "action": "seek", "position_ms": 0,
                   "track_uri": "spotify:track:AAAA"}
        for invalid in ({**payload, "token": "x"}, {**payload, "position_ms": True},
                        {**payload, "position_ms": -1}, {**payload, "action": "stop"}):
            with self.assertRaises(ValueError):
                client.control(invalid)
        self.assertEqual(client.opener.requests, [])

    def test_accepted_without_verification_flag_reports_unknown_not_definite_rejection(self):
        client = SpotifyCockpit()
        client.opener = Network({"status": "accepted"})
        with self.assertRaisesRegex(SpotifyCockpitError, "Ausgang.*unbekannt") as failure:
            client.control({"profile": "a", "target": "living", "action": "pause"})
        self.assertEqual(failure.exception.outcome, "unknown")
        self.assertEqual(len(client.opener.requests), 1)

    def test_bridge_outcome_is_preserved_or_defaults_to_unknown(self):
        client = SpotifyCockpit()
        for outcome in ("not_sent", "unknown", None, "arbitrary"):
            client.opener = Network({"status": "failed", "error": "Rejected", "outcome": outcome})
            with self.assertRaises(SpotifyCockpitError) as failure:
                client.control({"profile": "a", "target": "living", "action": "pause"})
            self.assertEqual(failure.exception.outcome, outcome if outcome in {"not_sent", "unknown"} else "unknown")

    def test_raw_proxy_transport_failure_and_malformed_json_are_unknown(self):
        for bad in (TimeoutError("private-details"), b"not-json"):
            client = SpotifyCockpit()
            def open_request(*args, **kwargs):
                if isinstance(bad, Exception):
                    raise bad
                return io.BytesIO(bad)
            client.opener = SimpleNamespace(open=open_request)
            with self.assertRaises(SpotifyCockpitError) as failure:
                client.control({"profile": "a", "target": "living", "action": "pause"})
            self.assertEqual(failure.exception.outcome, "unknown")
            self.assertNotIn("private-details", str(failure.exception))


class HandlerTests(unittest.TestCase):
    def handler(self, path, payload=None, headers=None):
        handler = CockpitHandler.__new__(CockpitHandler)
        handler.path = path
        body = json.dumps(payload).encode() if payload is not None else b""
        handler.headers = {"Content-Length": str(len(body)), "Content-Type": "application/json", "Host": "cockpit.test"}
        handler.headers.update(headers or {})
        handler.rfile = io.BytesIO(body)
        handler.results = []
        handler._send_json = lambda status, value: handler.results.append((status, value))
        return handler

    def test_get_status_is_read_only(self):
        handler = self.handler("/api/spotify/status")
        with patch("dashboard.server.SpotifyCockpit", return_value=SimpleNamespace(
                status=lambda: {"available": True, "profiles": [], "targets": [], "sessions": []})):
            handler.do_GET()
        self.assertEqual(handler.results[0][0], 200)

    def test_unknown_status_returns_unavailable_not_simulated_success(self):
        handler = self.handler("/api/spotify/status")
        with patch("dashboard.server.SpotifyCockpit", side_effect=SpotifyCockpitError("Unavailable")):
            handler.do_GET()
        self.assertEqual(handler.results[0][0], 503)
        self.assertFalse(handler.results[0][1]["available"])

    def test_play_failure_http409(self):
        handler = self.handler("/api/spotify", {"profile": "Alice", "target": "Office", "track": "Song"})
        with patch("dashboard.server.SpotifyCockpit", return_value=SimpleNamespace(
                play=lambda payload: {"status": "failed", "assignments": []})):
            handler.do_POST()
        self.assertEqual(handler.results[0][0], 409)

    def test_cross_origin_spotify_write_rejected_before_credentials_or_network(self):
        handler = self.handler("/api/spotify", {}, {"Origin": "https://attacker.invalid"})
        with patch("dashboard.server.SpotifyCockpit") as client:
            handler.do_POST()
        self.assertEqual(handler.results[0][0], 403)
        client.assert_not_called()

    def test_simple_form_cannot_trigger_playback(self):
        handler = self.handler("/api/spotify", {}, {"Content-Type": "application/x-www-form-urlencoded"})
        with patch("dashboard.server.SpotifyCockpit") as client:
            handler.do_POST()
        self.assertEqual(handler.results[0][0], 400)
        client.assert_not_called()

    def test_cross_site_status_fanout_is_rejected(self):
        handler = self.handler("/api/spotify/status", headers={"Sec-Fetch-Site": "cross-site"})
        with patch("dashboard.server.SpotifyCockpit") as client:
            handler.do_GET()
        self.assertEqual(handler.results[0][0], 403)
        client.assert_not_called()

    def test_control_route_and_failure_status(self):
        handler = self.handler("/api/spotify/control", {"profile": "a", "target": "living", "action": "pause"})
        with patch("dashboard.server.SpotifyCockpit", return_value=SimpleNamespace(
                control=lambda payload: {"status": "accepted", "playback_verified": False})):
            handler.do_POST()
        self.assertEqual(handler.results[0][0], 200)
        failed = self.handler("/api/spotify/control", {"profile": "a", "target": "living", "action": "pause"})
        with patch("dashboard.server.SpotifyCockpit", side_effect=SpotifyCockpitError("Unavailable")):
            failed.do_POST()
        self.assertEqual(failed.results[0][0], 409)

    def test_control_origin_gate_before_network(self):
        for headers in ({"Origin": "https://attacker.invalid"}, {"Sec-Fetch-Site": "cross-site"},
                        {"Content-Type": "application/x-www-form-urlencoded"}):
            handler = self.handler("/api/spotify/control", {}, headers)
            with patch("dashboard.server.SpotifyCockpit") as client:
                handler.do_POST()
            self.assertIn(handler.results[0][0], (400, 403))
            self.assertEqual(handler.results[0][1]["outcome"], "not_sent")
            client.assert_not_called()

    def test_control_server_forwards_known_vs_unknown_outcome(self):
        for outcome in ("not_sent", "unknown"):
            handler = self.handler("/api/spotify/control", {"profile": "a", "target": "living", "action": "pause"})
            with patch("dashboard.server.SpotifyCockpit", side_effect=SpotifyCockpitError("Controlled error", outcome=outcome)):
                handler.do_POST()
            self.assertEqual(handler.results[0][0], 409)
            self.assertEqual(handler.results[0][1]["outcome"], outcome)


if __name__ == "__main__":
    unittest.main()
