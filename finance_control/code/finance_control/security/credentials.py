from dataclasses import dataclass, field
import json
import re
import sys


class CredentialError(RuntimeError):
    """Safe diagnostic without underlying credential values."""


@dataclass(frozen=True)
class Credentials:
    username: str = field(repr=False)
    pin: str = field(repr=False)

    def __post_init__(self):
        if not isinstance(self.username, str) or not isinstance(self.pin, str) or not self.username or not self.pin:
            raise CredentialError('Benutzerkennung und PIN fehlen.')

    def __reduce__(self):
        raise TypeError('Credentials dürfen nicht serialisiert werden.')


class WindowsCredentialStore:
    """Use WinVault explicitly; do not consult configurable keyring backends."""
    def __init__(self):
        if sys.platform != 'win32':
            raise CredentialError('Windows Credential Manager erforderlich.')
        try:
            from keyring.backends.Windows import WinVaultKeyring
            self._backend = WinVaultKeyring()
            self._backend.persist = 'local machine'
        except Exception:
            raise CredentialError('Windows Credential Manager nicht verfügbar.') from None

    @staticmethod
    def _alias(alias):
        if not isinstance(alias, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', alias):
            raise CredentialError('Ungültiger lokaler Zugangsname.')
        return alias

    def save(self, alias, credentials):
        alias = self._alias(alias)
        payload = json.dumps({'username': credentials.username, 'pin': credentials.pin})
        try:
            self._backend.set_password('finance-control/banking', alias, payload)
        except Exception:
            raise CredentialError('Zugang konnte nicht sicher gespeichert werden.') from None

    def load(self, alias):
        alias = self._alias(alias)
        try:
            payload = self._backend.get_password('finance-control/banking', alias)
            if payload is None:
                raise CredentialError()
            values = json.loads(payload)
            return Credentials(**values)
        except Exception:
            raise CredentialError('Gespeicherter Zugang fehlt oder ist nicht lesbar.') from None

    def delete(self, alias):
        alias = self._alias(alias)
        try:
            self._backend.delete_password('finance-control/banking', alias)
        except Exception:
            raise CredentialError('Zugang konnte nicht gelöscht werden.') from None


def main():
    import argparse
    import getpass
    parser = argparse.ArgumentParser(description='Lokale sichere Bankzugänge verwalten')
    parser.add_argument('action', choices=['save', 'delete'])
    parser.add_argument('alias')
    args = parser.parse_args()
    try:
        if not sys.stdin.isatty():
            raise CredentialError('Ein lokales interaktives Terminal ist erforderlich.')
        WindowsCredentialStore._alias(args.alias)
        vault = WindowsCredentialStore()
        if args.action == 'save':
            # Both identity and PIN stay out of shell history and command arguments.
            vault.save(args.alias, Credentials(getpass.getpass('Bank-Benutzerkennung: '),
                                              getpass.getpass('Bank-PIN/Passwort: ')))
            print('Zugang im Windows Credential Manager gespeichert.')
        else:
            vault.delete(args.alias)
            print('Zugang entfernt.')
    except (CredentialError, EOFError, KeyboardInterrupt):
        print('Vorgang nicht abgeschlossen; lokale Eingabe und Credential Manager prüfen.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
