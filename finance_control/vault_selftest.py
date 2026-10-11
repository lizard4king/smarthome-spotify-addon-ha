"""Offline Linux build check for the bank vault; never opens the live vault."""

from __future__ import annotations

import os
import stat
import sys
import tempfile
from pathlib import Path

from finance_control.security import server_vault
from finance_control.security.credentials import Credentials
from finance_control.security.server_card_store import CardSecret, ServerCardStore
from finance_control.security.server_paypal_store import ServerPayPalStore


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
            paypal.delete(_OWNER)
            if paypal.load(_OWNER) is not None or paypal_path.exists():
                raise RuntimeError('paypal_delete_failed')
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
