"""Alexa HTTPS request verification and narrow MA stream capabilities.

Implements Amazon's documented Signature-256, certificate-chain and timestamp
checks without the prototype's simulator bypass or legacy oscrypto dependency.
No request body, URL, signature or capability is written to a log.
"""
from __future__ import annotations

import base64
import asyncio
import hashlib
import posixpath
import re
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import aiohttp
import certifi
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from OpenSSL import crypto


class Rejected(ValueError):
    """Deliberately contains only a generic, non-sensitive message."""


def origin(value: str, *, https: bool = False) -> str:
    """Accept an explicitly configured origin, never a caller-controlled URL."""
    try:
        parsed = urlsplit(value)
        allowed = {"https"} if https else {"http"}
        if (parsed.scheme not in allowed or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in ("", "/") or any(c.isspace() for c in value)):
            raise Rejected("Invalid origin")
        port = parsed.port
        if https and port not in (None, 443):
            raise Rejected("Invalid origin")
        return urlunsplit((parsed.scheme, parsed.netloc.lower(), "", "", ""))
    except (TypeError, ValueError):
        raise Rejected("Invalid origin") from None


def certificate_url(value: str) -> str:
    """Normalize Amazon's certificate URL before enforcing the allowlist."""
    try:
        parsed = urlsplit(value)
        if (parsed.scheme.lower() != "https" or parsed.hostname.lower() != "s3.amazonaws.com"
                or parsed.port not in (None, 443) or parsed.username or parsed.password
                or parsed.query or len(value) > 1024):
            raise Rejected("Invalid certificate URL")
        path = posixpath.normpath("/" + unquote(parsed.path).lstrip("/"))
        if not path.startswith("/echo.api/") or "\\" in path or any(ord(c) < 32 for c in path):
            raise Rejected("Invalid certificate URL")
        return "https://s3.amazonaws.com" + quote(path, safe="/-_.~")
    except (AttributeError, TypeError, ValueError):
        raise Rejected("Invalid certificate URL") from None


def validate_chain(pem: bytes, now: datetime, *, ca_file: str | None = None) -> rsa.RSAPublicKey:
    """Verify trust, validity and exact Alexa signing SAN before accepting a key."""
    try:
        certs = x509.load_pem_x509_certificates(pem)
        if not certs or len(certs) > 8:
            raise Rejected("Invalid signing certificate")
        leaf = certs[0]
        if not leaf.not_valid_before_utc <= now <= leaf.not_valid_after_utc:
            raise Rejected("Invalid signing certificate")
        sans = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        if "echo-api.amazon.com" not in sans.get_values_for_type(x509.DNSName):
            raise Rejected("Invalid signing certificate")
        trust = crypto.X509Store()
        trust.load_locations(ca_file or certifi.where())
        trust.set_time(now)
        crypto.X509StoreContext(trust, crypto.X509.from_cryptography(leaf),
                               [crypto.X509.from_cryptography(c) for c in certs[1:]]).verify_certificate()
        key = leaf.public_key()
        if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 2048:
            raise Rejected("Invalid signing certificate")
        return key
    except Exception:
        raise Rejected("Invalid signing certificate") from None


class AlexaVerifier:
    def __init__(self, session: aiohttp.ClientSession, skill_id: str):
        self.session, self.skill_id = session, skill_id
        self.cache: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
        self.fetch_slots = asyncio.Semaphore(2)

    async def verify(self, headers, body: bytes, payload: dict) -> None:
        """Check identity and freshness cheaply, then the exact raw-body signature."""
        try:
            if any(key.lower().startswith("x-simulator-") for key in headers):
                raise Rejected("Invalid Alexa request")
            system = payload["context"]["System"]
            if system["application"]["applicationId"] != self.skill_id:
                raise Rejected("Invalid Alexa request")
            stamp = datetime.fromisoformat(payload["request"]["timestamp"].replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            if stamp.tzinfo is None or abs((now - stamp).total_seconds()) > 150:
                raise Rejected("Invalid Alexa request")
            cert_url = certificate_url(headers.get("SignatureCertChainUrl", ""))
            signature = base64.b64decode(headers.get("Signature-256", ""), validate=True)
            if not signature or len(signature) > 1024:
                raise Rejected("Invalid Alexa request")
            cached = self.cache.get(cert_url)
            if cached and cached[0] > time.monotonic():
                pem = cached[1]
            else:
                async with self.fetch_slots:
                    async with self.session.get(cert_url, allow_redirects=False,
                                                headers={"Accept-Encoding": "identity"},
                                                timeout=aiohttp.ClientTimeout(total=5)) as response:
                        if response.status != 200 or response.headers.get("Content-Encoding", "identity") != "identity":
                            raise Rejected("Invalid signing certificate")
                        chunks, length = [], 0
                        async for chunk in response.content.iter_chunked(8192):
                            length += len(chunk)
                            if length > 65536:
                                raise Rejected("Invalid signing certificate")
                            chunks.append(chunk)
                        pem = b"".join(chunks)
                self.cache[cert_url] = (time.monotonic() + 900, pem)
                while len(self.cache) > 8:
                    self.cache.popitem(last=False)
            key = validate_chain(pem, now)
            key.verify(signature, body, padding.PKCS1v15(), hashes.SHA256())
        except Exception:
            raise Rejected("Invalid Alexa request") from None


@dataclass(frozen=True)
class StreamTarget:
    path: str
    player_id: str
    queue_id: str


def stream_target(value: str, fixed_origin: str, allowed_players: set[str]) -> StreamTarget:
    """Only MA queue MP3 routes: no source, command, query or alternate origin."""
    try:
        parsed = urlsplit(value)
        if (urlunsplit((parsed.scheme, parsed.netloc.lower(), "", "", "")) != fixed_origin
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or len(value) > 4096 or re.search(r"%(?![0-9a-fA-F]{2})", parsed.path)):
            raise Rejected("Invalid stream")
        path = unquote(parsed.path, errors="strict")
        parts = path.split("/")
        if (len(parts) != 6 or parts[0] or parts[1] not in {"flow", "single"}
                or any(not p or p in {".", ".."} or "\\" in p
                       or any(ord(c) < 32 for c in p) for p in parts[2:])
                or not parts[5].endswith(".mp3") or "%" in path):
            raise Rejected("Invalid stream")
        player_id = parts[5][:-4]
        if player_id not in allowed_players or parts[3] not in allowed_players:
            raise Rejected("Invalid stream")
        # MA Alexa uses its own active queue, not another configured room's queue.
        if parts[3] != player_id:
            raise Rejected("Invalid stream")
        return StreamTarget(parsed.path, player_id, parts[3])
    except (TypeError, UnicodeError, ValueError):
        raise Rejected("Invalid stream") from None


@dataclass(frozen=True)
class Grant:
    target: StreamTarget
    device_id: str
    expires: float


class Grants:
    def __init__(self, ttl: int, *, clock=time.monotonic):
        self.ttl, self.clock = ttl, clock
        self.items: dict[str, Grant] = {}

    def issue(self, target: StreamTarget, device_id: str) -> str:
        self.items = {key: grant for key, grant in self.items.items() if grant.expires > self.clock()}
        if len(self.items) >= 256:
            raise Rejected("Grant limit reached")
        token = secrets.token_urlsafe(32)
        self.items[hashlib.sha256(token.encode()).hexdigest()] = Grant(target, device_id, self.clock() + self.ttl)
        return token

    def get(self, token: str) -> Grant:
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            raise Rejected("Invalid grant")
        key = hashlib.sha256(token.encode()).hexdigest()
        grant = self.items.get(key)
        if not grant or grant.expires <= self.clock():
            self.items.pop(key, None)
            raise Rejected("Invalid grant")
        return grant
