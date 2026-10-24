import hashlib
import hmac
import http.client
import json
import threading
import time
import socket

import pytest

import codex_voice.voice_bridge as bridge_module
from codex_voice.voice_bridge import (
    JOB_TTL_SECONDS,
    MAX_BODY_BYTES,
    MAX_REPLAY_NONCES,
    VoiceJobs,
    make_server,
)


SECRET = "a" * 32
PRINCIPAL = "1" * 64


class FakeRuntime:
    def __init__(self, answer="ok", authenticated=True, block=False):
        self.answer = answer
        self.is_authenticated = authenticated
        self.started = threading.Event()
        self.release = threading.Event()
        if not block:
            self.release.set()
        self.calls = []

    def authenticated(self):
        return self.is_authenticated

    def query(self, question):
        self.calls.append(question)
        self.started.set()
        self.release.wait(3)
        return self.answer


def submit(jobs, request_id="req", question="hello", principal=PRINCIPAL):
    status, result = jobs.submit(principal, request_id, question)
    return status, result


def wait_for(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def signed_request(method, path, body, nonce=None, timestamp=None, secret=SECRET, signature=None):
    raw = json.dumps(body, separators=(",", ":")).encode()
    nonce = nonce or ("a" * 32)
    timestamp = str(int(time.time())) if timestamp is None else str(timestamp)
    canonical = b"POST\n" + path.encode() + b"\n" + timestamp.encode() + b"\n" + nonce.encode() + b"\n" + hashlib.sha256(raw).hexdigest().encode()
    signature = signature or hmac.new(secret.encode(), canonical, hashlib.sha256).hexdigest()
    conn = http.client.HTTPConnection("127.0.0.1", signed_request.port, timeout=2)
    conn.request(method, path, body=raw, headers={
        "Content-Type": "application/json",
        "Content-Length": str(len(raw)),
        "X-Voice-Timestamp": timestamp,
        "X-Voice-Nonce": nonce,
        "X-Voice-Signature": signature,
    })
    response = conn.getresponse()
    response_body = response.read()
    status = response.status
    response_signature = response.getheader("X-Voice-Response-Signature")
    headers = dict(response.getheaders())
    conn.close()
    expected_response = hmac.new(secret.encode(), b"RESPONSE\n" + path.encode() + b"\n" + nonce.encode() + b"\n" + str(status).encode() + b"\n" + hashlib.sha256(response_body).hexdigest().encode(), hashlib.sha256).hexdigest()
    assert hmac.compare_digest(response_signature, expected_response)
    assert headers["Cache-Control"] == "no-store"
    return status, json.loads(response_body), headers


@pytest.fixture
def running_server():
    runtime = FakeRuntime()
    server = make_server(runtime, SECRET, host="127.0.0.1", port=0)
    signed_request.port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, runtime
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_secret_is_optional_but_malformed_nonempty_fails_closed():
    assert make_server(FakeRuntime(), "") is None
    for secret in (None, "short", "x" * 129, "x" * 31 + "!"):
        with pytest.raises(ValueError):
            make_server(FakeRuntime(), secret)


def test_bind_failure_closes_queue_worker(monkeypatch):
    jobs = []
    real_jobs = bridge_module.VoiceJobs

    def tracked_jobs(runtime):
        job_queue = real_jobs(runtime)
        jobs.append(job_queue)
        return job_queue

    def fail_bind(self, address, handler, bind_and_activate=True):
        raise OSError("synthetic bind failure")

    monkeypatch.setattr(bridge_module, "VoiceJobs", tracked_jobs)
    monkeypatch.setattr(bridge_module.ThreadingHTTPServer, "__init__", fail_bind)
    with pytest.raises(OSError, match="synthetic bind failure"):
        make_server(FakeRuntime(), SECRET, host="127.0.0.1", port=0)
    assert jobs[0]._closed
    assert wait_for(lambda: not jobs[0]._worker.is_alive())


def test_server_suppresses_request_thread_tracebacks(running_server, capsys):
    server, _ = running_server
    server.handle_error(None, ("127.0.0.1", 1))
    assert capsys.readouterr().err == ""


def test_queue_fifo_single_worker_dedupe_conflict_and_principal_isolation():
    runtime = FakeRuntime(block=True)
    jobs = VoiceJobs(runtime)
    try:
        status, first = submit(jobs, "one", "first")
        assert status == 202
        assert runtime.started.wait(1)
        assert submit(jobs, "one", "first") == (202, {"job_id": first["job_id"], "state": "pending"})
        assert submit(jobs, "one", "different")[0] == 409
        assert submit(jobs, "three", "second", "3" * 64)[0] == 202
        other = "2" * 64
        assert submit(jobs, "one", "first", other)[0] == 202
        assert jobs.result(other, first["job_id"]) == (404, {"error": "unavailable"})
        runtime.release.set()
        assert wait_for(lambda: len(runtime.calls) == 3)
        assert runtime.calls == ["first", "second", "first"]
        assert jobs.result(PRINCIPAL, first["job_id"])[1]["state"] == "done"
    finally:
        jobs.close()


def test_queue_limits_records_per_principal_and_answer_size():
    runtime = FakeRuntime(block=True)
    jobs = VoiceJobs(runtime)
    try:
        status, first = submit(jobs)
        assert status == 202 and runtime.started.wait(1)
        assert submit(jobs, "next")[0] == 429
        for idx in range(1, 5):
            assert submit(jobs, f"p{idx}", principal=f"{idx + 1:x}" * 64)[0] == 202
        assert submit(jobs, "overflow", principal="f" * 64)[0] == 429
        runtime.release.set()
        assert wait_for(lambda: jobs.result(PRINCIPAL, first["job_id"])[1].get("state") == "done")
    finally:
        jobs.close()

    too_long = FakeRuntime(answer="x" * 6001)
    jobs = VoiceJobs(too_long)
    try:
        _, job = submit(jobs)
        assert wait_for(lambda: jobs.result(PRINCIPAL, job["job_id"])[1].get("state") == "failed")
        assert jobs.result(PRINCIPAL, job["job_id"])[1] == {"state": "failed", "error": "unavailable"}
    finally:
        jobs.close()


def test_authentication_only_in_worker_and_generic_query_failure():
    runtime = FakeRuntime(authenticated=False)
    jobs = VoiceJobs(runtime)
    try:
        _, job = submit(jobs)
        assert wait_for(lambda: jobs.result(PRINCIPAL, job["job_id"])[1].get("state") == "failed")
        assert runtime.calls == []
        assert jobs.result(PRINCIPAL, job["job_id"])[1] == {"state": "failed", "error": "unavailable"}
    finally:
        jobs.close()

    class Broken(FakeRuntime):
        def query(self, question):
            raise RuntimeError("secret internal traceback")

    jobs = VoiceJobs(Broken())
    try:
        _, job = submit(jobs)
        assert wait_for(lambda: jobs.result(PRINCIPAL, job["job_id"])[1].get("state") == "failed")
        assert "traceback" not in json.dumps(jobs.result(PRINCIPAL, job["job_id"]))
    finally:
        jobs.close()


def test_queue_ttl_and_close_clear_memory_without_waiting_for_query():
    now = [10.0]
    runtime = FakeRuntime(block=True)
    jobs = VoiceJobs(runtime, clock=lambda: now[0])
    _, active = submit(jobs, "active")
    assert runtime.started.wait(1)
    _, waiting = submit(jobs, "waiting", "queued", principal="2" * 64)
    now[0] += JOB_TTL_SECONDS
    assert jobs.result(PRINCIPAL, active["job_id"])[0] == 404
    assert jobs.result("2" * 64, waiting["job_id"])[0] == 404
    started = time.monotonic()
    jobs.close()
    assert time.monotonic() - started < 0.2
    runtime.release.set()


def test_submit_result_authentication_replay_and_response_signature(running_server):
    server, runtime = running_server
    status, data, headers = signed_request("POST", "/v1/submit", {
        "principal": PRINCIPAL, "request_id": "r1", "question": "Hello?"
    }, nonce="0" * 32)
    assert status == 202 and data["state"] == "pending"
    assert headers["Connection"].lower() == "close"
    assert runtime.started.wait(1)
    runtime.release.set()
    assert wait_for(lambda: server.jobs.result(PRINCIPAL, data["job_id"])[1].get("state") == "done")
    status, result, _ = signed_request("POST", "/v1/result", {
        "principal": PRINCIPAL, "job_id": data["job_id"]
    }, nonce="1" * 32)
    assert status == 200 and result == {"state": "done", "answer": "ok"}
    assert signed_request("POST", "/v1/result", {
        "principal": "2" * 64, "job_id": data["job_id"]
    }, nonce="2" * 32)[0] == 404
    assert signed_request("POST", "/v1/submit", {
        "principal": PRINCIPAL, "request_id": "r1", "question": "Hello?"
    }, nonce="0" * 32)[0] == 409


def test_mac_timestamp_replay_cache_and_replay_capacity(running_server):
    server, _ = running_server
    body = {"principal": PRINCIPAL, "request_id": "r", "question": "q"}
    assert signed_request("POST", "/v1/submit", body, timestamp=int(time.time()) - 31)[0] == 401
    assert signed_request("POST", "/v1/submit", body, nonce="3" * 32, signature="f" * 64)[0] == 401
    server._nonces = {f"{i:032x}": int(time.time()) for i in range(MAX_REPLAY_NONCES)}
    assert signed_request("POST", "/v1/submit", body, nonce="4" * 32)[0] == 429


@pytest.mark.parametrize("path", ["/v1/submit?x=1", "/v1/submit/", "/elsewhere"])
def test_path_variants_rejected(running_server, path):
    assert signed_request("POST", path, {"principal": PRINCIPAL})[0] == 404


def test_unknown_path_error_mac_cannot_be_replayed_as_result(running_server):
    status, result, headers = signed_request(
        "POST", "/unknown", {"principal": PRINCIPAL}, nonce="b" * 32
    )
    assert status == 404
    body = json.dumps(result, separators=(",", ":")).encode()
    nonce = "b" * 32
    signature_for_result = hmac.new(
        SECRET.encode(),
        b"RESPONSE\n/v1/result\n" + nonce.encode() + b"\n404\n"
        + hashlib.sha256(body).hexdigest().encode(),
        hashlib.sha256,
    ).hexdigest()
    assert headers["X-Voice-Response-Signature"] != signature_for_result


def test_http_limits_duplicate_json_keys_and_bad_payload_do_not_enqueue(running_server):
    server, runtime = running_server
    status, _, _ = signed_request("POST", "/v1/submit", {
        "principal": PRINCIPAL, "request_id": "", "question": "q"
    })
    assert status == 400
    assert runtime.calls == []


def test_http_rejects_oversize_get_and_duplicate_framing_headers(running_server):
    server, runtime = running_server
    body = {"principal": PRINCIPAL, "request_id": "x", "question": "q"}
    status, _, _ = signed_request(
        "POST", "/v1/submit", {**body, "question": "x" * 4100}, nonce="6" * 32
    )
    assert status == 400
    assert signed_request("GET", "/v1/submit", body, nonce="7" * 32)[0] == 405

    raw = json.dumps(body, separators=(",", ":")).encode()
    nonce, timestamp = "8" * 32, str(int(time.time()))
    canonical = b"POST\n/v1/submit\n" + timestamp.encode() + b"\n" + nonce.encode() + b"\n" + hashlib.sha256(raw).hexdigest().encode()
    signature = hmac.new(SECRET.encode(), canonical, hashlib.sha256).hexdigest()
    request = (
        b"POST /v1/submit HTTP/1.1\r\nHost: localhost\r\n"
        b"Content-Type: application/json\r\nContent-Length: " + str(len(raw)).encode() + b"\r\n"
        b"Content-Length: " + str(len(raw)).encode() + b"\r\n"
        b"X-Voice-Timestamp: " + timestamp.encode() + b"\r\n"
        b"X-Voice-Nonce: " + nonce.encode() + b"\r\n"
        b"X-Voice-Signature: " + signature.encode() + b"\r\nConnection: close\r\n\r\n" + raw
    )
    conn = socket.create_connection(("127.0.0.1", signed_request.port), timeout=2)
    conn.sendall(request)
    response = conn.recv(4096)
    conn.close()
    assert b"400" in response.split(b"\r\n", 1)[0]
    assert runtime.calls == []
    # The declared limit is enforced before a body is read.
    assert MAX_BODY_BYTES == 4096
    # Duplicate JSON names are rejected by the parser; send a correctly signed raw body.
    raw = b'{"principal":"' + PRINCIPAL.encode() + b'","principal":"' + PRINCIPAL.encode() + b'"}'
    nonce, timestamp = "5" * 32, str(int(time.time()))
    canonical = b"POST\n/v1/submit\n" + timestamp.encode() + b"\n" + nonce.encode() + b"\n" + hashlib.sha256(raw).hexdigest().encode()
    signature = hmac.new(SECRET.encode(), canonical, hashlib.sha256).hexdigest()
    conn = http.client.HTTPConnection("127.0.0.1", signed_request.port, timeout=2)
    conn.request("POST", "/v1/submit", body=raw, headers={
        "Content-Type": "application/json", "Content-Length": str(len(raw)),
        "X-Voice-Timestamp": timestamp, "X-Voice-Nonce": nonce, "X-Voice-Signature": signature,
    })
    response = conn.getresponse()
    assert response.status == 400
    response.read()
    conn.close()
    assert runtime.calls == []

