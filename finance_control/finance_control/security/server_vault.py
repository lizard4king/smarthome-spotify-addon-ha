"""Linux-only local bank vault. Never put this directory in HA or Drive backups.

This protects files from other unprivileged users, not from the server account
or root: either can read the persistent master key and encrypted credentials.
Losing that key requires fresh bank enrollment; existing records are never
silently re-keyed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import sys
from contextlib import contextmanager
from pathlib import Path

from .credentials import Credentials


APPROVED_DIRECTORY = Path('/data/FinanceControl/bank-secrets')
_ID = re.compile(r'[0-9a-f]{32}\Z')
_MAX_FILE = 16_384
_HEADER = b'FCV1'


class ServerVaultError(RuntimeError):
    """A fixed, secret-free diagnostic code."""


def _fail(code: str):
    raise ServerVaultError(code)


def _identity(connection_id: object, owner_user_id: object) -> tuple[str, str]:
    if (type(connection_id) is not str or _ID.fullmatch(connection_id) is None
            or type(owner_user_id) is not str or _ID.fullmatch(owner_user_id) is None):
        _fail('invalid_vault_identity')
    return connection_id, owner_user_id


def _aad(connection_id: str, owner_user_id: str) -> bytes:
    return b'finance-control/banking/v1\0' + owner_user_id.encode('ascii') + b'\0' + connection_id.encode('ascii')


def _credential_payload(credentials: Credentials) -> bytes:
    if (type(credentials) is not Credentials or len(credentials.username) > 256
            or len(credentials.pin) > 1024):
        _fail('invalid_vault_credentials')
    try:
        plaintext = json.dumps({'username': credentials.username, 'pin': credentials.pin},
                               ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    except (UnicodeError, ValueError, TypeError):
        _fail('invalid_vault_credentials')
    if len(plaintext) + len(_HEADER) + 12 + 16 > _MAX_FILE:
        _fail('invalid_vault_credentials')
    return plaintext


def _seal(key: bytes, connection_id: str, owner_user_id: str, credentials: Credentials) -> bytes:
    """Pure cryptographic operation, also testable without a Linux filesystem."""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        _fail('vault_crypto_unavailable')

    plaintext = _credential_payload(credentials)
    nonce = secrets.token_bytes(12)
    try:
        return _HEADER + nonce + AESGCM(key).encrypt(nonce, plaintext,
                                                     _aad(connection_id, owner_user_id))
    except Exception:
        _fail('vault_crypto_error')


def _open(key: bytes, connection_id: str, owner_user_id: str, sealed: bytes) -> Credentials:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        _fail('vault_crypto_unavailable')

    if (type(sealed) is not bytes or len(sealed) < len(_HEADER) + 12 + 16
            or len(sealed) > _MAX_FILE or not sealed.startswith(_HEADER)):
        _fail('vault_integrity_failed')
    try:
        nonce = sealed[len(_HEADER):len(_HEADER) + 12]
        plain = AESGCM(key).decrypt(nonce, sealed[len(_HEADER) + 12:],
                                     _aad(connection_id, owner_user_id))
        values = json.loads(plain.decode('utf-8'))
        if type(values) is not dict or set(values) != {'username', 'pin'}:
            _fail('vault_integrity_failed')
        return Credentials(**values)
    except Exception:
        _fail('vault_integrity_failed')


def _check_file(fd: int) -> None:
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_nlink != 1 or info.st_mode & 0o077 or info.st_mode & 0o600 != 0o600
            or info.st_size > _MAX_FILE):
        _fail('vault_permissions_invalid')


def _read_file(directory_fd: int, name: str) -> bytes | None:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                     dir_fd=directory_fd)
    except FileNotFoundError:
        return None
    except OSError:
        _fail('vault_storage_error')
    try:
        _check_file(fd)
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            return stream.read(_MAX_FILE + 1)
    except OSError:
        _fail('vault_storage_error')
    finally:
        os.close(fd)


def _write_atomic(directory_fd: int, name: str, payload: bytes, *, create_only=False) -> None:
    temporary = '.tmp-' + secrets.token_hex(16)
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=directory_fd)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'wb', closefd=False) as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(fd)
        finally:
            os.close(fd)
        if create_only:
            # Linking is an atomic no-replace publication of a new master key.
            os.link(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
                    follow_symlinks=False)
            os.unlink(temporary, dir_fd=directory_fd)
        else:
            os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    except OSError:
        _fail('vault_storage_error')
    finally:
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        except OSError:
            _fail('vault_storage_error')


class ServerCredentialStore:
    """Storage for one fixed Linux HA directory.

    The caller must authorize the owner/connection relationship against the
    application database. AAD detects changed context; it is not user login.
    """

    def __init__(self, directory):
        if sys.platform != 'linux' or (type(directory) is not str and not isinstance(directory, Path)):
            _fail('vault_platform_or_path_invalid')
        if Path(directory) != APPROVED_DIRECTORY:
            _fail('vault_platform_or_path_invalid')
        self._directory = APPROVED_DIRECTORY

    @contextmanager
    def _locked_directory(self):
        import fcntl

        # Check ancestors without following symlinks. The leaf alone may be created.
        for parent in (self._directory.parent.parent, self._directory.parent):
            try:
                info = parent.lstat()
            except OSError:
                _fail('vault_storage_error')
            if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                    or info.st_uid not in {0, os.geteuid()} or info.st_mode & 0o022):
                _fail('vault_permissions_invalid')
        created_directory = False
        try:
            try:
                self._directory.mkdir(mode=0o700)
                created_directory = True
            except FileExistsError:
                pass
            if created_directory:
                # A restrictive umask can remove owner access before open().
                # Trusted non-writable ancestors and no-follow chmod preserve
                # the newly created directory's identity.
                os.chmod(self._directory, 0o700, follow_symlinks=False)
            directory_fd = os.open(self._directory, os.O_RDONLY | os.O_DIRECTORY |
                                   os.O_NOFOLLOW | os.O_CLOEXEC)
            if created_directory:
                os.fchmod(directory_fd, 0o700)
        except OSError:
            _fail('vault_storage_error')
        lock_fd = None
        try:
            info = os.fstat(directory_fd)
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_mode & 0o077 or info.st_mode & 0o700 != 0o700):
                _fail('vault_permissions_invalid')
            created_lock = False
            try:
                lock_fd = os.open('.lock', os.O_RDWR | os.O_CREAT | os.O_EXCL |
                                  os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                                  dir_fd=directory_fd)
                created_lock = True
            except FileExistsError:
                lock_fd = os.open('.lock', os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
                                  dir_fd=directory_fd)
            if created_lock:
                os.fchmod(lock_fd, 0o600)
            _check_file(lock_fd)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            yield directory_fd
        except OSError:
            _fail('vault_storage_error')
        finally:
            try:
                if lock_fd is not None:
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    except OSError:
                        _fail('vault_storage_error')
                    finally:
                        os.close(lock_fd)
            finally:
                os.close(directory_fd)

    @staticmethod
    def _record_name(connection_id: str) -> str:
        return hashlib.sha256(connection_id.encode('ascii')).hexdigest() + '.vault'

    @staticmethod
    def _key(directory_fd: int, *, create: bool) -> bytes:
        key = _read_file(directory_fd, 'master.key')
        if key is None and create:
            # Never replace a lost key when ciphertext is already present.
            if any(name.endswith('.vault') for name in os.listdir(directory_fd)):
                _fail('vault_key_missing')
            _write_atomic(directory_fd, 'master.key', secrets.token_bytes(32), create_only=True)
            key = _read_file(directory_fd, 'master.key')
        if key is None:
            _fail('vault_key_missing')
        if len(key) != 32:
            _fail('vault_key_invalid')
        return key

    def save(self, connection_id, owner_user_id, credentials):
        connection_id, owner_user_id = _identity(connection_id, owner_user_id)
        _credential_payload(credentials)
        with self._locked_directory() as directory_fd:
            key = self._key(directory_fd, create=True)
            name = self._record_name(connection_id)
            previous = _read_file(directory_fd, name)
            if previous is not None:
                _open(key, connection_id, owner_user_id, previous)
            sealed = _seal(key, connection_id, owner_user_id, credentials)
            _write_atomic(directory_fd, name, sealed)

    def has(self, connection_id, owner_user_id):
        """Return absence only for a healthy vault; authenticate existing records."""
        connection_id, owner_user_id = _identity(connection_id, owner_user_id)
        with self._locked_directory() as directory_fd:
            sealed = _read_file(directory_fd, self._record_name(connection_id))
            if sealed is None:
                key = _read_file(directory_fd, 'master.key')
                if key is None:
                    if any(name.endswith('.vault') for name in os.listdir(directory_fd)):
                        _fail('vault_key_missing')
                elif len(key) != 32:
                    _fail('vault_key_invalid')
                return False
            _open(self._key(directory_fd, create=False), connection_id,
                  owner_user_id, sealed)
            return True

    def load(self, connection_id, owner_user_id):
        connection_id, owner_user_id = _identity(connection_id, owner_user_id)
        with self._locked_directory() as directory_fd:
            sealed = _read_file(directory_fd, self._record_name(connection_id))
            if sealed is None:
                _fail('vault_record_missing')
            return _open(self._key(directory_fd, create=False),
                         connection_id, owner_user_id, sealed)

    def delete(self, connection_id, owner_user_id):
        connection_id, owner_user_id = _identity(connection_id, owner_user_id)
        with self._locked_directory() as directory_fd:
            name = self._record_name(connection_id)
            sealed = _read_file(directory_fd, name)
            if sealed is None:
                _fail('vault_record_missing')
            _open(self._key(directory_fd, create=False), connection_id, owner_user_id, sealed)
            try:
                os.unlink(name, dir_fd=directory_fd)
                os.fsync(directory_fd)
            except OSError:
                _fail('vault_storage_error')
