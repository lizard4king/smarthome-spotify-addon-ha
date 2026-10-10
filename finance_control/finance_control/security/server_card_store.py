"""Linux-only encrypted Postbank card metadata beside the server bank vault.

The caller must authorize the owner/connection/card relationship. The full
card number never belongs in API responses, logs, filenames, or exceptions.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from dataclasses import dataclass

from . import server_vault


_HEADER = b'FCC1'
_AAD_PREFIX = b'finance-control/postbank-card/v1\0'
_ID = re.compile(r'[0-9a-f]{32}\Z', re.ASCII)
_PAN = re.compile(r'[0-9]{16}\Z', re.ASCII)
_FINGERPRINT = re.compile(r'[0-9a-f]{64}\Z', re.ASCII)
_MAX_FILE = 16_384


@dataclass(frozen=True, repr=False)
class CardSecret:
    card_number: str
    card_account_number: str | None = None
    account_fingerprint: str | None = None


def _identity(connection_id, owner_user_id, card_id):
    connection_id, owner_user_id = server_vault._identity(connection_id, owner_user_id)
    if type(card_id) is not str or _ID.fullmatch(card_id) is None:
        server_vault._fail('invalid_card_identity')
    return connection_id, owner_user_id, card_id


def _payload(secret):
    if (type(secret) is not CardSecret or type(secret.card_number) is not str or
            _PAN.fullmatch(secret.card_number) is None or
            (secret.card_account_number is not None and
             (type(secret.card_account_number) is not str or
              re.fullmatch(r'[\x20-\x7e]{1,30}', secret.card_account_number, re.ASCII) is None)) or
            (secret.account_fingerprint is not None and
             (type(secret.account_fingerprint) is not str or
              _FINGERPRINT.fullmatch(secret.account_fingerprint) is None))):
        server_vault._fail('invalid_card_secret')
    try:
        plain = json.dumps({
            'card_number': secret.card_number,
            'card_account_number': secret.card_account_number,
            'account_fingerprint': secret.account_fingerprint,
        }, ensure_ascii=True, separators=(',', ':')).encode('ascii')
    except (TypeError, UnicodeError, ValueError):
        server_vault._fail('invalid_card_secret')
    if len(plain) + len(_HEADER) + 12 + 16 > _MAX_FILE:
        server_vault._fail('invalid_card_secret')
    return plain


def _aad(connection_id, owner_user_id, card_id):
    return (_AAD_PREFIX + owner_user_id.encode('ascii') + b'\0' +
            connection_id.encode('ascii') + b'\0' + card_id.encode('ascii'))


def _seal(key, connection_id, owner_user_id, card_id, secret):
    """Pure authenticated encryption, testable without Linux filesystem access."""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        server_vault._fail('vault_crypto_unavailable')
    plain = _payload(secret)
    nonce = secrets.token_bytes(12)
    try:
        return _HEADER + nonce + AESGCM(key).encrypt(
            nonce, plain, _aad(connection_id, owner_user_id, card_id))
    except Exception:
        server_vault._fail('vault_crypto_error')


def _open(key, connection_id, owner_user_id, card_id, sealed):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        server_vault._fail('vault_crypto_unavailable')
    if (type(sealed) is not bytes or len(sealed) < len(_HEADER) + 12 + 16 or
            len(sealed) > _MAX_FILE or not sealed.startswith(_HEADER)):
        server_vault._fail('card_integrity_failed')
    try:
        nonce = sealed[len(_HEADER):len(_HEADER) + 12]
        plain = AESGCM(key).decrypt(
            nonce, sealed[len(_HEADER) + 12:], _aad(connection_id, owner_user_id, card_id))
        values = json.loads(plain.decode('ascii'))
        if type(values) is not dict or set(values) != {
                'card_number', 'card_account_number', 'account_fingerprint'}:
            server_vault._fail('card_integrity_failed')
        secret = CardSecret(**values)
        _payload(secret)
        return secret
    except Exception:
        server_vault._fail('card_integrity_failed')


class ServerCardStore:
    """One ciphertext per card, using the server credential vault's protection."""

    def __init__(self, directory):
        self._vault = server_vault.ServerCredentialStore(directory)

    @staticmethod
    def new_card_id():
        """Create an opaque server-side selector for a new card record."""
        return secrets.token_hex(16)

    @staticmethod
    def _prefix(connection_id, owner_user_id):
        digest = hashlib.sha256(
            _AAD_PREFIX + owner_user_id.encode('ascii') + b'\0' +
            connection_id.encode('ascii')).hexdigest()
        return digest + '.cards.'

    @classmethod
    def _record_name(cls, connection_id, owner_user_id, card_id):
        return cls._prefix(connection_id, owner_user_id) + card_id + '.vault'

    @staticmethod
    def _key_if_present(directory_fd):
        key = server_vault._read_file(directory_fd, 'master.key')
        if key is None:
            if any(name.endswith('.vault') for name in os.listdir(directory_fd)):
                server_vault._fail('vault_key_missing')
            return None
        if len(key) != 32:
            server_vault._fail('vault_key_invalid')
        return key

    def save(self, connection_id, owner_user_id, card_id, secret):
        connection_id, owner_user_id, card_id = _identity(
            connection_id, owner_user_id, card_id)
        _payload(secret)
        with self._vault._locked_directory() as directory_fd:
            key = self._vault._key(directory_fd, create=True)
            name = self._record_name(connection_id, owner_user_id, card_id)
            previous = server_vault._read_file(directory_fd, name)
            if previous is not None:
                _open(key, connection_id, owner_user_id, card_id, previous)
            server_vault._write_atomic(
                directory_fd, name, _seal(key, connection_id, owner_user_id, card_id, secret))

    def has(self, connection_id, owner_user_id, card_id):
        connection_id, owner_user_id, card_id = _identity(
            connection_id, owner_user_id, card_id)
        with self._vault._locked_directory() as directory_fd:
            sealed = server_vault._read_file(
                directory_fd, self._record_name(connection_id, owner_user_id, card_id))
            if sealed is None:
                self._key_if_present(directory_fd)
                return False
            _open(self._vault._key(directory_fd, create=False),
                  connection_id, owner_user_id, card_id, sealed)
            return True

    def load(self, connection_id, owner_user_id, card_id):
        connection_id, owner_user_id, card_id = _identity(
            connection_id, owner_user_id, card_id)
        with self._vault._locked_directory() as directory_fd:
            sealed = server_vault._read_file(
                directory_fd, self._record_name(connection_id, owner_user_id, card_id))
            if sealed is None:
                server_vault._fail('card_record_missing')
            return _open(self._vault._key(directory_fd, create=False),
                         connection_id, owner_user_id, card_id, sealed)

    def delete(self, connection_id, owner_user_id, card_id):
        connection_id, owner_user_id, card_id = _identity(
            connection_id, owner_user_id, card_id)
        with self._vault._locked_directory() as directory_fd:
            name = self._record_name(connection_id, owner_user_id, card_id)
            sealed = server_vault._read_file(directory_fd, name)
            if sealed is None:
                server_vault._fail('card_record_missing')
            _open(self._vault._key(directory_fd, create=False),
                  connection_id, owner_user_id, card_id, sealed)
            self._unlink(directory_fd, name)

    def delete_connection(self, connection_id, owner_user_id):
        connection_id, owner_user_id = server_vault._identity(connection_id, owner_user_id)
        with self._vault._locked_directory() as directory_fd:
            prefix = self._prefix(connection_id, owner_user_id)
            names = tuple(name for name in os.listdir(directory_fd) if name.startswith(prefix))
            if not names:
                self._key_if_present(directory_fd)
                return 0
            key = self._vault._key(directory_fd, create=False)
            for name in names:
                suffix = name[len(prefix):]
                if not suffix.endswith('.vault') or _ID.fullmatch(suffix[:-6]) is None:
                    server_vault._fail('card_integrity_failed')
                card_id = suffix[:-6]
                sealed = server_vault._read_file(directory_fd, name)
                if sealed is None:
                    server_vault._fail('card_integrity_failed')
                _open(key, connection_id, owner_user_id, card_id, sealed)
            for name in names:
                self._unlink(directory_fd, name)
            return len(names)

    @staticmethod
    def _unlink(directory_fd, name):
        try:
            os.unlink(name, dir_fd=directory_fd)
            os.fsync(directory_fd)
        except OSError:
            server_vault._fail('vault_storage_error')
