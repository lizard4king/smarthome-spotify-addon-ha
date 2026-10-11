"""Owner-bound encrypted Klarna state in the HA vault."""

import hashlib
import json
import os
import secrets

from . import server_vault

_HEADER = b'FCK1'
_PREFIX = b'finance-control/klarna-enable-banking/v1\0'
_MAX_PLAIN = 15_000
_ANCHOR_NAME = '.klarna-key-check'
_ANCHOR_HEADER = b'FCKK1'
_ANCHOR_PLAIN = b'finance-control/klarna-key-check/v1'
# Provider anchors may authenticate the shared master key. Their records are
# never opened or copied by this store.
_SHARED_ANCHORS = (('.paypal-key-check', b'FCPK1',
                    b'finance-control/paypal-enable-banking/v1\0key-check'),)


def _identity(owner):
    return server_vault._identity(owner, owner)[0]


def _payload(record):
    if type(record) is not dict or record.get('version') != 1:
        server_vault._fail('klarna_payload_invalid')
    try:
        plain = json.dumps(record, ensure_ascii=True, allow_nan=False).encode('ascii')
    except (TypeError, ValueError, UnicodeError):
        server_vault._fail('klarna_payload_invalid')
    if len(plain) > _MAX_PLAIN:
        server_vault._fail('klarna_payload_invalid')
    return plain


def _seal(key, owner, record):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = secrets.token_bytes(12)
    return _HEADER + nonce + AESGCM(key).encrypt(
        nonce, _payload(record), _PREFIX + _identity(owner).encode('ascii'))


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
        server_vault._fail('klarna_integrity_failed')


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
        server_vault._fail('klarna_key_anchor_invalid')


def _verify_shared_key_anchors(key, fd):
    for name, header, aad in _SHARED_ANCHORS:
        sealed = server_vault._read_file(fd, name)
        if sealed is None:
            continue
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        try:
            if (type(sealed) is not bytes or len(sealed) < len(header) + 28
                    or not sealed.startswith(header)):
                raise ValueError
            nonce = sealed[len(header):len(header) + 12]
            plain = AESGCM(key).decrypt(nonce, sealed[len(header) + 12:], aad)
            if plain != b'finance-control/paypal-key-check/v1':
                raise ValueError
        except Exception:
            server_vault._fail('klarna_key_anchor_invalid')


def _klarna_records_exist(fd):
    try:
        return any(name.endswith('.klarna.vault') for name in os.listdir(fd))
    except OSError:
        server_vault._fail('vault_storage_error')


def _checked_key(vault, fd, *, create):
    anchor = server_vault._read_file(fd, _ANCHOR_NAME)
    if anchor is None and _klarna_records_exist(fd):
        server_vault._fail('klarna_key_anchor_missing')
    key = vault._key(fd, create=create and anchor is None)
    _verify_shared_key_anchors(key, fd)
    if anchor is None:
        if create:
            server_vault._write_atomic(fd, _ANCHOR_NAME, _seal_anchor(key), create_only=True)
    else:
        _verify_anchor(key, anchor)
    return key


class ServerKlarnaStore:
    def __init__(self):
        self._vault = server_vault.ServerCredentialStore(server_vault.APPROVED_DIRECTORY)

    @staticmethod
    def _name(owner):
        return hashlib.sha256(_PREFIX + _identity(owner).encode('ascii')).hexdigest() + '.klarna.vault'

    def load(self, owner):
        with self._vault._locked_directory() as fd:
            server_vault._check_personal_recovery(fd, owner)
            sealed = server_vault._read_file(fd, self._name(owner))
            if sealed is None:
                anchor = server_vault._read_file(fd, _ANCHOR_NAME)
                if anchor is None and _klarna_records_exist(fd):
                    server_vault._fail('klarna_key_anchor_missing')
                key_file = server_vault._read_file(fd, 'master.key')
                if key_file is None and any(name.endswith('.vault') for name in os.listdir(fd)):
                    server_vault._fail('vault_key_missing')
                if key_file is not None and len(key_file) != 32:
                    server_vault._fail('vault_key_invalid')
                if anchor is not None:
                    _verify_anchor(self._vault._key(fd, create=False), anchor)
                    _verify_shared_key_anchors(self._vault._key(fd, create=False), fd)
                elif key_file is not None:
                    _verify_shared_key_anchors(key_file, fd)
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
                return
            _open(_checked_key(self._vault, fd, create=False), owner, sealed)
            try:
                os.unlink(name, dir_fd=fd)
                os.fsync(fd)
            except OSError:
                server_vault._fail('vault_storage_error')
