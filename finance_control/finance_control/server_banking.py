"""Owner-bound server credential administration; no bank connection or ledger access."""

from __future__ import annotations

import sys

from .administration import AdministrationError, _id, _revision
from .security.credentials import CredentialError, Credentials


_STATE = 'bank-credentials-state'
_SAVE = 'bank-credentials-save'
_DELETE = 'bank-credentials-delete'


def _real_vault():
    from .security.server_vault import APPROVED_DIRECTORY, ServerCredentialStore

    return ServerCredentialStore(APPROVED_DIRECTORY)


class ServerBanking:
    """Restrict vault operations to active owners with a current DB revision."""

    def __init__(self, administration, vault_factory=None):
        self.administration = administration
        self.vault_factory = vault_factory or _real_vault

    @staticmethod
    def _available():
        return sys.platform == 'linux'

    def _vault(self):
        if not self._available():
            raise AdministrationError('server_banking_unsupported', 503)
        try:
            return self.vault_factory()
        except Exception:
            raise AdministrationError('server_banking_unavailable', 503) from None

    @staticmethod
    def _has(vault, connection_id, owner_id):
        try:
            # A missing has() is an unavailable vault, never an absent secret.
            result = vault.has(connection_id, owner_id)
            if type(result) is not bool:
                raise TypeError('invalid vault response')
            return result
        except Exception:
            raise AdministrationError('server_banking_unavailable', 503) from None

    def cleanup_connections(self, connections):
        """Delete only DB-verified active connection records before revocation."""
        if not connections or not self._available():
            return
        vault = self._vault()
        for connection in connections:
            connection_id, owner_id = connection['id'], connection['user_id']
            if self._has(vault, connection_id, owner_id):
                try:
                    vault.delete(connection_id, owner_id)
                except Exception:
                    raise AdministrationError('server_banking_unavailable', 503) from None

    def dispatch(self, actor, action, data):
        fields = {_STATE: set(), _SAVE: {'id', 'revision', 'confirmed', 'username', 'pin'},
                  _DELETE: {'id', 'revision', 'confirmed'}}
        if action not in fields or type(data) is not dict or set(data) != fields[action]:
            raise AdministrationError('invalid_action')
        if action == _STATE:
            with self.administration._connection() as db:
                owner = self.administration._actor(db, actor)
                rows = db.execute(
                    "SELECT id,revision FROM app_bank_connections "
                    "WHERE user_id=? AND status!='REVOKED' ORDER BY rowid", (owner['id'],)).fetchall()
                if not self._available():
                    return {'supported': False, 'connections': [
                        {'id': row['id'], 'revision': row['revision'],
                         'server_credentials_present': None} for row in rows]}
                vault = self._vault()
                return {'supported': True, 'connections': [
                    {'id': row['id'], 'revision': row['revision'],
                     'server_credentials_present': self._has(vault, row['id'], owner['id'])}
                    for row in rows]}

        if data['confirmed'] is not True:
            raise AdministrationError('confirmation_required')
        connection_id, revision = _id(data['id']), _revision(data['revision'])
        credentials = None
        if action == _SAVE:
            username, pin = data['username'], data['pin']
            if type(username) is not str or type(pin) is not str or not username or not pin:
                raise AdministrationError('invalid_bank_credentials')
            try:
                if (len(username) > 256 or len(pin) > 1024
                        or len(username.encode('utf-8')) > 4096
                        or len(pin.encode('utf-8')) > 4096):
                    raise AdministrationError('invalid_bank_credentials')
                credentials = Credentials(username, pin)
            except (CredentialError, UnicodeError):
                raise AdministrationError('invalid_bank_credentials') from None
        with self.administration._connection(write=True) as db:
            owner = self.administration._actor(db, actor)
            row = db.execute(
                "SELECT id,user_id,revision FROM app_bank_connections WHERE id=? AND status!='REVOKED'",
                (connection_id,)).fetchone()
            if row is None:
                raise AdministrationError('unknown_connection', 404)
            if row['user_id'] != owner['id']:
                raise AdministrationError('forbidden', 403)
            if row['revision'] != revision:
                raise AdministrationError('stale_revision', 409)
            vault = self._vault()
            if action == _DELETE and not self._has(vault, connection_id, owner['id']):
                return {'id': connection_id, 'revision': revision,
                        'server_credentials_present': False}
            try:
                if action == _SAVE:
                    vault.save(connection_id, owner['id'], credentials)
                else:
                    vault.delete(connection_id, owner['id'])
            except Exception:
                raise AdministrationError('server_banking_unavailable', 503) from None
            db.execute('UPDATE app_bank_connections SET revision=revision+1 WHERE id=?',
                       (connection_id,))
            return {'id': connection_id, 'revision': revision + 1,
                    'server_credentials_present': action == _SAVE}
