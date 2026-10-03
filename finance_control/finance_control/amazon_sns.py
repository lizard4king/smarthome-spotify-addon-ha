"""Verify AWS SNS envelopes before Amazon portability data is trusted."""

from __future__ import annotations

import base64
import binascii
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Mapping

from .safe_http import NoRedirect, validate_https_url


class SnsVerificationError(RuntimeError):
    """Redacted validation failure for an untrusted SNS request."""


@dataclass(frozen=True)
class VerifiedSnsMessage:
    message_type: str
    message_id: str
    topic_arn: str
    timestamp: datetime
    message: str
    subject: str | None = None
    subscribe_url: str | None = None
    token: str | None = None


_CERT_PATH = re.compile(r"^/SimpleNotificationService-[A-Za-z0-9_-]+\.pem$")
SNS_TOPIC_ARN_PATTERN = re.compile(
    r"^arn:aws(?:-cn|-us-gov)?:sns:[a-z0-9-]+:\d{12}:[A-Za-z0-9_-]{1,256}$"
)
_FIELDS = {
    "Notification": ("Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type"),
    "SubscriptionConfirmation": (
        "Message", "MessageId", "SubscribeURL", "Timestamp", "Token", "TopicArn", "Type"
    ),
}


def _safe_https_url(value: str, *, certificate: bool = False) -> str:
    try:
        parsed = validate_https_url(value, require_path=certificate, allow_query=not certificate)
    except ValueError:
        raise SnsVerificationError("invalid_url") from None
    host = (parsed.hostname or "").lower()
    valid_host = bool(re.fullmatch(r"sns\.[a-z0-9-]+\.amazonaws\.com(?:\.cn)?", host))
    if not valid_host:
        raise SnsVerificationError("invalid_url")
    if certificate and not _CERT_PATH.fullmatch(parsed.path):
        raise SnsVerificationError("invalid_certificate_url")
    return value


def _text_fields(value: object) -> Mapping[str, str]:
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not isinstance(item, str) for key, item in value.items()
    ):
        raise SnsVerificationError("invalid_envelope")
    return value


def _canonical(envelope: Mapping[str, str], message_type: str) -> bytes:
    parts: list[str] = []
    for name in _FIELDS[message_type]:
        if name == "Subject" and name not in envelope:
            continue
        if name not in envelope:
            raise SnsVerificationError("missing_field")
        parts.extend((name, "\n", envelope[name], "\n"))
    return "".join(parts).encode("utf-8")


def _load_certificate(url: str, *, max_bytes: int, timeout: float = 10.0) -> bytes:
    opener = urllib.request.build_opener(NoRedirect)
    request = urllib.request.Request(url, headers={"Accept": "application/x-pem-file"})
    try:
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        if response.status != 200:
            raise SnsVerificationError("certificate_fetch_failed")
        length = response.headers.get("Content-Length")
        if length is not None:
            try:
                if int(length) > max_bytes:
                    raise SnsVerificationError("invalid_certificate")
            except ValueError:
                raise SnsVerificationError("invalid_certificate") from None
        value = response.read(max_bytes + 1)
    return value


def verify_sns_message(
    body: bytes,
    *,
    expected_topic_arn: str | None,
    certificate_loader: Callable[[str], bytes] | None = None,
    now: datetime | None = None,
    replay_window: timedelta = timedelta(minutes=15),
    max_body_bytes: int = 16_384,
    max_certificate_bytes: int = 65_536,
) -> VerifiedSnsMessage:
    """Validate and authenticate one SNS envelope without executing contained URLs.

    A custom ``certificate_loader`` is an offline test seam. Production uses the
    bounded, redirect-free HTTPS loader above.
    """
    if (
        expected_topic_arn is not None
        and (
            not isinstance(expected_topic_arn, str)
            or not SNS_TOPIC_ARN_PATTERN.fullmatch(expected_topic_arn)
        )
    ) or replay_window <= timedelta(0) or max_body_bytes <= 0:
        raise SnsVerificationError("invalid_configuration")
    if not isinstance(body, bytes) or len(body) > max_body_bytes:
        raise SnsVerificationError("invalid_envelope")
    try:
        envelope = _text_fields(json.loads(body.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise SnsVerificationError("invalid_envelope") from None
    message_type = envelope.get("Type", "")
    if message_type not in _FIELDS:
        raise SnsVerificationError("unsupported_message_type")
    topic_arn = envelope.get("TopicArn", "")
    if not SNS_TOPIC_ARN_PATTERN.fullmatch(topic_arn):
        raise SnsVerificationError("invalid_topic")
    if expected_topic_arn is not None and topic_arn != expected_topic_arn:
        raise SnsVerificationError("unexpected_topic")
    try:
        timestamp = datetime.fromisoformat(envelope["Timestamp"].replace("Z", "+00:00"))
    except (KeyError, ValueError):
        raise SnsVerificationError("invalid_timestamp") from None
    if timestamp.tzinfo is None:
        raise SnsVerificationError("invalid_timestamp")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or abs(current - timestamp.astimezone(timezone.utc)) > replay_window:
        raise SnsVerificationError("timestamp_outside_window")
    version = envelope.get("SignatureVersion")
    if version not in {"1", "2"}:
        raise SnsVerificationError("unsupported_signature_version")
    cert_url = _safe_https_url(envelope.get("SigningCertURL", ""), certificate=True)
    if message_type == "SubscriptionConfirmation":
        _safe_https_url(envelope.get("SubscribeURL", ""))
    try:
        signature = base64.b64decode(envelope["Signature"], validate=True)
    except (KeyError, ValueError, binascii.Error):
        raise SnsVerificationError("invalid_signature") from None
    try:
        certificate_pem = (
            certificate_loader(cert_url) if certificate_loader is not None
            else _load_certificate(cert_url, max_bytes=max_certificate_bytes)
        )
    except Exception:
        raise SnsVerificationError("certificate_fetch_failed") from None
    if not isinstance(certificate_pem, bytes) or not 0 < len(certificate_pem) <= max_certificate_bytes:
        raise SnsVerificationError("invalid_certificate")
    try:
        from cryptography import x509
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding, rsa

        certificate = x509.load_pem_x509_certificate(certificate_pem)
        public_key = certificate.public_key()
        if not isinstance(public_key, rsa.RSAPublicKey):
            raise SnsVerificationError("invalid_certificate")
        instant = current.astimezone(timezone.utc)
        if not certificate.not_valid_before_utc <= instant <= certificate.not_valid_after_utc:
            raise SnsVerificationError("invalid_certificate")
        digest = hashes.SHA1() if version == "1" else hashes.SHA256()
        public_key.verify(signature, _canonical(envelope, message_type), padding.PKCS1v15(), digest)
    except ImportError:
        raise SnsVerificationError("cryptography_unavailable") from None
    except InvalidSignature:
        raise SnsVerificationError("invalid_signature") from None
    except (TypeError, ValueError):
        raise SnsVerificationError("invalid_certificate") from None
    return VerifiedSnsMessage(
        message_type=message_type,
        message_id=envelope["MessageId"],
        topic_arn=envelope["TopicArn"],
        timestamp=timestamp,
        message=envelope["Message"],
        subject=envelope.get("Subject"),
        subscribe_url=envelope.get("SubscribeURL"),
        token=envelope.get("Token"),
    )
