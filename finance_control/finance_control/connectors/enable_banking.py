"""Bounded, account-information-only Enable Banking API client.

Contract: https://enablebanking.com/docs/api/reference/ (accessed 2026-10-11).
No bank credentials, payment endpoints, persistence, or transaction identity synthesis.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timezone
from time import monotonic
from typing import Callable, Mapping, Protocol
from uuid import UUID

API_BASE = "https://api.enablebanking.com"
AUTHORIZATION_HOSTS = frozenset({"auth.enablebanking.com", "tilisy.enablebanking.com"})
TIMEOUT = 15.0
MAX_BYTES = 2_000_000
MAX_PAGES = 100
MAX_TRANSACTIONS = 10_000
MAX_TOTAL_BYTES = 8_000_000
MAX_TOTAL_SECONDS = 60.0


class EnableBankingError(RuntimeError):
    """Stable secret-free error code; provider messages are never propagated."""

    def __init__(self, code: str, *, status: int | None = None) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class HttpTransport(Protocol):
    def request(self, method: str, url: str, *, headers: Mapping[str, str],
                body: bytes | None, timeout: float, max_bytes: int) -> HttpResponse: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


class UrllibTransport:
    """Default transport: no automatic redirect and bounded response reading."""

    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(_NoRedirect)

    def request(self, method: str, url: str, *, headers: Mapping[str, str],
                body: bytes | None, timeout: float, max_bytes: int) -> HttpResponse:
        request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
        try:
            response = self._opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            length = response.headers.get("Content-Length")
            if length is not None:
                try:
                    declared = int(length)
                except ValueError:
                    raise EnableBankingError("invalid_content_length") from None
                if declared < 0 or declared > max_bytes:
                    raise EnableBankingError("response_too_large")
            payload = response.read(max_bytes + 1)
            if len(payload) > max_bytes:
                raise EnableBankingError("response_too_large")
            return HttpResponse(response.status, dict(response.headers.items()), payload)


def canonical_uuid(value: object, code: str) -> str:
    """Validate a hyphenated UUID and return its canonical lower-case form."""
    try:
        if not isinstance(value, str):
            raise ValueError
        if str(UUID(value)) != value.lower():
            raise ValueError
    except (ValueError, AttributeError, TypeError):
        raise EnableBankingError(code) from None
    return value.lower()


def _date(value: str) -> str:
    try:
        if not isinstance(value, str) or len(value) != 10 or date.fromisoformat(value).isoformat() != value:
            raise ValueError
    except ValueError:
        raise EnableBankingError("invalid_date") from None
    return value


def _https_url(value: object, code: str, *, hosts: frozenset[str] | None = None) -> str:
    try:
        if not isinstance(value, str):
            raise ValueError
        if len(value) > 4096 or any(ord(char) < 33 or char == "\\" for char in value):
            raise ValueError
        parsed = urllib.parse.urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.fragment or parsed.port not in (None, 443)
                or (hosts is not None and parsed.hostname not in hosts)):
            raise ValueError
    except (ValueError, TypeError):
        raise EnableBankingError(code) from None
    return value


class EnableBankingClient:
    """Only the account-information endpoints and explicit session revocation."""

    def __init__(self, application_id: str, private_key_pem: str | bytes, *,
                 transport: HttpTransport | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.application_id = canonical_uuid(application_id, "invalid_application_id")
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import rsa
            import jwt
            pem = private_key_pem.encode("ascii") if isinstance(private_key_pem, str) else private_key_pem
            key = serialization.load_pem_private_key(pem, password=None)
            if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
                raise ValueError
        except (ImportError, ValueError, TypeError, UnicodeError):
            raise EnableBankingError("invalid_private_key") from None
        self._key = key
        self._jwt = jwt
        self._transport = transport if transport is not None else UrllibTransport()
        self._clock = clock if clock is not None else lambda: datetime.now(timezone.utc)

    def _token(self) -> str:
        try:
            now = self._clock()
            if not isinstance(now, datetime) or now.tzinfo is None:
                raise ValueError
            issued = int(now.timestamp())
            return self._jwt.encode(
                {"iss": "enablebanking.com", "aud": "api.enablebanking.com",
                 "iat": issued, "exp": issued + 300},
                self._key, algorithm="RS256", headers={"kid": self.application_id, "typ": "JWT"},
            )
        except Exception:
            raise EnableBankingError("signing_failed") from None

    def _request(self, method: str, path: str, *, query: Mapping[str, str] | None = None,
                 payload: Mapping[str, object] | None = None,
                 deadline: float | None = None,
                 byte_budget: list[int] | None = None) -> dict:
        if method not in {"GET", "POST", "DELETE"} or not path.startswith("/") or "//" in path:
            raise EnableBankingError("invalid_request")
        url = API_BASE + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {"Accept": "application/json", "Authorization": "Bearer " + self._token()}
        body = None
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        remaining = deadline - monotonic() if deadline is not None else TIMEOUT
        if remaining <= 0:
            raise EnableBankingError("time_limit")
        try:
            response = self._transport.request(method, url, headers=headers, body=body,
                                               timeout=min(TIMEOUT, remaining), max_bytes=MAX_BYTES)
        except EnableBankingError:
            raise
        except Exception:
            raise EnableBankingError("transport_failed") from None
        if deadline is not None and monotonic() >= deadline:
            raise EnableBankingError("time_limit")
        if type(response.status) is not int or not 100 <= response.status <= 599:
            raise EnableBankingError("invalid_http_status")
        if response.status != 200:
            raise EnableBankingError("http_error", status=response.status)
        try:
            if len(response.body) > MAX_BYTES:
                raise EnableBankingError("response_too_large")
            if byte_budget is not None:
                byte_budget[0] -= len(response.body)
                if byte_budget[0] < 0:
                    raise EnableBankingError("total_bytes_limit")
            content_type = next((v for k, v in response.headers.items() if k.lower() == "content-type"), "")
            if content_type.split(";", 1)[0].strip().lower() != "application/json":
                raise EnableBankingError("invalid_content_type")
            result = json.loads(response.body.decode("utf-8"))
            if not isinstance(result, dict):
                raise EnableBankingError("invalid_response")
            return result
        except EnableBankingError:
            raise
        except (AttributeError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
            raise EnableBankingError("invalid_response") from None

    def application(self) -> dict:
        return self._request("GET", "/application")

    def providers(self) -> dict:
        return self._request("GET", "/aspsps", query={"psu_type": "personal", "service": "AIS"})

    def start_authorization(self, aspsp: Mapping[str, str], state: str,
                            redirect_url: str, valid_until: str) -> dict:
        if (not isinstance(aspsp, Mapping) or set(aspsp) != {"name", "country"}
                or not isinstance(aspsp.get("name"), str) or not aspsp["name"]
                or not isinstance(aspsp.get("country"), str) or len(aspsp["country"]) != 2
                or not aspsp["country"].isalpha() or aspsp["country"] != aspsp["country"].upper()):
            raise EnableBankingError("invalid_aspsp")
        if not isinstance(state, str) or not 1 <= len(state) <= 512 or any(ord(c) < 32 for c in state):
            raise EnableBankingError("invalid_state")
        _https_url(redirect_url, "invalid_redirect_url")
        try:
            if (not isinstance(valid_until, str) or len(valid_until) > 64
                    or "T" not in valid_until):
                raise ValueError
            deadline = datetime.fromisoformat(valid_until.replace("Z", "+00:00"))
            if deadline.tzinfo is None:
                raise ValueError
        except ValueError:
            raise EnableBankingError("invalid_valid_until") from None
        result = self._request("POST", "/auth", payload={
            "access": {"balances": True, "transactions": True, "valid_until": valid_until},
            "aspsp": dict(aspsp),
            "state": state, "redirect_url": redirect_url, "psu_type": "personal",
        })
        _https_url(result.get("url"), "invalid_authorization_url", hosts=AUTHORIZATION_HOSTS)
        canonical_uuid(result.get("authorization_id"), "invalid_authorization_id")
        return result

    def authorize_session(self, code: str) -> dict:
        if not isinstance(code, str) or not 1 <= len(code) <= 4096 or any(ord(c) < 32 for c in code):
            raise EnableBankingError("invalid_authorization_code")
        return self._request("POST", "/sessions", payload={"code": code})

    def session(self, session_id: str) -> dict:
        return self._request("GET", "/sessions/" + canonical_uuid(session_id, "invalid_session_id"))

    def balances(self, account_uid: str) -> dict:
        return self._request("GET", "/accounts/" + canonical_uuid(account_uid, "invalid_account_uid") + "/balances")

    def transactions(self, account_uid: str, date_from: str, date_to: str) -> dict:
        uid = canonical_uuid(account_uid, "invalid_account_uid")
        start, end = _date(date_from), _date(date_to)
        if start > end:
            raise EnableBankingError("invalid_date_range")
        path = f"/accounts/{uid}/transactions"
        query = {"date_from": start, "date_to": end}
        all_transactions: list[dict] = []
        seen: set[str] = set()
        deadline = monotonic() + MAX_TOTAL_SECONDS
        byte_budget = [MAX_TOTAL_BYTES]
        for _ in range(MAX_PAGES):
            page = self._request("GET", path, query=query,
                                 deadline=deadline, byte_budget=byte_budget)
            transactions = page.get("transactions")
            continuation = page.get("continuation_key")
            if not isinstance(transactions, list) or not all(isinstance(item, dict) for item in transactions):
                raise EnableBankingError("invalid_transactions_page")
            if len(all_transactions) + len(transactions) > MAX_TRANSACTIONS:
                raise EnableBankingError("transactions_limit")
            all_transactions.extend(transactions)
            if continuation is None:
                return {"transactions": all_transactions, "continuation_key": None}
            if (not isinstance(continuation, str) or not continuation
                    or len(continuation) > 4096 or continuation in seen):
                raise EnableBankingError("invalid_continuation_key")
            seen.add(continuation)
            query = {"date_from": start, "date_to": end, "continuation_key": continuation}
        raise EnableBankingError("pagination_limit")

    def close_session(self, session_id: str) -> dict:
        """Revoke access with DELETE /sessions/{id}; no account or ledger deletion."""
        return self._request("DELETE", "/sessions/" + canonical_uuid(session_id, "invalid_session_id"))
