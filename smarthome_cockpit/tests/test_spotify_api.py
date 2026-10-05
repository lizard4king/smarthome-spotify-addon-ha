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


if __name__ == "__main__":
    unittest.main()
