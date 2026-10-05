"""Offline HMAC v2 and single-use transport request checks."""
import hashlib
import hmac
import io
import json
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "smarthome_cockpit"))
sys.path.insert(0, str(ROOT / "smarthome_spotify_bridge"))
from smarthome.spotify_bridge import (
    ASSIGNMENTS_PATH, BRIDGE_PATH, CONTROL_PATH, MAX_BODY_BYTES,
    SEARCH_PATH, STATUS_PATH, SpotifyBridge, SpotifyBridgeError,
)
from dashboard.spotify import SpotifyCockpit, SpotifyCockpitError

SECRET = "offline-test-secret-" * 3
NONCE = "0123456789abcdef" * 2


def signed(body, *, timestamp=1000, nonce=NONCE, path=CONTROL_PATH,
           method="POST", secret=SECRET, version="2"):
    stamp = str(timestamp)
    message = (f"v2.{method}.{path}.{stamp}.{nonce}." if version == "2" else f"{stamp}.").encode() + body
    headers = {"X-SmartHome-Timestamp": stamp,
               "X-SmartHome-Signature": hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()}
    if version is not None:
        headers.update({"X-SmartHome-Signature-Version": version, "X-SmartHome-Nonce": nonce})
    return headers


class ControlAuthTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000
        self.calls = []
        self.body = json.dumps({"profile": "a", "target": "living", "action": "pause"}).encode()
        self.dispatcher = SimpleNamespace(
            control=lambda payload, **kw: self.calls.append(payload) or {"status": "accepted"},
            status=lambda **kw: {"status": "ok"}, search=lambda *a, **kw: [],
            play_assignments=lambda *a, **kw: {"status": "accepted"},
            dispatch=lambda *a, **kw: SimpleNamespace(status=SimpleNamespace(value="accepted")),
        )
        self.bridge = SpotifyBridge(self.dispatcher, SECRET, clock=lambda: self.now)

    def control(self, headers=None, body=None, **kwargs):
        return self.bridge.handle_cockpit(method=kwargs.get("method", "POST"),
            path=kwargs.get("path", CONTROL_PATH), headers=headers or signed(self.body),
            body=self.body if body is None else body)

    def test_valid_once_identical_retry_rejected(self):
        self.assertEqual(self.control()["status"], "accepted")
        with self.assertRaises(SpotifyBridgeError):
            self.control()
        self.assertEqual(len(self.calls), 1)

    def test_concurrent_same_nonce_dispatches_only_once(self):
        barrier = threading.Barrier(2)
        def submit():
            barrier.wait(timeout=3)
            try:
                self.control()
                return True
            except SpotifyBridgeError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: submit(), range(2)))
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(len(self.calls), 1)

    def test_wrong_path_method_and_secret_do_not_claim_nonce(self):
        for headers in (signed(self.body, path=STATUS_PATH), signed(self.body, method="GET"),
                        signed(self.body, secret="other-secret-" * 4)):
            with self.assertRaises(SpotifyBridgeError):
                self.control(headers)
        self.assertEqual(self.bridge._control_nonces, {})
        self.control()

    def test_body_tampering_and_wrong_route_method_are_rejected(self):
        with self.assertRaises(SpotifyBridgeError):
            self.control(signed(self.body), self.body + b" ")
        for method in ("GET", "post"):
            with self.assertRaises(SpotifyBridgeError):
                self.control(method=method)
        with self.assertRaises(SpotifyBridgeError):
            self.control(path=STATUS_PATH)
        self.assertEqual(self.bridge._control_nonces, {})
        self.assertEqual(self.calls, [])

    def test_absent_wrong_version_and_malformed_nonce_rejected(self):
        for version in (None, "1", "3"):
            with self.assertRaises(SpotifyBridgeError):
                self.control(signed(self.body, version=version))
        for nonce in ("", "a" * 31, "a" * 33, "G" * 32, "A" * 32):
            with self.assertRaises(SpotifyBridgeError):
                self.control(signed(self.body, nonce=nonce))
        self.assertEqual(self.calls, [])

    def test_control_v2_signature_cannot_be_replayed_to_legacy_routes(self):
        headers = signed(self.body)
        for path in (ASSIGNMENTS_PATH, STATUS_PATH, SEARCH_PATH):
            with self.subTest(path=path):
                handler = self.bridge.handle_search if path == SEARCH_PATH else self.bridge.handle_cockpit
                with self.assertRaisesRegex(SpotifyBridgeError, "Signatur"):
                    handler(method="POST", path=path, headers=headers, body=self.body)
                self.assertEqual(self.bridge._control_nonces, {})
        self.assertEqual(self.calls, [])

    def test_v1_signature_with_fake_v2_headers_cannot_authorize_control(self):
        headers = signed(self.body, version=None)
        headers.update({"X-SmartHome-Signature-Version": "2", "X-SmartHome-Nonce": NONCE})
        with self.assertRaisesRegex(SpotifyBridgeError, "Signatur"):
            self.control(headers)
        self.assertEqual(self.bridge._control_nonces, {})
        self.assertEqual(self.calls, [])

    def test_expired_future_and_non_decimal_timestamp_rejected(self):
        for stamp in (699, 1301, "+1000", "01000", " 1000", "1e3"):
            with self.assertRaises(SpotifyBridgeError):
                self.control(signed(self.body, timestamp=stamp))
        self.assertEqual(self.calls, [])

    def test_future_timestamp_replay_kept_until_full_window_expires(self):
        headers = signed(self.body, timestamp=1300)
        self.control(headers)
        self.now = 1600
        with self.assertRaises(SpotifyBridgeError):
            self.control(headers)
        self.assertEqual(len(self.calls), 1)
        self.now = 1601
        self.control(signed(self.body, timestamp=1601, nonce="b" * 32))
        self.assertNotIn(NONCE, self.bridge._control_nonces)

    def test_full_cache_fails_closed_and_expired_entries_release_capacity(self):
        with patch("smarthome.spotify_bridge.MAX_CONTROL_NONCES", 1):
            self.control()
            with self.assertRaises(SpotifyBridgeError):
                self.control(signed(self.body, nonce="b" * 32))
            with self.assertRaises(SpotifyBridgeError):
                self.control()
            self.now = 1301
            self.control(signed(self.body, timestamp=1301, nonce="b" * 32))
        self.assertEqual(len(self.calls), 2)

    def test_oversized_does_not_claim_nonce(self):
        body = b"x" * (MAX_BODY_BYTES + 1)
        with self.assertRaises(SpotifyBridgeError):
            self.control(signed(body), body)
        self.assertEqual(self.bridge._control_nonces, {})

    def test_legacy_status_search_assignments_and_commands_remain_v1(self):
        for path, value in ((STATUS_PATH, {}), (ASSIGNMENTS_PATH, {"assignments": []})):
            body = json.dumps(value).encode()
            for _ in range(2):
                self.bridge.handle_cockpit(method="POST", path=path, body=body,
                                          headers=signed(body, version=None))
        body = json.dumps({"profile_alias": "a", "query": "Song"}).encode()
        self.bridge.handle_search(method="POST", path=SEARCH_PATH, body=body,
                                  headers=signed(body, version=None))
        body = json.dumps({"intent": {"name": "SpotifyStopIntent"},
            "voice_identity": {"status": "absent", "person_id": None},
            "session": {"profile_id": None}}).encode()
        self.bridge.handle(method="POST", path=BRIDGE_PATH, body=body,
                           headers=signed(body, version=None))
        self.assertEqual(self.bridge._control_nonces, {})


class DashboardSigningTests(unittest.TestCase):
    def setUp(self):
        self.client = SpotifyCockpit()
        self.client.url = "http://offline.invalid"
        self.client.secret = SECRET
        self.requests = []
        def open_request(request, timeout):
            self.requests.append(request)
            return io.BytesIO(b'{"status":"accepted"}')
        self.client.opener = SimpleNamespace(open=open_request)

    def test_control_signs_canonical_v2_with_fresh_nonce(self):
        with patch("dashboard.spotify.time.time", return_value=1000):
            self.client.request(CONTROL_PATH, {"action": "pause"})
            self.client.request(CONTROL_PATH, {"action": "pause"})
        nonces = []
        for request in self.requests:
            headers = {key.lower(): value for key, value in request.header_items()}
            nonce = headers["x-smarthome-nonce"]
            nonces.append(nonce)
            self.assertRegex(nonce, r"^[0-9a-f]{32}$")
            self.assertEqual(headers["x-smarthome-signature-version"], "2")
            expected = signed(request.data, nonce=nonce)["X-SmartHome-Signature"]
            self.assertEqual(headers["x-smarthome-signature"], expected)
        self.assertNotEqual(*nonces)

    def test_legacy_signing_unchanged(self):
        with patch("dashboard.spotify.time.time", return_value=1000):
            self.client.request(STATUS_PATH, {})
        request = self.requests[0]
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertNotIn("x-smarthome-nonce", headers)
        self.assertNotIn("x-smarthome-signature-version", headers)
        self.assertEqual(headers["x-smarthome-signature"],
                         signed(request.data, version=None)["X-SmartHome-Signature"])

    def test_transport_failure_is_never_retried(self):
        def fail(request, timeout):
            self.requests.append(request)
            raise TimeoutError("offline")
        self.client.opener.open = fail
        with self.assertRaises(SpotifyCockpitError):
            self.client.request(CONTROL_PATH, {})
        self.assertEqual(len(self.requests), 1)


if __name__ == "__main__":
    unittest.main()
