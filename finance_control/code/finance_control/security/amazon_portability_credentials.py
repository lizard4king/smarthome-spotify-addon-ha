"""Local Windows Credential Manager storage for Amazon Data Portability."""

from dataclasses import dataclass, field
import json
import re
import sys


class AmazonPortabilityCredentialError(RuntimeError):
    """Sanitized diagnostic that never contains credential values."""


@dataclass(frozen=True)
class AmazonPortabilityCredentials:
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)
    refresh_token: str = field(repr=False)

    def __post_init__(self):
        if any(not isinstance(value, str) or not value for value in
               (self.client_id, self.client_secret, self.refresh_token)):
            raise AmazonPortabilityCredentialError("Amazon-Zugangsdaten müssen vollständig sein.")

    def __reduce__(self):
        raise TypeError("Amazon-Zugangsdaten dürfen nicht serialisiert werden.")


class AmazonPortabilityCredentialStore:
    """Store one profile's OAuth credentials as one vault payload."""

    SERVICE = "finance-control/amazon-data-portability"

    def __init__(self, backend=None):
        if backend is not None:
            self._backend = backend
            return
        if sys.platform != "win32":
            raise AmazonPortabilityCredentialError("Windows Credential Manager erforderlich.")
        try:
            from keyring.backends.Windows import WinVaultKeyring
            self._backend = WinVaultKeyring()
            self._backend.persist = "local machine"
        except Exception:
            raise AmazonPortabilityCredentialError("Windows Credential Manager nicht verfügbar.") from None

    @staticmethod
    def validate_alias(alias):
        if not isinstance(alias, str) or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", alias) is None:
            raise AmazonPortabilityCredentialError("Ungültiger lokaler Profilname.")
        return alias

    def save(self, alias, credentials):
        alias = self.validate_alias(alias)
        if not isinstance(credentials, AmazonPortabilityCredentials):
            raise AmazonPortabilityCredentialError("Ungültige Amazon-Zugangsdaten.")
        try:
            payload = json.dumps({
                "client_id": credentials.client_id,
                "client_secret": credentials.client_secret,
                "refresh_token": credentials.refresh_token,
            })
            self._backend.set_password(self.SERVICE, alias, payload)
        except Exception:
            raise AmazonPortabilityCredentialError("Amazon-Zugangsdaten konnten nicht sicher gespeichert werden.") from None

    def load(self, alias):
        alias = self.validate_alias(alias)
        try:
            payload = self._backend.get_password(self.SERVICE, alias)
            if payload is None:
                raise ValueError
            values = json.loads(payload)
            if not isinstance(values, dict) or set(values) != {"client_id", "client_secret", "refresh_token"}:
                raise ValueError
            return AmazonPortabilityCredentials(**values)
        except Exception:
            raise AmazonPortabilityCredentialError(
                "Gespeicherte Amazon-Zugangsdaten fehlen oder sind nicht lesbar."
            ) from None

    def delete(self, alias):
        alias = self.validate_alias(alias)
        try:
            self._backend.delete_password(self.SERVICE, alias)
        except Exception:
            raise AmazonPortabilityCredentialError("Amazon-Zugangsdaten konnten nicht gelöscht werden.") from None
