"""Loopback-only HTTP receiver for authenticated Amazon SNS messages."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable

from .amazon_portability_api import PortabilityApiError, parse_response_notification
from .amazon_sns import SNS_TOPIC_ARN_PATTERN as _TOPIC_ARN, SnsVerificationError, VerifiedSnsMessage, verify_sns_message
from .amazon_sns_inbox import AmazonSnsInbox, InboxError

MAX_BODY_BYTES = 16_384
REQUEST_TIMEOUT_SECONDS = 5
RATE_LIMIT = 60
RATE_WINDOW_SECONDS = 60.0
_RATE_LOCK = threading.Lock()
_REQUEST_TIMES: deque[float] = deque()
_CONTENT_LENGTH = re.compile(r"^[0-9]+$")
_DISCOVERY_FILE_LIMIT = 64 * 1024


def _record_discovered_topic(path: Path, topic_arn: str) -> None:
    """Atomically publish verified topic candidates without SNS secrets."""
    if not isinstance(path, Path) or not path.is_absolute() or not _TOPIC_ARN.fullmatch(topic_arn):
        raise ValueError("invalid discovery configuration")
    path.parent.mkdir(parents=True, exist_ok=True)
    topics: set[str] = set()
    if path.exists():
        if path.is_symlink() or path.stat().st_size > _DISCOVERY_FILE_LIMIT:
            raise ValueError("invalid discovery file")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("invalid discovery file") from None
        if (
            not isinstance(value, dict)
            or set(value) != {"topic_arns"}
            or not isinstance(value["topic_arns"], list)
            or any(not isinstance(item, str) or not _TOPIC_ARN.fullmatch(item)
                   for item in value["topic_arns"])
        ):
            raise ValueError("invalid discovery file")
        topics.update(value["topic_arns"])
    topics.add(topic_arn)
    data = (json.dumps({"topic_arns": sorted(topics)}, indent=2) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _under_rate_limit() -> bool:
    now = time.monotonic()
    with _RATE_LOCK:
        while _REQUEST_TIMES and now - _REQUEST_TIMES[0] >= RATE_WINDOW_SECONDS:
            _REQUEST_TIMES.popleft()
        if len(_REQUEST_TIMES) >= RATE_LIMIT:
            return False
        _REQUEST_TIMES.append(now)
        return True


class _LoopbackHTTPServer(HTTPServer):
    """Single-request-at-a-time server bounds concurrent sockets and workers."""

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(REQUEST_TIMEOUT_SECONDS)
        return request, address

    def handle_error(self, request, client_address) -> None:
        # Suppress BaseServer's traceback, which can include request context.
        return


def make_server(
    inbox,
    *,
    expected_topic_arn: str | None,
    bootstrap_only: bool = False,
    discovered_topics_path: Path | None = None,
    port: int = 8787,
    certificate_loader: Callable[[str], bytes] | None = None,
    datetime_now: Callable[[], datetime] | None = None,
) -> HTTPServer:
    """Create a receiver on IPv4 loopback.

    ``datetime_now`` may be a zero-argument callable returning the current
    datetime; when omitted, the verifier uses its production UTC clock.
    """
    normal_configuration = (
        bootstrap_only is False
        and isinstance(expected_topic_arn, str)
        and bool(_TOPIC_ARN.fullmatch(expected_topic_arn))
        and discovered_topics_path is None
    )
    bootstrap_configuration = (
        bootstrap_only is True
        and expected_topic_arn is None
        and isinstance(discovered_topics_path, Path)
        and discovered_topics_path.is_absolute()
    )
    if not (normal_configuration or bootstrap_configuration):
        raise ValueError("invalid configuration")
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("invalid configuration")

    class Handler(BaseHTTPRequestHandler):
        server_version = "FinanceControlSNS"
        sys_version = ""

        def log_message(self, _format: str, *_args) -> None:
            # Request paths and client supplied values are deliberately omitted.
            return

        def send_error(self, code, message=None, explain=None):
            # BaseHTTPRequestHandler can otherwise echo the request line.
            if code == 501:
                code = 405
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()

        def _respond(self, status: int, payload: dict | None = None) -> None:
            body = b"" if payload is None else json.dumps(payload, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _method_not_allowed(self) -> None:
            self._respond(405)

        def do_GET(self):
            self._method_not_allowed()

        do_HEAD = do_GET
        do_PUT = do_GET
        do_PATCH = do_GET
        do_DELETE = do_GET
        do_OPTIONS = do_GET
        do_CONNECT = do_GET
        do_TRACE = do_GET

        def do_POST(self):
            if self.path != "/amazon/sns":
                self._respond(404)
                return
            if self.headers.get_all("Expect"):
                self._respond(417)
                return
            if self.headers.get_all("Transfer-Encoding"):
                self._respond(400)
                return

            lengths = self.headers.get_all("Content-Length") or []
            if not lengths:
                self._respond(411)
                return
            if len(lengths) != 1 or not _CONTENT_LENGTH.fullmatch(lengths[0].strip()):
                self._respond(400)
                return
            raw_length = lengths[0].strip()
            if len(raw_length) > 5:
                self._respond(400)
                return
            length = int(raw_length)
            if length > MAX_BODY_BYTES:
                self._respond(413)
                return

            content_types = self.headers.get_all("Content-Type") or []
            if len(content_types) != 1 or not self._valid_content_type(content_types[0]):
                self._respond(415)
                return
            if not _under_rate_limit():
                self._respond(429)
                return
            try:
                body = self.rfile.read(length)
                if len(body) != length:
                    self._respond(400)
                    return
                verified = verify_sns_message(
                    body,
                    expected_topic_arn=expected_topic_arn,
                    certificate_loader=certificate_loader,
                    now=datetime_now() if datetime_now is not None else None,
                    replay_window=timedelta(minutes=65),
                    max_body_bytes=MAX_BODY_BYTES,
                )
            except SnsVerificationError as exc:
                code = str(exc)
                status = 503 if code in {"certificate_fetch_failed", "cryptography_unavailable"} else 403
                self._respond(status)
                return
            except (TimeoutError, OSError):
                self._respond(400)
                return
            except Exception:
                self._respond(500)
                return

            if not self._headers_match(verified):
                self._respond(400)
                return
            if bootstrap_only and verified.message_type != "SubscriptionConfirmation":
                self._respond(403)
                return
            if verified.message_type == "Notification":
                notification = json.dumps({
                    "Type": "Notification",
                    "Subject": verified.subject,
                    "Message": verified.message,
                }, separators=(",", ":")).encode("utf-8")
                try:
                    parse_response_notification(notification)
                except PortabilityApiError:
                    self._respond(400)
                    return
            try:
                if bootstrap_only:
                    _record_discovered_topic(discovered_topics_path, verified.topic_arn)
                inserted = inbox.record(verified)
                if type(inserted) is not bool:
                    self._respond(500)
                    return
            except InboxError as exc:
                self._respond(409 if exc.code == "message_id_conflict" else 503)
                return
            except Exception:
                self._respond(503)
                return
            self._respond(200, {"accepted": True})

        @staticmethod
        def _valid_content_type(value: str) -> bool:
            parts = [part.strip() for part in value.split(";")]
            if parts[0].lower() not in {"text/plain", "application/json"}:
                return False
            return len(parts) == 1 or (
                len(parts) == 2 and parts[1].lower() in {"charset=utf-8", 'charset="utf-8"'}
            )

        def _headers_match(self, message: VerifiedSnsMessage) -> bool:
            expected = {
                "x-amz-sns-message-type": message.message_type,
                "x-amz-sns-message-id": message.message_id,
                "x-amz-sns-topic-arn": message.topic_arn,
            }
            for name, signed_value in expected.items():
                values = self.headers.get_all(name) or []
                if values and (len(values) != 1 or values[0] != signed_value):
                    return False
            return True

    server = _LoopbackHTTPServer(("127.0.0.1", port), Handler)
    server.timeout = REQUEST_TIMEOUT_SECONDS
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="finance-control-amazon-sns")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--inbox-database", required=True)
    serve.add_argument("--topic-arn", required=True)
    serve.add_argument("--port", type=int, default=8787)
    serve.add_argument("--max-messages", type=int, default=10_000)
    args = parser.parse_args(argv)
    if args.max_messages <= 0 or not 0 <= args.port <= 65535:
        print("Ungültige Konfiguration.")
        return 2
    inbox = None
    server = None
    try:
        inbox = AmazonSnsInbox(args.inbox_database, max_messages=args.max_messages)
        server = make_server(inbox, expected_topic_arn=args.topic_arn, port=args.port)
        print(f"SNS-Empfang aktiv auf http://127.0.0.1:{server.server_port}/amazon/sns (nur lokal)")
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except (InboxError, OSError, ValueError):
        print("Serverstart fehlgeschlagen.")
        return 1
    finally:
        if server is not None:
            server.server_close()
        if inbox is not None:
            inbox.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
