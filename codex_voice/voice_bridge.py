"""Optional, private-network voice bridge with a bounded in-memory job queue."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from collections import OrderedDict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


MAX_BODY_BYTES = 4096
MAX_QUESTION_CHARS = 600
MAX_ANSWER_CHARS = 6000
MAX_HTTP_THREADS = 4
SOCKET_TIMEOUT_SECONDS = 3
MAX_INFLIGHT = 5
MAX_RECORDS = 32
MAX_PER_PRINCIPAL = 1
JOB_TTL_SECONDS = 600
MAX_REPLAY_NONCES = 256
MAX_CLOCK_SKEW_SECONDS = 30
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8098

_PRINCIPAL_RE = re.compile(r"[0-9a-f]{64}\Z")
_MEMORY_KEY_RE = re.compile(r"[0-9a-f]{64}\Z")
_NONCE_RE = re.compile(r"[0-9a-f]{32}\Z")
_SIGNATURE_RE = re.compile(r"[0-9a-f]{64}\Z")
_JOB_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
_SECRET_RE = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")


class _ProtocolError(Exception):
    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status = status
        self.code = code


def _json_bytes(value: dict) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _mac(key: bytes, data: bytes) -> str:
    return hmac.new(key, data, hashlib.sha256).hexdigest()


def _valid_memory(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"person_key", "session_key"}
        and all(
            isinstance(value[key], str)
            and (value[key] == "" or _MEMORY_KEY_RE.fullmatch(value[key]))
            for key in ("person_key", "session_key")
        )
    )


class VoiceJobs:
    """FIFO single-worker queue; all job state stays in process memory."""

    def __init__(self, runtime, *, clock=time.monotonic):
        self.runtime = runtime
        self._clock = clock
        self._condition = threading.Condition()
        self._queue: deque[str] = deque()
        self._records: OrderedDict[str, dict] = OrderedDict()
        self._dedupe: dict[tuple[str, str], str] = {}
        self._active_principal: str | None = None
        self._closed = False
        self._worker = threading.Thread(target=self._run, name="voice-queue", daemon=True)
        self._worker.start()

    def _expire_locked(self, now: float) -> None:
        for job_id, record in list(self._records.items()):
            if now - record["created"] >= JOB_TTL_SECONDS:
                self._records.pop(job_id, None)
                self._dedupe.pop((record["principal"], record["request_id"]), None)
        self._queue = deque(job_id for job_id in self._queue if job_id in self._records)

    def submit(
        self, principal: str, request_id: str, question: str | None = None,
        memory: dict[str, str] | None = None,
        *, kind: str | None = None,
    ) -> tuple[int, dict]:
        if kind not in (None, "greeting"):
            return 400, {"error": "invalid request"}
        if kind == "greeting" and (question is not None or not _valid_memory(memory)):
            return 400, {"error": "invalid request"}
        if kind is None and (not isinstance(question, str) or not question.strip()
                             or len(question) > MAX_QUESTION_CHARS):
            return 400, {"error": "invalid request"}
        if memory is not None and not _valid_memory(memory):
            return 400, {"error": "invalid request"}
        digest_value = ({"kind": "greeting", "memory": memory} if kind == "greeting"
                        else {"question": question, "memory": memory})
        digest = hashlib.sha256(_json_bytes(digest_value)).hexdigest()
        now = self._clock()
        with self._condition:
            if self._closed:
                return 503, {"error": "unavailable"}
            self._expire_locked(now)
            old_id = self._dedupe.get((principal, request_id))
            if old_id:
                old = self._records.get(old_id)
                if old and old["question_hash"] == digest:
                    state = old["state"]
                    return 202, {"job_id": old_id, "state": "pending" if state == "running" else state}
                return 409, {"error": "conflict"}
            inflight = [r for r in self._records.values() if r["state"] == "pending"]
            active_count = 1 if self._active_principal is not None else 0
            if len(inflight) + active_count >= MAX_INFLIGHT or len(self._records) >= MAX_RECORDS:
                return 429, {"error": "busy"}
            if self._active_principal == principal or any(r["principal"] == principal for r in inflight):
                return 429, {"error": "busy"}
            job_id = secrets.token_hex(16)
            record = {
                "principal": principal,
                "request_id": request_id,
                "question_hash": digest,
                "created": now,
                "state": "pending",
                "answer": None,
            }
            if kind == "greeting":
                record["kind"] = "greeting"
            else:
                record["question"] = question
            if memory is not None:
                record["memory"] = dict(memory)
            self._records[job_id] = record
            self._dedupe[(principal, request_id)] = job_id
            self._queue.append(job_id)
            self._condition.notify()
            return 202, {"job_id": job_id, "state": "pending"}

    def result(self, principal: str, job_id: str) -> tuple[int, dict]:
        with self._condition:
            self._expire_locked(self._clock())
            record = self._records.get(job_id)
            if not record or record["principal"] != principal:
                return 404, {"error": "unavailable"}
            if record["state"] == "done":
                return 200, {"state": "done", "answer": record["answer"]}
            if record["state"] == "failed":
                return 200, {"state": "failed", "error": "unavailable"}
            return 200, {"state": "pending"}

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._queue and not self._closed:
                    self._condition.wait()
                if self._closed:
                    return
                job_id = self._queue.popleft()
                record = self._records.get(job_id)
                if record is None or self._clock() - record["created"] >= JOB_TTL_SECONDS:
                    self._expire_locked(self._clock())
                    continue
                record["state"] = "running"
                self._active_principal = record["principal"]
                kind = record.pop("kind", None)
                question = record.pop("question", None)
                memory = record.pop("memory", None)
            try:
                if not self.runtime.authenticated():
                    raise RuntimeError("unavailable")
                if kind == "greeting":
                    greeting = getattr(self.runtime, "greet_lumi", None)
                    if not callable(greeting):
                        raise RuntimeError("unavailable")
                    answer = greeting(memory=dict(memory))
                elif memory is None:
                    query = getattr(self.runtime, "query_voice", self.runtime.query)
                    answer = query(question)
                else:
                    query_lumi = getattr(self.runtime, "query_lumi", None)
                    if not callable(query_lumi):
                        raise RuntimeError("unavailable")
                    query_request = getattr(self.runtime, "query_lumi_request", None)
                    if callable(query_request):
                        operation_id = hashlib.sha256(_json_bytes({
                            "principal": record["principal"],
                            "request_id": record["request_id"],
                        })).hexdigest()
                        answer = query_request(question, memory=dict(memory), request_id=operation_id)
                    else:
                        answer = query_lumi(question, memory=dict(memory))
                if not isinstance(answer, str) or len(answer) > MAX_ANSWER_CHARS:
                    raise RuntimeError("unavailable")
            except Exception as exc:
                print("Voice request failed:", type(exc).__name__, getattr(exc, "diagnosis", "unavailable"), flush=True)
                answer = None
                state = "failed"
            else:
                state = "done"
            with self._condition:
                self._active_principal = None
                current = self._records.get(job_id)
                if current is not None and not self._closed:
                    current["state"] = state
                    current["answer"] = answer
                    self._condition.notify_all()

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._queue.clear()
            self._records.clear()
            self._dedupe.clear()
            self._condition.notify_all()


class _BoundedVoiceServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 8
    allow_reuse_address = True

    def __init__(self, address, handler, runtime, secret: str):
        self.jobs = VoiceJobs(runtime)
        self._key = secret.encode("ascii")
        self._replay_lock = threading.Lock()
        self._nonces: dict[str, int] = {}
        self._request_slots = threading.BoundedSemaphore(MAX_HTTP_THREADS)
        try:
            super().__init__(address, handler)
        except Exception:
            self.jobs.close()
            raise

    def handle_error(self, request, client_address):
        return

    def process_request(self, request, client_address):
        if not self._request_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._request_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()

    def server_close(self):
        self.jobs.close()
        super().server_close()

    def close(self):
        self.server_close()

    def shutdown(self):
        self.jobs.close()
        super().shutdown()


class _VoiceHandler(BaseHTTPRequestHandler):
    server: _BoundedVoiceServer

    def setup(self):
        super().setup()
        self.connection.settimeout(SOCKET_TIMEOUT_SECONDS)

    def log_message(self, _format, *args):
        return

    def send_error(self, code, message=None, explain=None):
        values = self.headers.get_all("X-Voice-Nonce", []) if hasattr(self, "headers") else []
        nonce = values[0] if len(values) == 1 and _NONCE_RE.fullmatch(values[0]) else ""
        self._respond(code, {"error": "unavailable" if code >= 500 else "invalid request"}, nonce,
                      getattr(self, "path", ""))

    def do_GET(self):
        values = self.headers.get_all("X-Voice-Nonce", [])
        nonce = values[0] if len(values) == 1 and _NONCE_RE.fullmatch(values[0]) else ""
        self._respond(405, {"error": "invalid request"}, nonce, self.path)

    def _respond(self, status: int, data: dict, nonce: str, path: str) -> None:
        body = _json_bytes(data)
        signature = _mac(
            self.server._key,
            b"RESPONSE\n" + path.encode("utf-8") + b"\n"
            + nonce.encode("ascii", "ignore") + b"\n" + str(status).encode()
            + b"\n" + hashlib.sha256(body).hexdigest().encode(),
        )
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Voice-Response-Signature", signature)
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def _one_header(self, name: str) -> str:
        values = self.headers.get_all(name, [])
        if len(values) != 1:
            raise _ProtocolError(400, "invalid request")
        return values[0]

    def _authenticated_body(self, path: str) -> tuple[bytes, str]:
        if self.path != path:
            raise _ProtocolError(404, "unavailable")
        if self.headers.get_all("Transfer-Encoding", []):
            raise _ProtocolError(400, "invalid request")
        length_text = self._one_header("Content-Length")
        if not length_text.isascii() or not length_text.isdecimal():
            raise _ProtocolError(400, "invalid request")
        length = int(length_text)
        if length < 1 or length > MAX_BODY_BYTES:
            raise _ProtocolError(400, "invalid request")
        if self._one_header("Content-Type").lower() != "application/json":
            raise _ProtocolError(400, "invalid request")
        timestamp = self._one_header("X-Voice-Timestamp")
        nonce = self._one_header("X-Voice-Nonce")
        signature = self._one_header("X-Voice-Signature")
        if not timestamp.isascii() or not timestamp.isdecimal() or not _NONCE_RE.fullmatch(nonce) or not _SIGNATURE_RE.fullmatch(signature):
            raise _ProtocolError(401, "unauthenticated")
        stamp = int(timestamp)
        if abs(time.time() - stamp) > MAX_CLOCK_SKEW_SECONDS:
            raise _ProtocolError(401, "unauthenticated")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise _ProtocolError(400, "invalid request")
        canonical = b"POST\n" + path.encode() + b"\n" + timestamp.encode() + b"\n" + nonce.encode() + b"\n" + hashlib.sha256(raw).hexdigest().encode()
        if not hmac.compare_digest(_mac(self.server._key, canonical), signature):
            raise _ProtocolError(401, "unauthenticated")
        now = int(time.time())
        with self.server._replay_lock:
            self.server._nonces = {n: ts for n, ts in self.server._nonces.items() if abs(now - ts) <= MAX_CLOCK_SKEW_SECONDS}
            if nonce in self.server._nonces:
                raise _ProtocolError(409, "conflict")
            if len(self.server._nonces) >= MAX_REPLAY_NONCES:
                raise _ProtocolError(429, "busy")
            self.server._nonces[nonce] = stamp
        if self._one_header("Content-Length") != length_text:
            raise _ProtocolError(400, "invalid request")
        return raw, nonce

    @staticmethod
    def _json_object(raw: bytes) -> dict:
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError("duplicate key")
                result[key] = value
            return result

        value = json.loads(raw, object_pairs_hook=pairs)
        if not isinstance(value, dict):
            raise ValueError("not an object")
        return value

    def do_POST(self):
        nonce_values = self.headers.get_all("X-Voice-Nonce", [])
        response_nonce = nonce_values[0] if len(nonce_values) == 1 and _NONCE_RE.fullmatch(nonce_values[0]) else ""
        try:
            if self.path == "/v1/submit":
                path = "/v1/submit"
            elif self.path == "/v1/result":
                path = "/v1/result"
            else:
                raise _ProtocolError(404, "unavailable")
            raw, response_nonce = self._authenticated_body(path)
            try:
                data = self._json_object(raw)
            except (ValueError, json.JSONDecodeError):
                raise _ProtocolError(400, "invalid request")
            principal = data.get("principal")
            if not isinstance(principal, str) or not _PRINCIPAL_RE.fullmatch(principal):
                raise _ProtocolError(400, "invalid request")
            if path == "/v1/submit":
                legacy_fields = {"principal", "request_id", "question"}
                memory_fields = legacy_fields | {"memory"}
                greeting_fields = {"principal", "request_id", "kind", "memory"}
                if set(data) not in (legacy_fields, memory_fields, greeting_fields):
                    raise _ProtocolError(400, "invalid request")
                request_id = data.get("request_id")
                question = data.get("question")
                if not isinstance(request_id, str) or not 1 <= len(request_id) <= 1024:
                    raise _ProtocolError(400, "invalid request")
                greeting = set(data) == greeting_fields
                if greeting:
                    if data["kind"] != "greeting":
                        raise _ProtocolError(400, "invalid request")
                elif not isinstance(question, str) or not question.strip() or len(question) > MAX_QUESTION_CHARS:
                    raise _ProtocolError(400, "invalid request")
                memory_present = "memory" in data
                memory = data.get("memory")
                if memory_present and not _valid_memory(memory):
                    raise _ProtocolError(400, "invalid request")
                status, result = self.server.jobs.submit(
                    principal, request_id, None if greeting else question.strip(), memory=memory,
                    kind="greeting" if greeting else None,
                )
            else:
                if set(data) != {"principal", "job_id"}:
                    raise _ProtocolError(400, "invalid request")
                job_id = data.get("job_id")
                if not isinstance(job_id, str) or not _JOB_ID_RE.fullmatch(job_id):
                    raise _ProtocolError(400, "invalid request")
                status, result = self.server.jobs.result(principal, job_id)
        except _ProtocolError as exc:
            status, result = exc.status, {"error": exc.code}
        except Exception:
            status, result = 400, {"error": "invalid request"}
        self._respond(status, result, response_nonce, self.path)


def make_server(runtime, secret, host=DEFAULT_HOST, port=DEFAULT_PORT):
    """Return None when disabled; reject malformed non-empty secrets closed."""
    if secret == "":
        return None
    if not isinstance(secret, str) or not _SECRET_RE.fullmatch(secret):
        raise ValueError("invalid voice bridge secret")
    return _BoundedVoiceServer((host, port), _VoiceHandler, runtime, secret)

