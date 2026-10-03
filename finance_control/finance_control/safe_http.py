"""Shared URL and redirect guards for read-only external downloads."""

from __future__ import annotations

import urllib.parse
import urllib.request
import re

_JSON_TYPE = re.compile(r"application/json(?:\s*;\s*charset\s*=\s*[^;\s]+)?", re.I)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Reject redirects so allowlisted URLs cannot change authority."""

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def validate_host_allowlist(hosts: frozenset[str]) -> frozenset[str]:
    """Validate an explicit lowercase DNS-host allowlist once at construction."""
    if not hosts or any(
        not isinstance(host, str)
        or host != host.lower()
        or not re.fullmatch(r"[a-z0-9.-]+", host)
        or host.startswith(".")
        or host.endswith(".")
        for host in hosts
    ):
        raise ValueError("invalid_host_allowlist")
    return frozenset(hosts)


def is_json_content_type(value: object) -> bool:
    return isinstance(value, str) and _JSON_TYPE.fullmatch(value.strip()) is not None


def validate_https_url(
    value: object, *, require_path: bool = True, allow_query: bool = True
) -> urllib.parse.SplitResult:
    """Apply security checks common to every externally supplied HTTPS URL."""
    if not isinstance(value, str):
        raise ValueError("invalid_url")
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("invalid_url") from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or bool(parsed.fragment)
        or (require_path and not parsed.path)
        or (not allow_query and bool(parsed.query))
    ):
        raise ValueError("invalid_url")
    return parsed
