"""Local, transport-free Spotify OAuth and token lifecycle primitives."""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Callable, Mapping, Protocol
from urllib.parse import parse_qs, urlencode, urlsplit


SPOTIFY_AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
MINIMUM_SPOTIFY_SCOPES = frozenset(
    {
        "user-read-private",
        "user-read-playback-state",
        "user-modify-playback-state",
    }
)
PROFILE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
OAUTH_ERROR_PATTERN = re.compile(r"^[a-z_]{1,64}$")
# Spotify's opaque ``ubi`` value currently uses standard Base64 characters.
# Keep it bounded and single-valued, but accept ``+`` and ``/`` after URL decoding.
OPAQUE_CALLBACK_PATTERN = re.compile(r"^[A-Za-z0-9._~+/=-]{1,4096}$")


class SpotifyOAuthError(RuntimeError):
    """Base class for controlled OAuth failures without secret values."""


class SpotifyOAuthConfigurationError(SpotifyOAuthError):
    """Raised when local OAuth configuration is unsafe or incomplete."""


class SpotifyAuthorizationError(SpotifyOAuthError):
    """Raised when an authorization callback is invalid or expired."""


class SpotifyAuthorizationDenied(SpotifyAuthorizationError):
    """Raised when Spotify or the user rejects an authorization request."""

    def __init__(self, error_code: str) -> None:
        self.error_code = (
            error_code if OAUTH_ERROR_PATTERN.fullmatch(error_code) else "oauth_error"
        )
        super().__init__("Die Spotify-Autorisierung wurde nicht erteilt.")


class SpotifyReauthorizationRequired(SpotifyOAuthError):
    """Raised when no valid local grant can be refreshed."""


class SpotifyTokenStatus(StrEnum):
    """Local access-token decision before a Spotify API request."""

    ACTIVE = "active"
    REFRESH_REQUIRED = "refresh_required"


@dataclass(frozen=True, slots=True)
class SpotifyOAuthConfig:
    """Public PKCE settings; no client secret is accepted or required."""

    client_id: str
    redirect_uri: str
    scopes: frozenset[str] = MINIMUM_SPOTIFY_SCOPES
    authorization_timeout_seconds: int = 600

    def __post_init__(self) -> None:
        _require_text(self.client_id, "Spotify-Client-ID")
        _validate_redirect_uri(self.redirect_uri)
        if not isinstance(self.scopes, frozenset) or self.scopes != MINIMUM_SPOTIFY_SCOPES:
            raise SpotifyOAuthConfigurationError(
                "Spotify muss exakt mit den freigegebenen Minimal-Scopes arbeiten."
            )
        if (
            not isinstance(self.authorization_timeout_seconds, int)
            or isinstance(self.authorization_timeout_seconds, bool)
            or not 60 <= self.authorization_timeout_seconds <= 1800
        ):
            raise SpotifyOAuthConfigurationError(
                "Das Spotify-Autorisierungsfenster muss 60 bis 1800 Sekunden betragen."
            )


@dataclass(frozen=True, slots=True)
class SpotifyAuthorizationGrant:
    """Single callback result for a later token exchange."""

    profile_id: str
    client_id: str
    redirect_uri: str
    code: str = field(repr=False)
    code_verifier: str = field(repr=False)


class SpotifyAuthorizationSession:
    """Single-use PKCE transaction with state and expiration checks."""

    def __init__(
        self,
        *,
        config: SpotifyOAuthConfig,
        profile_id: str,
        state: str,
        code_verifier: str,
        created_at: datetime,
    ) -> None:
        if not PROFILE_ID_PATTERN.fullmatch(profile_id):
            raise SpotifyOAuthConfigurationError("Die Spotify-Profil-ID ist ungültig.")
        _require_aware_datetime(created_at, "Startzeit")
        self.config = config
        self.profile_id = profile_id
        self._state = state
        self._code_verifier = code_verifier
        self.created_at = created_at
        self.expires_at = created_at + timedelta(
            seconds=config.authorization_timeout_seconds
        )
        self._consumed = False

    @classmethod
    def begin(
        cls,
        config: SpotifyOAuthConfig,
        profile_id: str,
        *,
        now: datetime,
        entropy: Callable[[int], bytes] = secrets.token_bytes,
    ) -> SpotifyAuthorizationSession:
        """Create a local authorization transaction without network access."""

        state = _base64url(entropy(32))
        code_verifier = _base64url(entropy(64))
        if len(state) < 32:
            raise SpotifyOAuthConfigurationError(
                "Der erzeugte OAuth-Statuswert ist zu kurz."
            )
        if not 43 <= len(code_verifier) <= 128:
            raise SpotifyOAuthConfigurationError(
                "Der erzeugte PKCE-Code-Verifier hat eine ungültige Länge."
            )
        return cls(
            config=config,
            profile_id=profile_id,
            state=state,
            code_verifier=code_verifier,
            created_at=now,
        )

    @property
    def authorization_url(self) -> str:
        """Return the Spotify consent URL without exposing the verifier."""

        challenge = _base64url(
            hashlib.sha256(self._code_verifier.encode("ascii")).digest()
        )
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.config.client_id,
                "redirect_uri": self.config.redirect_uri,
                "scope": " ".join(sorted(self.config.scopes)),
                "state": self._state,
                "code_challenge_method": "S256",
                "code_challenge": challenge,
            }
        )
        return f"{SPOTIFY_AUTHORIZE_URL}?{query}"

    @property
    def consumed(self) -> bool:
        return self._consumed

    def complete(
        self,
        callback_url: str,
        *,
        now: datetime,
    ) -> SpotifyAuthorizationGrant:
        """Validate one callback and consume the transaction on a valid response."""

        _require_aware_datetime(now, "Callback-Zeit")
        if self._consumed:
            raise SpotifyAuthorizationError(
                "Die Spotify-Autorisierung wurde bereits verarbeitet."
            )
        if now >= self.expires_at:
            self._consumed = True
            raise SpotifyAuthorizationError(
                "Die Spotify-Autorisierung ist abgelaufen."
            )

        expected = urlsplit(self.config.redirect_uri)
        actual = urlsplit(callback_url)
        if (
            actual.scheme != expected.scheme
            or actual.netloc != expected.netloc
            or actual.path != expected.path
            or actual.fragment
        ):
            raise SpotifyAuthorizationError(
                "Der Spotify-Callback verwendet nicht die freigegebene Redirect-URI."
            )
        try:
            query = parse_qs(actual.query, keep_blank_values=True, strict_parsing=True)
        except ValueError as exc:
            raise SpotifyAuthorizationError(
                "Der Spotify-Callback ist ungültig."
            ) from exc

        callback_fields = set(query)
        callback_fields.discard("ubi")
        if callback_fields not in ({"state", "code"}, {"state", "error"}):
            raise SpotifyAuthorizationError(
                "Der Spotify-Callback enthält unerwartete Parameter."
            )
        if "ubi" in query:
            ubi = _single_query_value(query, "ubi")
            if not OPAQUE_CALLBACK_PATTERN.fullmatch(ubi):
                raise SpotifyAuthorizationError(
                    "Der Spotify-Callback enthält einen ungültigen Zusatzwert."
                )

        state = _single_query_value(query, "state")
        if not secrets.compare_digest(state, self._state):
            raise SpotifyAuthorizationError(
                "Der Spotify-Callback besitzt einen ungültigen Statuswert."
            )

        if "error" in query:
            error_code = _single_query_value(query, "error")
            self._consumed = True
            raise SpotifyAuthorizationDenied(error_code)

        code = _single_query_value(query, "code")
        if not code or len(code) > 4096:
            raise SpotifyAuthorizationError(
                "Der Spotify-Autorisierungscode ist ungültig."
            )
        self._consumed = True
        return SpotifyAuthorizationGrant(
            profile_id=self.profile_id,
            client_id=self.config.client_id,
            redirect_uri=self.config.redirect_uri,
            code=code,
            code_verifier=self._code_verifier,
        )


@dataclass(frozen=True, slots=True)
class SpotifyTokenSet:
    """In-memory token material whose repr never contains credentials."""

    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    scopes: frozenset[str]
    expires_at: datetime

    def __post_init__(self) -> None:
        _validate_token(self.access_token, "Access-Token")
        _validate_token(self.refresh_token, "Refresh-Token")
        if self.scopes != MINIMUM_SPOTIFY_SCOPES:
            raise SpotifyOAuthError(
                "Das Spotify-Token besitzt nicht exakt die freigegebenen Scopes."
            )
        _require_aware_datetime(self.expires_at, "Token-Ablaufzeit")

    @classmethod
    def from_initial_response(
        cls,
        response: Mapping[str, object],
        *,
        now: datetime,
    ) -> SpotifyTokenSet:
        """Validate the first PKCE token response."""

        return cls._from_response(response, now=now, previous=None)

    @classmethod
    def from_refresh_response(
        cls,
        response: Mapping[str, object],
        *,
        previous: SpotifyTokenSet,
        now: datetime,
    ) -> SpotifyTokenSet:
        """Validate refreshed tokens and retain an omitted refresh token."""

        return cls._from_response(response, now=now, previous=previous)

    @classmethod
    def _from_response(
        cls,
        response: Mapping[str, object],
        *,
        now: datetime,
        previous: SpotifyTokenSet | None,
    ) -> SpotifyTokenSet:
        _require_aware_datetime(now, "Token-Zeit")
        if not isinstance(response, Mapping):
            raise SpotifyOAuthError("Spotify lieferte keine gültige Token-Antwort.")
        if response.get("error") == "invalid_grant":
            raise SpotifyReauthorizationRequired(
                "Das Spotify-Profil muss erneut autorisiert werden."
            )
        if "error" in response:
            raise SpotifyOAuthError("Spotify hat die Token-Anfrage abgelehnt.")
        if response.get("token_type") != "Bearer":
            raise SpotifyOAuthError("Spotify lieferte keinen Bearer-Token.")

        access_token = _response_token(response, "access_token")
        refresh_value = response.get("refresh_token")
        if refresh_value is None and previous is not None:
            refresh_token = previous.refresh_token
        else:
            refresh_token = _response_token(response, "refresh_token")

        scope_value = response.get("scope")
        if scope_value is None and previous is not None:
            scopes = previous.scopes
        elif isinstance(scope_value, str):
            scopes = frozenset(scope_value.split())
        else:
            raise SpotifyOAuthError("Spotify lieferte keine gültigen Token-Scopes.")

        expires_in = response.get("expires_in")
        if (
            not isinstance(expires_in, int)
            or isinstance(expires_in, bool)
            or expires_in <= 0
        ):
            raise SpotifyOAuthError("Spotify lieferte keine gültige Token-Laufzeit.")

        try:
            expires_at = now + timedelta(seconds=expires_in)
        except (OverflowError, ValueError) as exc:
            raise SpotifyOAuthError(
                "Spotify lieferte keine gültige Token-Laufzeit."
            ) from exc

        return cls(
            access_token=access_token,
            refresh_token=refresh_token,
            scopes=scopes,
            expires_at=expires_at,
        )

    def status(
        self,
        *,
        now: datetime,
        refresh_margin_seconds: int = 60,
    ) -> SpotifyTokenStatus:
        """Classify whether callers may use or must refresh the access token."""

        _require_aware_datetime(now, "Prüfzeit")
        if (
            not isinstance(refresh_margin_seconds, int)
            or isinstance(refresh_margin_seconds, bool)
            or refresh_margin_seconds < 0
        ):
            raise SpotifyOAuthConfigurationError(
                "Der Token-Erneuerungsvorlauf darf nicht negativ sein."
            )
        if now + timedelta(seconds=refresh_margin_seconds) >= self.expires_at:
            return SpotifyTokenStatus.REFRESH_REQUIRED
        return SpotifyTokenStatus.ACTIVE


class SpotifyTokenStore(Protocol):
    """Secret-store boundary implemented outside the repository."""

    def load(self, connection_id: str) -> SpotifyTokenSet | None:
        """Load one account's tokens without logging them."""

    def save(self, connection_id: str, tokens: SpotifyTokenSet) -> None:
        """Atomically replace one account's tokens."""

    def delete(self, connection_id: str) -> None:
        """Delete local tokens when access is revoked or disconnected."""


class SpotifyTokenManager:
    """Refresh access tokens through injected local-only collaborators."""

    def __init__(
        self,
        store: SpotifyTokenStore,
        refresher: Callable[[str], Mapping[str, object]],
    ) -> None:
        self.store = store
        self.refresher = refresher

    def access_token(self, connection_id: str, *, now: datetime) -> str:
        """Return a usable token, refreshing once when necessary."""

        _require_text(connection_id, "Spotify-Verbindungs-ID")
        tokens = self.store.load(connection_id)
        if tokens is None:
            raise SpotifyReauthorizationRequired(
                "Das Spotify-Profil ist nicht autorisiert."
            )
        if tokens.status(now=now) is SpotifyTokenStatus.ACTIVE:
            return tokens.access_token

        response = self.refresher(tokens.refresh_token)
        try:
            refreshed = SpotifyTokenSet.from_refresh_response(
                response,
                previous=tokens,
                now=now,
            )
        except SpotifyReauthorizationRequired:
            self.store.delete(connection_id)
            raise
        self.store.save(connection_id, refreshed)
        return refreshed.access_token

    def disconnect(self, connection_id: str) -> None:
        """Remove local authorization material for exactly one profile."""

        _require_text(connection_id, "Spotify-Verbindungs-ID")
        self.store.delete(connection_id)


def _validate_redirect_uri(value: object) -> None:
    _require_text(value, "Spotify-Redirect-URI")
    parsed = urlsplit(value)
    local_http = parsed.scheme == "http" and parsed.hostname == "127.0.0.1"
    if (
        not (parsed.scheme == "https" or local_http)
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or "*" in value
        or parsed.hostname == "localhost"
    ):
        raise SpotifyOAuthConfigurationError(
            "Die Spotify-Redirect-URI muss HTTPS oder lokales 127.0.0.1-HTTP verwenden."
        )


def _single_query_value(query: Mapping[str, list[str]], name: str) -> str:
    values = query.get(name)
    if values is None or len(values) != 1:
        raise SpotifyAuthorizationError(
            "Der Spotify-Callback enthält keinen eindeutigen Pflichtwert."
        )
    return values[0]


def _response_token(response: Mapping[str, object], name: str) -> str:
    value = response.get(name)
    _validate_token(value, name)
    return value


def _validate_token(value: object, label: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 16_384:
        raise SpotifyOAuthError(f"Spotify lieferte keinen gültigen {label}.")


def _require_text(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise SpotifyOAuthConfigurationError(f"{label} darf nicht leer sein.")


def _require_aware_datetime(value: object, label: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise SpotifyOAuthConfigurationError(
            f"{label} muss eine zeitzonenbewusste Zeit sein."
        )


def _base64url(value: bytes) -> str:
    if not isinstance(value, bytes):
        raise SpotifyOAuthConfigurationError("Die OAuth-Zufallsquelle ist ungültig.")
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")
