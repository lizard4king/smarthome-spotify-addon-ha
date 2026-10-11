"""Owner-bound encrypted PayPal application and consent state in the HA vault.

The shared bank vault supplies no-follow file access, permissions, locking and
the master key. Separate filenames, header and AAD prevent cross-protocol reads.
This directory is already excluded from HA and financial-data backups.
"""

import hashlib
import json
import os
import secrets

from . import server_vault

_HEADER = b'FCP1'
_PREFIX = b'finance-control/paypal-enable-banking/v1\0'
_MAX_PLAIN = 15_000
_ANCHOR_NAME = '.paypal-key-check'
_ANCHOR_HEADER = b'FCPK1'
_ANCHOR_PLAIN = b'finance-control/paypal-key-check/v1'


def _identity(owner):
    return server_vault._identity(owner, owner)[0]


def _payload(record):
    if type(record) is not dict or record.get('version') != 1:
        server_vault._fail('paypal_secret_invalid')
    try:
        plain = json.dumps(record, ensure_ascii=True, allow_nan=False).encode('ascii')
    except (TypeError, ValueError, UnicodeError):
        server_vault._fail('paypal_secret_invalid')
    if len(plain) > _MAX_PLAIN:
        server_vault._fail('paypal_secret_invalid')
    return plain


def _seal(key, owner, record):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = secrets.token_bytes(12)
    return _HEADER + nonce + AESGCM(key).encrypt(nonce, _payload(record),
                                               _PREFIX + _identity(owner).encode('ascii'))


def _open(key, owner, sealed):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        if (type(sealed) is not bytes or not 32 <= len(sealed) <= server_vault._MAX_FILE
                or not sealed.startswith(_HEADER)):
            raise ValueError
        plain = AESGCM(key).decrypt(sealed[4:16], sealed[16:],
                                    _PREFIX + _identity(owner).encode('ascii'))
        record = json.loads(plain.decode('ascii'))
        _payload(record)
        return record
    except Exception:
        server_vault._fail('paypal_integrity_failed')


def _seal_anchor(key):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = secrets.token_bytes(12)
    return _ANCHOR_HEADER + nonce + AESGCM(key).encrypt(
        nonce, _ANCHOR_PLAIN, _PREFIX + b'key-check')


def _verify_anchor(key, sealed):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        if (type(sealed) is not bytes or len(sealed) != len(_ANCHOR_HEADER) + 12
                + len(_ANCHOR_PLAIN) + 16 or not sealed.startswith(_ANCHOR_HEADER)):
            raise ValueError
        plain = AESGCM(key).decrypt(
            sealed[len(_ANCHOR_HEADER):len(_ANCHOR_HEADER) + 12],
            sealed[len(_ANCHOR_HEADER) + 12:], _PREFIX + b'key-check')
        if plain != _ANCHOR_PLAIN:
            raise ValueError
    except Exception:
        server_vault._fail('paypal_key_check_invalid')


def _paypal_records_exist(fd):
    try:
        return any(name.endswith('.paypal.vault') for name in os.listdir(fd))
    except OSError:
        server_vault._fail('vault_storage_error')


def _checked_key(vault, fd, *, create):
    anchor = server_vault._read_file(fd, _ANCHOR_NAME)
    if anchor is None and _paypal_records_exist(fd):
        server_vault._fail('paypal_key_check_missing')
    key = vault._key(fd, create=create and anchor is None)
    if anchor is None:
        if create:
            server_vault._write_atomic(fd, _ANCHOR_NAME, _seal_anchor(key), create_only=True)
    else:
        _verify_anchor(key, anchor)
    return key


class ServerPayPalStore:
    def __init__(self):
        self._vault = server_vault.ServerCredentialStore(server_vault.APPROVED_DIRECTORY)

    @staticmethod
    def _name(owner):
        return hashlib.sha256(_PREFIX + _identity(owner).encode('ascii')).hexdigest() + '.paypal.vault'

    def load(self, owner):
        with self._vault._locked_directory() as fd:
            server_vault._check_personal_recovery(fd, owner)
            sealed = server_vault._read_file(fd, self._name(owner))
            if sealed is None:
                anchor = server_vault._read_file(fd, _ANCHOR_NAME)
                if anchor is None and _paypal_records_exist(fd):
                    server_vault._fail('paypal_key_check_missing')
                if anchor is not None:
                    _verify_anchor(self._vault._key(fd, create=False), anchor)
                    return None
                # Absence must not conceal a missing or malformed shared key.
                key = server_vault._read_file(fd, 'master.key')
                if key is None and any(name.endswith('.vault') for name in os.listdir(fd)):
                    server_vault._fail('vault_key_missing')
                if key is not None and len(key) != 32:
                    server_vault._fail('vault_key_invalid')
                return None
            return _open(_checked_key(self._vault, fd, create=False), owner, sealed)

    def save(self, owner, record):
        _payload(record)
        with self._vault._locked_directory() as fd:
            server_vault._check_personal_recovery(fd, owner)
            key = _checked_key(self._vault, fd, create=True)
            name = self._name(owner)
            previous = server_vault._read_file(fd, name)
            if previous is not None:
                _open(key, owner, previous)
            server_vault._write_atomic(fd, name, _seal(key, owner, record))

    def delete(self, owner):
        with self._vault._locked_directory() as fd:
            server_vault._check_personal_recovery(fd, owner)
            name = self._name(owner)
            sealed = server_vault._read_file(fd, name)
            if sealed is None:
                # There is no PayPal material for this owner to revoke.
                return
            _open(_checked_key(self._vault, fd, create=False), owner, sealed)
            try:
                os.unlink(name, dir_fd=fd)
                os.fsync(fd)
            except OSError:
                server_vault._fail('vault_storage_error')
