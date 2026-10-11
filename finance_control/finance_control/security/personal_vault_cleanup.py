"""Recoverable two-provider cleanup coordinated with the caller's SQL commit.

The SQL writer must be acquired before staging. ``revoke`` must remain entered
until that writer has committed or rolled back. Vault locks cover only individual
file phases; existing bank hooks may acquire the vault while the context yields.
No provider I/O occurs.
This is a recovery protocol, not an atomic transaction across SQLite and files.
"""

import logging
import os
import re
import sqlite3
import sys
from contextlib import contextmanager

from . import server_klarna_store, server_paypal_store, server_vault


_PREFIX = '.personal-revoke-'
_PENDING = re.compile(r'\.personal-revoke-([0-9a-f]{32})\.(paypal|klarna)\.vault\Z')
_ERROR = 'personal_cleanup_recovery_required'
_LOG = logging.getLogger(__name__)
_PROVIDERS = (
    ('paypal', server_paypal_store, server_paypal_store.ServerPayPalStore),
    ('klarna', server_klarna_store, server_klarna_store.ServerKlarnaStore),
)


def _owner(owner):
    try:
        return server_vault._identity(owner, owner)[0]
    except Exception:
        server_vault._fail(_ERROR)


def _pending_owner(name):
    match = _PENDING.fullmatch(name) if type(name) is str else None
    if match is None:
        server_vault._fail(_ERROR)
    return match[1]


class PersonalVaultCleanup:
    def __init__(self, administration):
        self.administration = administration

    @staticmethod
    def _vault():
        return server_vault.ServerCredentialStore(server_vault.APPROVED_DIRECTORY)

    def _fresh_active(self, owner):
        # The caller's SQL context has already ended. Read durable state before
        # opening the vault; an exception after commit must never revive grants.
        with self.administration._connection() as db:
            row = db.execute('SELECT active FROM app_users WHERE id=?', (_owner(owner),)).fetchone()
            return row is not None and row[0] == 1

    @staticmethod
    def _preflight(fd, owner):
        """Validate all originals/recovery records and anchors before mutation."""
        owner = _owner(owner)
        records = []
        for provider, module, store in _PROVIDERS:
            original = store._name(owner)
            pending = f'{_PREFIX}{owner}.{provider}.vault'
            sealed = server_vault._read_file(fd, original)
            saved = server_vault._read_file(fd, pending)
            if sealed is not None and saved is not None:
                server_vault._fail(_ERROR)
            records.append((module, original, pending, sealed, saved))
        anchors = [(provider, module, server_vault._read_file(fd, module._ANCHOR_NAME))
                   for provider, module, _ in _PROVIDERS]
        key = server_vault._read_file(fd, 'master.key')
        if key is None:
            if (any(sealed is not None or saved is not None for _, _, _, sealed, saved in records)
                    or any(anchor is not None for _, _, anchor in anchors)
                    or any(name.endswith('.vault') for name in os.listdir(fd))):
                server_vault._fail(_ERROR)
            return records
        if len(key) != 32:
            server_vault._fail(_ERROR)
        for provider, module, anchor in anchors:
            if anchor is not None:
                module._verify_anchor(key, anchor)
            elif any(name.endswith('.' + provider + '.vault') for name in os.listdir(fd)):
                server_vault._fail(_ERROR)
        for module, _, _, sealed, saved in records:
            for value in (sealed, saved):
                if value is not None:
                    module._open(key, owner, value)
        return records

    @staticmethod
    def _restore(fd, records):
        # All paths were checked under this still-held directory lock. Refuse
        # collisions again; no original may be replaced with a recovery record.
        for _, original, pending, _, _ in records:
            saved = server_vault._read_file(fd, pending)
            if saved is not None:
                if server_vault._read_file(fd, original) is not None:
                    server_vault._fail(_ERROR)
                os.rename(pending, original, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)

    @staticmethod
    def _purge(fd, records):
        for _, _, pending, _, _ in records:
            if server_vault._read_file(fd, pending) is not None:
                os.unlink(pending, dir_fd=fd)
        os.fsync(fd)

    @contextmanager
    def revoke(self, owner):
        """Stage deletion; the caller exits this context only after SQL ends."""
        if sys.platform != 'linux':
            yield
            return
        try:
            with self._vault()._locked_directory() as fd:
                records = self._preflight(fd, owner)
                if any(saved is not None for _, _, _, _, saved in records):
                    server_vault._fail(_ERROR)
                try:
                    for _, original, pending, sealed, _ in records:
                        if sealed is not None:
                            os.rename(original, pending, src_dir_fd=fd, dst_dir_fd=fd)
                    os.fsync(fd)
                except Exception:
                    self._restore(fd, self._preflight(fd, owner))
                    server_vault._fail(_ERROR)
        except (server_vault.ServerVaultError, OSError):
            server_vault._fail(_ERROR)
        # Release the vault before caller hooks. The caller still holds its SQL
        # writer, and provider store guards reject pending records.
        try:
            yield
        except BaseException:
            active = None
            try:
                active = self._fresh_active(owner)
                with self._vault()._locked_directory() as fd:
                    records = self._preflight(fd, owner)
                    if active:
                        self._restore(fd, records)
                    else:
                        self._purge(fd, records)
            except (server_vault.ServerVaultError, OSError, sqlite3.Error):
                if active is not False:
                    server_vault._fail(_ERROR)
                # Durable revocation succeeded. Preserve the caller's original
                # post-commit exception, leaving encrypted recovery for retry.
                _LOG.warning(_ERROR)
            raise
        else:
            try:
                with self._vault()._locked_directory() as fd:
                    self._purge(fd, self._preflight(fd, owner))
            except (server_vault.ServerVaultError, OSError):
                # SQL already revoked the owner. Retain remaining ciphertext;
                # before_restore/recover must finish purging before reactivation.
                _LOG.warning(_ERROR)

    def recover(self):
        """Startup recovery: SQL writer first; an invalid plan stops startup."""
        if sys.platform != 'linux':
            return
        try:
            with self.administration._connection(write=True) as db:
                with self._vault()._locked_directory() as fd:
                    names = [name for name in os.listdir(fd) if name.startswith(_PREFIX)]
                    owners = sorted({_pending_owner(name) for name in names})
                    # Validate the full discovered plan before mutating any owner.
                    plans = [(owner, self._preflight(fd, owner)) for owner in owners]
                    for owner, records in plans:
                        row = db.execute('SELECT active FROM app_users WHERE id=?', (owner,)).fetchone()
                        if row is not None and row[0] == 1:
                            self._restore(fd, records)
                        else:
                            self._purge(fd, records)
        except (server_vault.ServerVaultError, OSError, sqlite3.Error):
            server_vault._fail(_ERROR)

    def before_restore(self, owner):
        """Caller holds SQL writer and has verified the target is inactive."""
        if sys.platform != 'linux':
            return
        try:
            with self._vault()._locked_directory() as fd:
                records = self._preflight(fd, owner)
                self._purge(fd, records)
        except (server_vault.ServerVaultError, OSError):
            server_vault._fail(_ERROR)
