"""Bounded, atomic download of Amazon Data Portability JSON record files."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import urllib.error
import urllib.request
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Protocol

from .amazon_portability_api import DEFAULT_RECORD_HOSTS, validate_record_url
from .import_preview import outside_repository
from .safe_http import NoRedirect, is_json_content_type, validate_host_allowlist

DEFAULT_MAX_BYTES = 64 * 1024 * 1024
CHUNK_BYTES = 64 * 1024
class AmazonRecordDownloadError(RuntimeError):
    """Stable code only; no URL, source bytes, private path or transport text."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class DownloadedRecord:
    path: Path = field(repr=False)
    size: int
    sha256: str


class RecordStream(Protocol):
    status: int
    headers: Mapping[str, str]

    def read(self, size: int) -> bytes: ...

    def close(self) -> None: ...


class RecordStreamTransport(Protocol):
    def open(self, url: str, *, timeout: float) -> RecordStream: ...


class UrllibRecordStreamTransport:
    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(NoRedirect)

    def open(self, url: str, *, timeout: float) -> RecordStream:
        request = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
        try:
            return self._opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            # HTTPError is a response stream; the caller rejects its status
            # before reading any provider-supplied error body.
            return error


def _header(headers: Mapping[str, str], name: str) -> str | None:
    return next((value for key, value in headers.items() if key.lower() == name.lower()), None)


class AmazonRecordDownloader:
    def __init__(
        self,
        transport: RecordStreamTransport | None = None,
        *,
        timeout: float = 30.0,
        max_bytes: int = DEFAULT_MAX_BYTES,
        record_hosts: frozenset[str] = DEFAULT_RECORD_HOSTS,
    ) -> None:
        invalid_hosts = False
        try:
            record_hosts = validate_host_allowlist(record_hosts)
        except ValueError:
            invalid_hosts = True
        if invalid_hosts:
            raise AmazonRecordDownloadError("invalid_record_hosts")
        if timeout <= 0 or type(max_bytes) is not int or max_bytes <= 0:
            raise AmazonRecordDownloadError("invalid_download_limits")
        self._transport = transport if transport is not None else UrllibRecordStreamTransport()
        self._timeout = timeout
        self._max_bytes = max_bytes
        self._record_hosts = record_hosts

    def download_json(self, url: str, destination: str | Path) -> DownloadedRecord:
        invalid_url = False
        try:
            validate_record_url(url, self._record_hosts)
        except Exception:
            invalid_url = True
        if invalid_url:
            raise AmazonRecordDownloadError("invalid_record_url")

        invalid_destination = False
        try:
            path = outside_repository(destination)
            parent = outside_repository(path.parent)
            parent.mkdir(parents=True, exist_ok=True)
            # Recheck after creation so an existing symlink cannot redirect a
            # private download into a checkout.
            path = outside_repository(parent / path.name)
        except Exception:
            invalid_destination = True
        if invalid_destination:
            raise AmazonRecordDownloadError("invalid_destination")
        if not path.name or path.is_dir():
            raise AmazonRecordDownloadError("invalid_destination")

        partial = parent / f".{path.name}.{secrets.token_hex(16)}.partial"
        download_failed = False
        try:
            with closing(self._transport.open(url, timeout=self._timeout)) as response:
                if type(response.status) is not int or response.status != 200:
                    raise AmazonRecordDownloadError("unexpected_http_status")
                content_type = _header(response.headers, "Content-Type")
                if not is_json_content_type(content_type):
                    raise AmazonRecordDownloadError("invalid_content_type")
                length = _header(response.headers, "Content-Length")
                if length is not None:
                    if not isinstance(length, str) or not re.fullmatch(r"[0-9]+", length.strip()):
                        raise AmazonRecordDownloadError("invalid_content_length")
                    expected = int(length.strip())
                    if expected > self._max_bytes:
                        raise AmazonRecordDownloadError("response_too_large")
                else:
                    expected = None
                size = 0
                digest = hashlib.sha256()
                with partial.open("xb") as output:
                    while True:
                        chunk = response.read(min(CHUNK_BYTES, self._max_bytes - size + 1))
                        if not isinstance(chunk, bytes):
                            raise AmazonRecordDownloadError("invalid_response_stream")
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > self._max_bytes:
                            raise AmazonRecordDownloadError("response_too_large")
                        output.write(chunk)
                        digest.update(chunk)
                    if expected is not None and size != expected:
                        raise AmazonRecordDownloadError("content_length_mismatch")
                    output.flush()
                    os.fsync(output.fileno())
            os.replace(partial, path)
            result = DownloadedRecord(path, size, digest.hexdigest())
        except AmazonRecordDownloadError:
            raise
        except Exception:
            download_failed = True
        finally:
            try:
                partial.unlink(missing_ok=True)
            except OSError:
                pass
        if download_failed:
            raise AmazonRecordDownloadError("download_failed")
        return result
