"""Read-only Amazon Data Portability API transport for the European region.

This module does not persist credentials, receive webhooks, or fetch record files.
The caller must verify the authenticity of an SNS notification before trusting it.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Mapping, Protocol
from uuid import UUID

from .safe_http import (
    NoRedirect, is_json_content_type, validate_host_allowlist, validate_https_url,
)

TOKEN_URL = "https://api.amazon.co.uk/auth/o2/token"
DATA_URL = "https://intake.eu-west-1.portability.data.amazon"
ALLOWED_SCOPES = frozenset(
    {"portability-physical-orders", "portability-physical-order-returns"}
)
DEFAULT_RECORD_HOSTS = frozenset(
    {"s3.eu-west-1.amazonaws.com", "s3.amazonaws.com"}
)
class PortabilityApiError(RuntimeError):
    """Error with a stable code and no provider-supplied or credential text."""

    def __init__(self, code: str, *, status: int | None = None) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str] = field(repr=False)
    body: bytes = field(repr=False)


class HttpTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
        max_bytes: int,
    ) -> HttpResponse: ...


class UrllibTransport:
    """Bounded stdlib transport; redirects cannot carry credentials elsewhere."""

    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(NoRedirect)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
        max_bytes: int,
    ) -> HttpResponse:
        request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
        try:
            response = self._opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            length = response.headers.get("Content-Length")
            if length is not None:
                try:
                    if int(length) > max_bytes:
                        raise PortabilityApiError("response_too_large")
                except ValueError:
                    raise PortabilityApiError("invalid_content_length") from None
            payload = response.read(max_bytes + 1)
            if len(payload) > max_bytes:
                raise PortabilityApiError("response_too_large")
            return HttpResponse(response.status, dict(response.headers.items()), payload)


@dataclass(frozen=True)
class AccessToken:
    value: str = field(repr=False)
    expires_in: int

    def __reduce__(self):
        raise TypeError("Amazon-Zugangstoken darf nicht serialisiert werden.")


@dataclass(frozen=True)
class QueryRecord:
    schema_url: str = field(repr=False)
    file_url: str = field(repr=False)


@dataclass(frozen=True)
class RecordPage:
    records: tuple[QueryRecord, ...] = field(repr=False)
    next_page_token: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class ResponseNotification:
    query_id: str
    status: str


def _uuid(value: str) -> str:
    if not isinstance(value, str):
        raise PortabilityApiError("invalid_query_id")
    invalid = False
    try:
        UUID(value)
    except (ValueError, AttributeError):
        invalid = True
    if invalid:
        raise PortabilityApiError("invalid_query_id")
    return value


def _object(payload: bytes, code: str) -> dict:
    invalid = False
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        invalid = True
    if invalid:
        raise PortabilityApiError(code)
    if not isinstance(value, dict):
        raise PortabilityApiError(code)
    return value


def validate_record_url(value: object, hosts: frozenset[str]) -> str:
    invalid = False
    try:
        parsed = validate_https_url(value)
        host = parsed.hostname
    except ValueError:
        invalid = True
    if invalid:
        raise PortabilityApiError("invalid_record_url")
    if (
        not (
            host in hosts
            or (
                "s3.eu-west-1.amazonaws.com" in hosts
                and host.endswith(".s3.eu-west-1.amazonaws.com")
            )
        )
    ):
        raise PortabilityApiError("invalid_record_url")
    return str(value)


class AmazonPortabilityApi:
    """Explicit, side-effect-free operations except the requested HTTP calls."""

    def __init__(
        self,
        transport: HttpTransport | None = None,
        *,
        timeout: float = 15.0,
        max_response_bytes: int = 1_048_576,
        record_hosts: frozenset[str] = DEFAULT_RECORD_HOSTS,
    ) -> None:
        if timeout <= 0 or max_response_bytes <= 0:
            raise PortabilityApiError("invalid_transport_limits")
        invalid_hosts = False
        try:
            record_hosts = validate_host_allowlist(record_hosts)
        except ValueError:
            invalid_hosts = True
        if invalid_hosts:
            raise PortabilityApiError("invalid_record_hosts")
        self._transport = transport if transport is not None else UrllibTransport()
        self._timeout = timeout
        self._max_response_bytes = max_response_bytes
        self._record_hosts = record_hosts

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        expected_status: int,
    ) -> dict:
        transport_failed = False
        try:
            response = self._transport.request(
                method, url, headers=headers, body=body,
                timeout=self._timeout, max_bytes=self._max_response_bytes,
            )
        except PortabilityApiError:
            raise
        except Exception:
            transport_failed = True
        if transport_failed:
            # Raise outside the handler so the original exception is not
            # retained as __context__ on the application-facing exception.
            raise PortabilityApiError("transport_failed")
        if type(response.status) is not int or not 100 <= response.status <= 599:
            raise PortabilityApiError("invalid_http_status")
        if response.status != expected_status:
            raise PortabilityApiError("unexpected_http_status", status=response.status)
        content_type = next(
            (value for key, value in response.headers.items() if key.lower() == "content-type"),
            "",
        )
        if not is_json_content_type(content_type):
            raise PortabilityApiError("invalid_content_type")
        if len(response.body) > self._max_response_bytes:
            raise PortabilityApiError("response_too_large")
        return _object(response.body, "invalid_json_response")

    def refresh_access_token(
        self, *, client_id: str, client_secret: str, refresh_token: str
    ) -> AccessToken:
        if not all(isinstance(v, str) and v for v in (client_id, client_secret, refresh_token)):
            raise PortabilityApiError("missing_credentials")
        body = urllib.parse.urlencode({
            "grant_type": "refresh_token", "refresh_token": refresh_token,
            "client_id": client_id, "client_secret": client_secret,
        }).encode("ascii")
        data = self._request_json(
            "POST", TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            body=body, expected_status=200,
        )
        token = data.get("access_token")
        expires = data.get("expires_in")
        if (
            not isinstance(token, str) or not token
            or str(data.get("token_type", "")).lower() != "bearer"
            or type(expires) is not int or expires <= 0
        ):
            raise PortabilityApiError("invalid_token_response")
        return AccessToken(token, expires)

    def create_query(self, scope_id: str, *, access_token: str) -> str:
        self._validate_scope_token(scope_id, access_token)
        data = self._request_json(
            "POST", f"{DATA_URL}/{scope_id}/data-queries",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            body=None, expected_status=201,
        )
        return _uuid(data.get("id"))

    def list_query_records(
        self,
        scope_id: str,
        query_id: str,
        *,
        access_token: str,
        max_results: int = 250,
        next_page_token: str | None = None,
    ) -> RecordPage:
        self._validate_scope_token(scope_id, access_token)
        _uuid(query_id)
        if type(max_results) is not int or not 1 <= max_results <= 250:
            raise PortabilityApiError("invalid_max_results")
        if next_page_token is not None and (not isinstance(next_page_token, str) or not next_page_token):
            raise PortabilityApiError("invalid_page_token")
        params = {"maxResults": str(max_results)}
        if next_page_token is not None:
            params["nextPageToken"] = next_page_token
        url = f"{DATA_URL}/{scope_id}/data-queries/{query_id}/records?{urllib.parse.urlencode(params)}"
        data = self._request_json(
            "GET", url,
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            body=None, expected_status=200,
        )
        records = data.get("records")
        if not isinstance(records, list):
            raise PortabilityApiError("invalid_records_response")
        result = []
        for record in records:
            if not isinstance(record, dict):
                raise PortabilityApiError("invalid_records_response")
            result.append(QueryRecord(
                validate_record_url(record.get("schema"), self._record_hosts),
                validate_record_url(record.get("file"), self._record_hosts),
            ))
        next_token = data.get("nextPageToken")
        if next_token is not None and (not isinstance(next_token, str) or not next_token):
            raise PortabilityApiError("invalid_records_response")
        return RecordPage(tuple(result), next_token)

    @staticmethod
    def _validate_scope_token(scope_id: str, access_token: str) -> None:
        if scope_id not in ALLOWED_SCOPES:
            raise PortabilityApiError("scope_not_allowed")
        if not isinstance(access_token, str) or not access_token or any(
            char.isspace() for char in access_token
        ):
            raise PortabilityApiError("invalid_access_token")


def parse_response_notification(payload: bytes) -> ResponseNotification:
    """Parse SNS-shaped JSON only; signature verification belongs to the receiver."""
    if not isinstance(payload, bytes) or len(payload) > 1024:
        raise PortabilityApiError("invalid_notification")
    envelope = _object(payload, "invalid_notification")
    if (
        envelope.get("Type") != "Notification"
        or envelope.get("Subject") != "Data Portability Notification 1.0"
        or not isinstance(envelope.get("Message"), str)
    ):
        raise PortabilityApiError("invalid_notification")
    message = _object(envelope["Message"].encode("utf-8"), "invalid_notification")
    if message.get("version") != "1.0" or message.get("status") not in {"COMPLETED", "CANCELED"}:
        raise PortabilityApiError("invalid_notification")
    try:
        query_id = _uuid(message.get("id"))
    except PortabilityApiError:
        raise PortabilityApiError("invalid_notification") from None
    return ResponseNotification(query_id, message["status"])
