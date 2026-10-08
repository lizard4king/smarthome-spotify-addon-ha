"""Offline Linux build check for the bank vault; never opens the live vault."""

from __future__ import annotations

import os
import stat
import sys
import tempfile
from pathlib import Path

from finance_control.security import server_vault
from finance_control.security.credentials import Credentials


_CONNECTION = 'a' * 32
_OWNER = 'b' * 32
_OTHER_OWNER = 'c' * 32
_CREDS = Credentials('synthetic-user', 'synthetic-pin')


def _check() -> None:
    if sys.platform != 'linux':
        raise RuntimeError('linux_required')
    original_directory = server_vault.APPROVED_DIRECTORY
    temporary_root = Path('/tmp').resolve()
    if temporary_root == original_directory or original_directory in temporary_root.parents:
        raise RuntimeError('invalid_temporary_root')
    with tempfile.TemporaryDirectory(prefix='finance-vault-selftest-', dir='/tmp') as temporary:
        parent = Path(temporary) / 'FinanceControl'
        parent.mkdir(mode=0o700)
        parent.chmod(0o700)
        directory = parent / 'bank-secrets'
        # This substitution is process-local and is always restored before exit.
        server_vault.APPROVED_DIRECTORY = directory
        try:
            previous_umask = os.umask(0o777)
            try:
                store = server_vault.ServerCredentialStore(directory)
                if store.has(_CONNECTION, _OWNER):
                    raise RuntimeError('unexpected_record')
                store.save(_CONNECTION, _OWNER, _CREDS)
            finally:
                os.umask(previous_umask)

            record = directory / store._record_name(_CONNECTION)
            for path, expected in ((directory, 0o700),
                                   (directory / '.lock', 0o600),
                                   (directory / 'master.key', 0o600),
                                   (record, 0o600)):
                if stat.S_IMODE(path.stat().st_mode) != expected:
                    raise RuntimeError('invalid_file_mode')
            sealed = record.read_bytes()
            if b'synthetic-user' in sealed or b'synthetic-pin' in sealed:
                raise RuntimeError('plaintext_record')

            reopened = server_vault.ServerCredentialStore(directory)
            if not reopened.has(_CONNECTION, _OWNER) or reopened.load(_CONNECTION, _OWNER) != _CREDS:
                raise RuntimeError('persistence_failed')
            try:
                reopened.has(_CONNECTION, _OTHER_OWNER)
            except server_vault.ServerVaultError as error:
                if str(error) != 'vault_integrity_failed':
                    raise RuntimeError('wrong_owner_check_failed') from None
            else:
                raise RuntimeError('wrong_owner_accepted')

            record.write_bytes(sealed[:-1] + bytes([sealed[-1] ^ 1]))
            try:
                reopened.has(_CONNECTION, _OWNER)
            except server_vault.ServerVaultError as error:
                if str(error) != 'vault_integrity_failed':
                    raise RuntimeError('tamper_check_failed') from None
            else:
                raise RuntimeError('tamper_accepted')
            record.write_bytes(sealed)
            reopened.delete(_CONNECTION, _OWNER)
            if reopened.has(_CONNECTION, _OWNER):
                raise RuntimeError('delete_failed')
        finally:
            server_vault.APPROVED_DIRECTORY = original_directory


def main() -> int:
    try:
        _check()
    except Exception:
        print('Vault-Selbsttest: FEHLER', file=sys.stderr)
        return 1
    print('Vault-Selbsttest: OK')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
