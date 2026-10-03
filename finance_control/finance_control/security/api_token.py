"""Dedicated Windows Credential Manager storage for the read API token."""
from dataclasses import dataclass, field
import re
import secrets
import sys


class ApiTokenError(RuntimeError):
    """Safe diagnostic that never contains a token value."""


@dataclass(frozen=True)
class ApiToken:
    value: str = field(repr=False)

    def __post_init__(self):
        if not isinstance(self.value, str) or len(self.value) < 32:
            raise ApiTokenError('Ungültiger API-Zugangstoken.')

    def __reduce__(self):
        raise TypeError('ApiToken darf nicht serialisiert werden.')


class WindowsApiTokenStore:
    """Store tokens only in the Windows vault under a separate service name."""

    SERVICE = 'finance-control/read-api'

    def __init__(self, backend=None):
        if backend is not None:
            self._backend = backend
            return
        if sys.platform != 'win32':
            raise ApiTokenError('Windows Credential Manager erforderlich.')
        try:
            from keyring.backends.Windows import WinVaultKeyring
            self._backend = WinVaultKeyring()
            self._backend.persist = 'local machine'
        except Exception:
            raise ApiTokenError('Windows Credential Manager nicht verfügbar.') from None

    @staticmethod
    def validate_alias(alias):
        if not isinstance(alias, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', alias):
            raise ApiTokenError('Ungültiger lokaler Tokenname.')
        return alias

    def save(self, alias, token):
        alias = self.validate_alias(alias)
        token = token if isinstance(token, ApiToken) else ApiToken(token)
        try:
            self._backend.set_password(self.SERVICE, alias, token.value)
        except Exception:
            raise ApiTokenError('Token konnte nicht sicher gespeichert werden.') from None

    def load(self, alias):
        alias = self.validate_alias(alias)
        try:
            value = self._backend.get_password(self.SERVICE, alias)
            if value is None:
                raise ApiTokenError()
            return ApiToken(value)
        except Exception:
            raise ApiTokenError('Gespeicherter API-Token fehlt oder ist nicht lesbar.') from None

    def delete(self, alias):
        alias = self.validate_alias(alias)
        try:
            self._backend.delete_password(self.SERVICE, alias)
        except Exception:
            raise ApiTokenError('Token konnte nicht gelöscht werden.') from None


def generate_token():
    """Return a cryptographically random URL-safe bearer token."""
    return ApiToken(secrets.token_urlsafe(48))
