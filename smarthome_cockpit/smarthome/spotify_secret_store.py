"""Windows credential-store adapter for per-profile Spotify tokens."""

from __future__ import annotations

import json
from datetime import datetime
from importlib import import_module
from typing import Protocol

from smarthome.spotify_oauth import SpotifyTokenSet


KEYRING_SERVICE_NAME = "SmartHome Spotify"
SECRET_RECORD_VERSION = 1
MAX_SECRET_RECORD_CHARS = 32_768
SECRET_RECORD_FIELDS = frozenset(
    {
        "version",
        "access_token",
        "refresh_token",
        "scopes",
        "expires_at",
    }
)


class SpotifySecretStoreError(RuntimeError):
    """Controlled local credential-store failure without backend details."""


class KeyringBackend(Protocol):
    """Narrow subset of the Python keyring API used by this adapter."""

    def get_password(self, service_name: str, username: str) -> str | None:
        """Return one stored credential value."""

    def set_password(self, service_name: str, username: str, password: str) -> None:
        """Store one credential value."""

    def delete_password(self, service_name: str, username: str) -> None:
        """Delete one credential value."""


class KeyringSpotifyTokenStore:
    """Persist tokens as one opaque record per routing connection ID."""

    def __init__(self, backend: KeyringBackend | None = None) -> None:
        if backend is None:
            try:
                backend = import_module("keyring")  # type: ignore[assignment]
            except (ImportError, ModuleNotFoundError):
                raise SpotifySecretStoreError(
                    "Der optionale lokale Credential-Store ist nicht installiert."
                ) from None
        self._backend = backend

    def load(self, connection_id: str) -> SpotifyTokenSet | None:
        """Load and validate one token record from the operating-system store."""

        _validate_connection_id(connection_id)
        try:
            payload = self._backend.get_password(KEYRING_SERVICE_NAME, connection_id)
        except Exception:
            raise SpotifySecretStoreError(
                "Der lokale Spotify-Credential-Store konnte nicht gelesen werden."
            ) from None
        if payload is None:
            return None
        if not isinstance(payload, str) or len(payload) > MAX_SECRET_RECORD_CHARS:
            raise SpotifySecretStoreError(
                "Der lokale Spotify-Credential-Eintrag ist ungültig."
            )
        try:
            raw = json.loads(payload)
            return _tokens_from_record(raw)
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            raise SpotifySecretStoreError(
                "Der lokale Spotify-Credential-Eintrag ist ungültig."
            ) from None

    def save(self, connection_id: str, tokens: SpotifyTokenSet) -> None:
        """Replace exactly one profile's opaque token record."""

        _validate_connection_id(connection_id)
        if not isinstance(tokens, SpotifyTokenSet):
            raise SpotifySecretStoreError(
                "Es können nur validierte Spotify-Tokens gespeichert werden."
            )
        payload = json.dumps(
            {
                "version": SECRET_RECORD_VERSION,
                "access_token": tokens.access_token,
                "refresh_token": tokens.refresh_token,
                "scopes": sorted(tokens.scopes),
                "expires_at": tokens.expires_at.isoformat(),
            },
            ensure_ascii=True,
            separators=(",", ":"),
        )
        if len(payload) > MAX_SECRET_RECORD_CHARS:
            raise SpotifySecretStoreError(
                "Der Spotify-Credential-Eintrag ist zu groß."
            )
        try:
            self._backend.set_password(
                KEYRING_SERVICE_NAME,
                connection_id,
                payload,
            )
        except Exception:
            raise SpotifySecretStoreError(
                "Der lokale Spotify-Credential-Store konnte nicht geschrieben werden."
            ) from None

    def delete(self, connection_id: str) -> None:
        """Idempotently delete exactly one profile's credential record."""

        _validate_connection_id(connection_id)
        try:
            existing = self._backend.get_password(
                KEYRING_SERVICE_NAME,
                connection_id,
            )
            if existing is not None:
                self._backend.delete_password(
                    KEYRING_SERVICE_NAME,
                    connection_id,
                )
        except Exception:
            raise SpotifySecretStoreError(
                "Der lokale Spotify-Credential-Store konnte nicht bereinigt werden."
            ) from None


def _tokens_from_record(raw: object) -> SpotifyTokenSet:
    if not isinstance(raw, dict) or set(raw) != SECRET_RECORD_FIELDS:
        raise ValueError("invalid credential record")
    if raw.get("version") != SECRET_RECORD_VERSION:
        raise ValueError("unsupported credential record version")
    scopes = raw["scopes"]
    if not isinstance(scopes, list) or not all(
        isinstance(scope, str) for scope in scopes
    ):
        raise ValueError("invalid credential scopes")
    expires_at = raw["expires_at"]
    if not isinstance(expires_at, str):
        raise ValueError("invalid credential expiry")
    return SpotifyTokenSet(
        access_token=raw["access_token"],
        refresh_token=raw["refresh_token"],
        scopes=frozenset(scopes),
        expires_at=datetime.fromisoformat(expires_at),
    )


def _validate_connection_id(value: object) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 128
        or any(character in value for character in "\r\n\0")
    ):
        raise SpotifySecretStoreError(
            "Die Spotify-Verbindungs-ID ist ungültig."
        )
