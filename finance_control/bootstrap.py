"""One-time, stdin-only restore of a verified Finance Control backup."""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import os
import re
import select
import shutil
import socket
import ssl
import sys
import tempfile
import time
import urllib.parse
import zipfile
from pathlib import Path, PurePosixPath

from finance_control.backup_package import verify_backup_package

DATA_DIR = Path("/data/FinanceControl/data")
MAX_INPUT = 128 * 1024
MAX_ARCHIVE = 512 * 1024 * 1024
MAX_EXPANDED = 1024 * 1024 * 1024
MAX_MEMBERS = 4096
TOTAL_TIMEOUT = 180
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,}\Z")
ALLOWED_MEMBERS = re.compile(
    r"(?:database\.sqlite|reporting-history\.json|"
    r"sources/[0-9a-f]{32}/(?:source\.xlsx|stage\.json|decision-[0-9a-f]{16}\.json)|"
    r"documents/[1-9][0-9]*\.txt)\Z"
)


class BootstrapError(Exception):
    """Safe-to-report generic bootstrap failure."""


def _read_request(stream, *, timeout: float = 180, wait_fn=select.select,
                  read_fn=os.read) -> dict[str, str]:
    raw = bytearray()
    deadline = time.monotonic() + timeout
    try:
        descriptor = stream.fileno()
    except (AttributeError, OSError):
        descriptor = None
    if descriptor is None:
        line = stream.readline(MAX_INPUT + 1)
        if not line.endswith(b"\n"):
            raise BootstrapError("invalid request")
        raw.extend(line)
    else:
        while len(raw) <= MAX_INPUT and (not raw or raw[-1] != 0x0A):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not wait_fn([descriptor], [], [], remaining)[0]:
                raise BootstrapError("request timeout")
            chunk = read_fn(descriptor, 1)
            if not chunk:
                raise BootstrapError("invalid request")
            raw.extend(chunk)
    if not raw or len(raw) > MAX_INPUT or not raw.endswith(b"\n"):
        raise BootstrapError("invalid request")

    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate key")
            value[key] = item
        return value

    try:
        request = json.loads(raw, object_pairs_hook=unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise BootstrapError("invalid request") from error
    if (not isinstance(request, dict)
            or set(request) != {"action", "backup_url", "capability", "ca_pem", "expected_sha256"}
            or request.get("action") != "restore"
            or not all(isinstance(value, str) for value in request.values())):
        raise BootstrapError("invalid request")
    return request


def _validated_url(value: str) -> tuple[str, int, str, str]:
    try:
        parsed = urllib.parse.urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port if parsed.port is not None else 443
    except ValueError as error:
        raise BootstrapError("invalid request") from error
    if (parsed.scheme != "https" or not hostname or parsed.username or parsed.password
            or not 1 <= port <= 65535 or parsed.path != "/backup" or parsed.query
            or parsed.fragment
            or "\r" in value or "\n" in value):
        raise BootstrapError("invalid request")
    try:
        addresses = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        ips = sorted({item[4][0] for item in addresses})
        if not ips or any(
            ipaddress.ip_address(address).is_loopback
            or ipaddress.ip_address(address).is_link_local
            or ipaddress.ip_address(address).is_unspecified
            or ipaddress.ip_address(address).is_multicast
            for address in ips
        ):
            raise ValueError("restricted address")
    except (OSError, ValueError) as error:
        raise BootstrapError("invalid request") from error
    return hostname, port, parsed.path, ips[0]


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, address: str, context: ssl.SSLContext):
        super().__init__(host, port, timeout=10, context=context)
        self.address = address

    def connect(self):
        raw = socket.create_connection((self.address, self.port), timeout=10)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


def _download(request: dict[str, str], destination: Path) -> None:
    hostname, port, path, address = _validated_url(request["backup_url"])
    capability = request["capability"]
    ca_pem = request["ca_pem"]
    if (not TOKEN_PATTERN.fullmatch(capability)
            or "-----BEGIN PRIVATE KEY-----" in ca_pem
            or "-----BEGIN CERTIFICATE-----" not in ca_pem
            or len(ca_pem.encode("utf-8")) > 64 * 1024
            or not re.fullmatch(r"[0-9a-fA-F]{64}", request["expected_sha256"])):
        raise BootstrapError("invalid request")
    try:
        context = ssl.create_default_context(cadata=ca_pem)
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        connection = _PinnedHTTPSConnection(hostname, port, address, context)
        started = time.monotonic()
        connection.request("GET", "/" + capability + path)
        response = connection.getresponse()
        if response.status != 200:
            raise BootstrapError("download failed")
        length = response.getheader("Content-Length")
        if length is not None and (not length.isdecimal() or int(length) > MAX_ARCHIVE):
            raise BootstrapError("download failed")
        digest = hashlib.sha256()
        total = 0
        with destination.open("xb") as output:
            while True:
                if time.monotonic() - started > TOTAL_TIMEOUT:
                    raise BootstrapError("download failed")
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_ARCHIVE:
                    raise BootstrapError("download failed")
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if (length is not None and total != int(length)
                or digest.hexdigest().lower() != request["expected_sha256"].lower()):
            raise BootstrapError("download verification failed")
    except BootstrapError:
        raise
    except Exception as error:
        raise BootstrapError("download failed") from error
    finally:
        if "connection" in locals():
            connection.close()


def _target_for_member(name: str, data_dir: Path) -> Path:
    if not ALLOWED_MEMBERS.fullmatch(name):
        raise BootstrapError("backup contents rejected")
    parts = PurePosixPath(name).parts
    if name == "database.sqlite":
        relative = Path("finance.sqlite")
    elif name == "reporting-history.json":
        relative = Path("reporting-history.json")
    elif parts[0] == "sources":
        relative = Path("finance-imports", *parts[1:])
    else:
        relative = Path("finance-documents", *parts[1:])
    target = data_dir / relative
    if not target.resolve(strict=False).is_relative_to(data_dir.resolve()):
        raise BootstrapError("backup contents rejected")
    return target


def _prepare_existing(data_dir: Path) -> None:
    if data_dir.is_symlink() or (data_dir.exists() and not data_dir.is_dir()):
        raise BootstrapError("existing data rejected")
    if not data_dir.exists():
        return
    for child in data_dir.iterdir():
        if child.name == "profile.json" and child.is_file() and not child.is_symlink():
            continue
        raise BootstrapError("existing data rejected")


def _recover_interrupted_swap(data_dir: Path) -> None:
    previous = data_dir.with_name(data_dir.name + ".restore-previous")
    if not previous.exists() and not previous.is_symlink():
        return
    if data_dir.exists() or data_dir.is_symlink():
        raise BootstrapError("existing restore state rejected")
    _prepare_existing(previous)
    os.replace(previous, data_dir)


def _verify_archive(archive_path: Path, staging_root: Path) -> list[tuple[str, zipfile.ZipInfo]]:
    try:
        with zipfile.ZipFile(archive_path) as archive:
            infos = archive.infolist()
            if (len(infos) > MAX_MEMBERS or sum(info.file_size for info in infos) > MAX_EXPANDED
                    or any(info.file_size < 0 or info.file_size > MAX_EXPANDED for info in infos)):
                raise BootstrapError("backup contents rejected")
            members = []
            for info in infos:
                if info.filename == "manifest.json":
                    continue
                mode = info.external_attr >> 16
                if (info.is_dir() or (mode and (mode & 0o170000) == 0o120000)
                        or not ALLOWED_MEMBERS.fullmatch(info.filename)):
                    raise BootstrapError("backup contents rejected")
                _target_for_member(info.filename, staging_root)
                members.append((info.filename, info))
            if not any(name == "database.sqlite" for name, _info in members):
                raise BootstrapError("backup contents rejected")
        verify_backup_package(archive_path, staging_root)
        return members
    except BootstrapError:
        raise
    except Exception as error:
        raise BootstrapError("backup verification failed") from error


def _restore(archive_path: Path, data_dir: Path = DATA_DIR) -> None:
    data_dir = data_dir.absolute()
    _prepare_existing(data_dir)
    parent = data_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".finance-restore-", dir=parent))
    try:
        members = _verify_archive(archive_path, staging)
        try:
            with zipfile.ZipFile(archive_path) as archive:
                for name, info in members:
                    target = _target_for_member(name, staging)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info) as source, target.open("xb") as output:
                        shutil.copyfileobj(source, output, 1024 * 1024)
                        output.flush()
                        os.fsync(output.fileno())
            if data_dir.exists():
                profile = data_dir / "profile.json"
                if profile.exists():
                    os.link(profile, staging / "profile.json")
            previous = data_dir.with_name(data_dir.name + ".restore-previous")
            if previous.exists() or previous.is_symlink():
                raise BootstrapError("existing restore state rejected")
            moved_old = False
            try:
                if data_dir.exists():
                    os.replace(data_dir, previous)
                    moved_old = True
                os.replace(staging, data_dir)
            except OSError:
                if moved_old and not data_dir.exists():
                    os.replace(previous, data_dir)
                raise
            if moved_old:
                shutil.rmtree(previous)
        except BootstrapError:
            raise
        except Exception as error:
            raise BootstrapError("restore failed") from error
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def run(stdin=None, data_dir: Path = DATA_DIR, downloader=_download) -> int:
    stdin = sys.stdin.buffer if stdin is None else stdin
    database = data_dir / "finance.sqlite"
    try:
        _recover_interrupted_swap(data_dir)
    except (OSError, BootstrapError):
        print("Finance Control: vorhandener Wiederherstellungszustand muss geprüft werden.", file=sys.stderr)
        return 1
    if database.exists() or database.is_symlink():
        print("Finance Control: vorhandene Datenbank bleibt unverändert.", file=sys.stderr)
        return 1
    try:
        request = _read_request(stdin)
        with tempfile.TemporaryDirectory(prefix="finance-bootstrap-") as temporary:
            archive = Path(temporary) / "backup.zip"
            downloader(request, archive)
            if not archive.is_file() or archive.stat().st_size > MAX_ARCHIVE:
                raise BootstrapError("download failed")
            _restore(archive, data_dir)
        return 0
    except BootstrapError as error:
        print(f"Finance Control: Erstinitialisierung fehlgeschlagen ({error}).", file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001 - never expose untrusted transport or parser details
        print("Finance Control: Erstinitialisierung fehlgeschlagen.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
