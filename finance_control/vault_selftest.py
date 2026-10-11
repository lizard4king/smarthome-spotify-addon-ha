"""Offline Linux build check for the bank vault; never opens the live vault."""

from __future__ import annotations

import os
import stat
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path

from finance_control.administration import Administration
from finance_control.security import server_vault
from finance_control.security.credentials import Credentials
from finance_control.security.personal_vault_cleanup import PersonalVaultCleanup
from finance_control.security.server_card_store import CardSecret, ServerCardStore
from finance_control.security.server_klarna_store import ServerKlarnaStore
from finance_control.security.server_paypal_store import ServerPayPalStore


_CONNECTION = 'a' * 32
_OWNER = 'b' * 32
_OTHER_OWNER = 'c' * 32
_CREDS = Credentials('synthetic-user', 'synthetic-pin')


def _expect_vault_error(code, operation):
    try:
        operation()
    except server_vault.ServerVaultError as error:
        if str(error) != code:
            raise RuntimeError('klarna_unexpected_vault_error') from None
    else:
        raise RuntimeError('klarna_expected_vault_error_missing')


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
            cards = ServerCardStore(directory)
            card_id = cards.new_card_id()
            secret = CardSecret('5412345678901234')
            cards.save(_CONNECTION, _OWNER, card_id, secret)
            card_path = directory / cards._record_name(_CONNECTION, _OWNER, card_id)
            if (stat.S_IMODE(card_path.stat().st_mode) != 0o600
                    or secret.card_number.encode('ascii') in card_path.read_bytes()
                    or cards.load(_CONNECTION, _OWNER, card_id) != secret):
                raise RuntimeError('card_store_failed')
            sealed_card = card_path.read_bytes()
            card_path.write_bytes(sealed_card[:-1] + bytes([sealed_card[-1] ^ 1]))
            try:
                cards.has(_CONNECTION, _OWNER, card_id)
            except server_vault.ServerVaultError:
                pass
            else:
                raise RuntimeError('card_tamper_accepted')
            card_path.write_bytes(sealed_card)
            if cards.delete_connection(_CONNECTION, _OWNER) != 1:
                raise RuntimeError('card_cleanup_failed')
            if cards.has(_CONNECTION, _OWNER, card_id):
                raise RuntimeError('card_delete_failed')

            paypal = ServerPayPalStore()
            paypal_record = {'version': 1, 'private_key': 'SYNTHETIC-ONLY',
                             'application_id': None, 'session': None}
            paypal.save(_OWNER, paypal_record)
            paypal_path = directory / paypal._name(_OWNER)
            anchor = directory / '.paypal-key-check'
            if (paypal.load(_OWNER) != paypal_record or paypal.load(_OTHER_OWNER) is not None
                    or stat.S_IMODE(paypal_path.stat().st_mode) != 0o600
                    or stat.S_IMODE(anchor.stat().st_mode) != 0o600
                    or b'SYNTHETIC-ONLY' in paypal_path.read_bytes()
                    or 'SYNTHETIC' in paypal_path.name):
                raise RuntimeError('paypal_store_failed')
            sealed_paypal = paypal_path.read_bytes()
            paypal_path.write_bytes(sealed_paypal[:-1] + bytes([sealed_paypal[-1] ^ 1]))
            try:
                paypal.load(_OWNER)
            except server_vault.ServerVaultError as error:
                if str(error) != 'paypal_integrity_failed':
                    raise RuntimeError('paypal_tamper_check_failed') from None
            else:
                raise RuntimeError('paypal_tamper_accepted')
            paypal_path.write_bytes(sealed_paypal)

            master = directory / 'master.key'
            original_key = master.read_bytes()
            master.write_bytes(bytes(reversed(original_key)))
            try:
                paypal.save(_OTHER_OWNER, paypal_record)
            except server_vault.ServerVaultError as error:
                if str(error) != 'paypal_key_check_invalid':
                    raise RuntimeError('paypal_key_check_failed') from None
            else:
                raise RuntimeError('paypal_replacement_key_accepted')
            if (directory / paypal._name(_OTHER_OWNER)).exists():
                raise RuntimeError('paypal_replacement_key_wrote_record')
            master.write_bytes(original_key)

            sealed_anchor = anchor.read_bytes()
            anchor.unlink()
            try:
                paypal.load(_OWNER)
            except server_vault.ServerVaultError as error:
                if str(error) != 'paypal_key_check_missing':
                    raise RuntimeError('paypal_missing_anchor_check_failed') from None
            else:
                raise RuntimeError('paypal_missing_anchor_accepted')
            anchor.write_bytes(sealed_anchor)
            anchor.chmod(0o600)

            klarna = ServerKlarnaStore()
            klarna_record = {'version': 1, 'private_key': 'SYNTHETIC-KLARNA-ONLY',
                             'application_id': None, 'session': None}
            klarna.save(_OWNER, klarna_record)
            klarna_path = directory / klarna._name(_OWNER)
            klarna_anchor = directory / '.klarna-key-check'
            if (ServerKlarnaStore().load(_OWNER) != klarna_record
                    or klarna.load(_OTHER_OWNER) is not None
                    or paypal.load(_OWNER) != paypal_record
                    or klarna_path == paypal_path
                    or stat.S_IMODE(klarna_path.stat().st_mode) != 0o600
                    or stat.S_IMODE(klarna_anchor.stat().st_mode) != 0o600
                    or b'SYNTHETIC-KLARNA-ONLY' in klarna_path.read_bytes()):
                raise RuntimeError('klarna_store_failed')
            sealed_klarna = klarna_path.read_bytes()
            other_klarna_path = directory / klarna._name(_OTHER_OWNER)
            other_klarna_path.write_bytes(sealed_klarna)
            other_klarna_path.chmod(0o600)
            _expect_vault_error('klarna_integrity_failed', lambda: klarna.load(_OTHER_OWNER))
            other_klarna_path.unlink()

            # A provider record cannot be interpreted through the other's store.
            klarna_path.write_bytes(sealed_paypal)
            _expect_vault_error('klarna_integrity_failed', lambda: klarna.load(_OWNER))
            klarna_path.write_bytes(sealed_klarna)
            paypal_path.write_bytes(sealed_klarna)
            _expect_vault_error('paypal_integrity_failed', lambda: paypal.load(_OWNER))
            paypal_path.write_bytes(sealed_paypal)

            klarna_path.write_bytes(sealed_klarna[:-1] + bytes([sealed_klarna[-1] ^ 1]))
            _expect_vault_error('klarna_integrity_failed', lambda: klarna.load(_OWNER))
            klarna_path.write_bytes(sealed_klarna)
            master.write_bytes(bytes([original_key[0] ^ 1]) + original_key[1:])
            try:
                _expect_vault_error('klarna_key_anchor_invalid',
                                    lambda: klarna.save(_OTHER_OWNER, klarna_record))
                if other_klarna_path.exists():
                    raise RuntimeError('klarna_replacement_key_wrote_record')
            finally:
                master.write_bytes(original_key)
            klarna.delete(_OWNER)
            if (klarna.load(_OWNER) is not None or klarna_path.exists()
                    or paypal.load(_OWNER) != paypal_record):
                raise RuntimeError('klarna_delete_failed')
            paypal.delete(_OWNER)
            if paypal.load(_OWNER) is not None or paypal_path.exists():
                raise RuntimeError('paypal_delete_failed')

            # All database and vault paths still belong to the temporary tree.
            administration = Administration(parent / 'synthetic-admin.sqlite')
            with administration._connection(write=True) as db:
                db.execute('INSERT INTO app_users(id,email,display_name,role) VALUES (?,?,?,?)',
                           (_OWNER, 'synthetic@example.org', 'Synthetic', 'member'))
            cleanup = PersonalVaultCleanup(administration)
            paypal.save(_OWNER, paypal_record)
            klarna.save(_OWNER, klarna_record)
            try:
                with ExitStack() as exits:
                    with administration._connection(write=True) as db:
                        exits.enter_context(cleanup.revoke(_OWNER))
                        # Existing bank cleanup opens its own descriptor. This
                        # must work while personal revocation is staged.
                        with server_vault.ServerCredentialStore(directory)._locked_directory():
                            pass
                        db.execute('UPDATE app_users SET active=0 WHERE id=?', (_OWNER,))
                        raise ValueError('synthetic_sql_rollback')
            except ValueError:
                pass
            if paypal.load(_OWNER) != paypal_record or klarna.load(_OWNER) != klarna_record:
                raise RuntimeError('personal_cleanup_rollback_failed')
            paypal_recovery = directory / f'.personal-revoke-{_OWNER}.paypal.vault'
            paypal_path.rename(paypal_recovery)
            cleanup.recover()
            if paypal.load(_OWNER) != paypal_record or paypal_recovery.exists():
                raise RuntimeError('personal_cleanup_crash_restore_failed')
            with ExitStack() as exits:
                with administration._connection(write=True) as db:
                    exits.enter_context(cleanup.revoke(_OWNER))
                    db.execute('UPDATE app_users SET active=0 WHERE id=?', (_OWNER,))
            if paypal.load(_OWNER) is not None or klarna.load(_OWNER) is not None:
                raise RuntimeError('personal_cleanup_commit_failed')
            # A post-commit process crash leaves staged encrypted files. Startup
            # must purge them for the inactive owner, never resurrect the grant.
            paypal.save(_OWNER, paypal_record)
            klarna.save(_OWNER, klarna_record)
            paypal_path.rename(paypal_recovery)
            klarna_recovery = directory / f'.personal-revoke-{_OWNER}.klarna.vault'
            klarna_path.rename(klarna_recovery)
            cleanup.recover()
            if paypal_recovery.exists() or klarna_recovery.exists():
                raise RuntimeError('personal_cleanup_crash_purge_failed')
            with administration._connection(write=True) as db:
                db.execute('UPDATE app_users SET active=1 WHERE id=?', (_OWNER,))
            paypal.save(_OWNER, paypal_record)
            klarna.save(_OWNER, klarna_record)
            try:
                with ExitStack() as exits:
                    with administration._connection(write=True) as db:
                        exits.enter_context(cleanup.revoke(_OWNER))
                        db.execute('UPDATE app_users SET active=0 WHERE id=?', (_OWNER,))
                    raise ValueError('synthetic_postcommit_failure')
            except ValueError:
                pass
            if paypal_path.exists() or klarna_path.exists():
                raise RuntimeError('personal_cleanup_postcommit_restored_grant')
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
