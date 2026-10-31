"""Signed browser wire and bounded queue checks, without actual device actions."""
import hashlib
import hmac
import http.client
import json
import threading
import time

import pytest

from codex_voice.voice_bridge import VoiceJobs, make_server


SECRET = "offline-voice-bridge-secret-0123456789-abcdef"
PRINCIPAL = "a" * 64
MEMORY = {"person_key": "", "session_key": "b" * 64}


class Runtime:
    def __init__(self):
        self.calls = []
        self.release = threading.Event()
        self.started = threading.Event()

    def authenticated(self):
        return True

    def query_browser(self, question, *, memory):
        self.calls.append((question, memory))
        self.started.set()
        self.release.wait(2)
        return "Browserantwort"

    def query_voice(self, _question):
        raise AssertionError("browser fell back to Alexa")

    def query_lumi(self, _question, *, memory):
        raise AssertionError("browser fell back to Lumi")


def submit_wire(port, payload, nonce):
    raw = json.dumps(payload, separators=(",", ":")).encode()
    stamp = str(int(time.time()))
    canonical = b"POST\n/v1/submit\n" + stamp.encode() + b"\n" + nonce.encode() + b"\n" + hashlib.sha256(raw).hexdigest().encode()
    signature = hmac.new(SECRET.encode(), canonical, hashlib.sha256).hexdigest()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    conn.request("POST", "/v1/submit", raw, {
        "Content-Type": "application/json", "X-Voice-Timestamp": stamp,
        "X-Voice-Nonce": nonce, "X-Voice-Signature": signature})
    response = conn.getresponse()
    result = response.status, json.loads(response.read())
    conn.close()
    return result


def test_signed_browser_wire_validation_and_dedupe():
    runtime = Runtime()
    try:
        server = make_server(runtime, SECRET, host="127.0.0.1", port=0)
    except PermissionError as exc:
        pytest.skip(f"loopback unavailable: {exc}")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    payload = {"principal": PRINCIPAL, "request_id": "one", "kind": "browser",
               "question": "Frage", "memory": MEMORY}
    try:
        try:
            status, result = submit_wire(server.server_port, payload, "1" * 32)
        except PermissionError as exc:
            pytest.skip(f"loopback unavailable: {exc}")
        assert status == 202
        assert runtime.started.wait(1)
        status, repeated = submit_wire(server.server_port, payload, "2" * 32)
        assert status == 202 and repeated["job_id"] == result["job_id"]
        changed = {**payload, "question": "Anders"}
        assert submit_wire(server.server_port, changed, "3" * 32)[0] == 409
        spoof = {**payload, "request_id": "spoof", "memory": {"person_key": "c" * 64, "session_key": "b" * 64}}
        assert submit_wire(server.server_port, spoof, "4" * 32)[0] == 400
        assert submit_wire(server.server_port, {**payload, "request_id": "empty", "memory": {"person_key": "", "session_key": ""}}, "5" * 32)[0] == 400
        assert submit_wire(server.server_port, {**payload, "request_id": "extra", "owner": "c" * 64}, "6" * 32)[0] == 400
        assert submit_wire(server.server_port, {**payload, "request_id": "busy"}, "7" * 32)[0] == 429
        assert runtime.calls == [("Frage", MEMORY)]
    finally:
        runtime.release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_missing_browser_method_has_no_fallback():
    class Legacy:
        def authenticated(self):
            return True
        def query_voice(self, _question):
            raise AssertionError("Alexa fallback")
        query_lumi = query_voice

    jobs = VoiceJobs(Legacy())
    try:
        status, job = jobs.submit(PRINCIPAL, "missing", "Frage", MEMORY, kind="browser")
        assert status == 202
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            status, result = jobs.result(PRINCIPAL, job["job_id"])
            if result.get("state") != "pending":
                break
            time.sleep(.01)
        assert result == {"state": "failed", "error": "unavailable"}
    finally:
        jobs.close()


def test_local_browser_forget_does_not_require_model_login():
    from codex_voice.app import CodexRuntime
    from unittest.mock import Mock
    runtime = CodexRuntime()
    runtime.authenticated = lambda: False
    runtime.query = Mock(side_effect=AssertionError("local forget must not call model"))
    jobs = VoiceJobs(runtime)
    try:
        status, first = jobs.submit(PRINCIPAL, "forget", "Vergiss unsere Gespräche", MEMORY, kind="browser")
        assert status == 202
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            status, result = jobs.result(PRINCIPAL, first["job_id"])
            if result.get("state") != "pending": break
            time.sleep(.01)
        assert result == {"state": "done", "answer": "Der Kontext dieser Sitzung wurde gelöscht."}
        runtime.query.assert_not_called()
    finally:
        jobs.close()


def test_browser_queue_dedupe_busy_and_principal_isolation():
    runtime = Runtime()
    jobs = VoiceJobs(runtime)
    try:
        status, first = jobs.submit(PRINCIPAL, "first", "Frage", MEMORY, kind="browser")
        assert status == 202 and runtime.started.wait(1)
        assert jobs.submit(PRINCIPAL, "first", "Frage", MEMORY, kind="browser")[1]["job_id"] == first["job_id"]
        assert jobs.submit(PRINCIPAL, "first", "Andere", MEMORY, kind="browser")[0] == 409
        assert jobs.submit(PRINCIPAL, "second", "Frage", MEMORY, kind="browser")[0] == 429
        assert jobs.result("c" * 64, first["job_id"])[0] == 404
        runtime.release.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            status, result = jobs.result(PRINCIPAL, first["job_id"])
            if result.get("state") == "done":
                break
            time.sleep(.01)
        assert result == {"state": "done", "answer": "Browserantwort"}
    finally:
        runtime.release.set()
        jobs.close()
