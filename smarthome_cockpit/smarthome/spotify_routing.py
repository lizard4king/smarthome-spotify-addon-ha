"""Pure routing decisions for personalized Spotify playback requests."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping


PROFILE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SPOTIFY_PROFILE_CONFIG_VERSION = 1
MAX_SPOTIFY_PROFILE_CONFIG_BYTES = 64 * 1024
PROFILE_CONFIG_FIELDS = frozenset(
    {
        "profile_id",
        "display_name",
        "connection_id",
        "aliases",
        "alexa_person_ids",
        "allowed_targets",
        "default_target",
        "enabled",
    }
)


class SpotifyRoutingConfigurationError(ValueError):
    """Raised when a Spotify profile registry is ambiguous or unsafe."""


class SpotifyRoutingStatus(StrEnum):
    """Result category for a profile routing attempt."""

    ROUTED = "routed"
    NEEDS_PROFILE = "needs_profile"
    UNKNOWN_PROFILE = "unknown_profile"
    TARGET_NOT_ALLOWED = "target_not_allowed"


class SpotifyRoutingSource(StrEnum):
    """Input that selected the Spotify profile."""

    EXPLICIT = "explicit"
    SESSION = "session"
    VOICE_ID = "voice_id"
    HOUSEHOLD_DEFAULT = "household_default"


@dataclass(frozen=True, slots=True)
class SpotifyProfile:
    """Non-secret routing metadata for one separately authorized account.

    OAuth tokens deliberately do not belong to this model. The application
    service resolves ``connection_id`` through a local secret store.
    """

    profile_id: str
    display_name: str
    connection_id: str
    aliases: tuple[str, ...] = ()
    alexa_person_ids: tuple[str, ...] = ()
    allowed_targets: tuple[str, ...] = ()
    default_target: str | None = None
    enabled: bool = True

    def __post_init__(self) -> None:
        if not PROFILE_ID_PATTERN.fullmatch(self.profile_id):
            raise SpotifyRoutingConfigurationError(
                "Die Spotify-Profil-ID muss ein stabiler technischer Name sein."
            )
        _require_text(self.display_name, "Anzeigename")
        _require_text(self.connection_id, "Verbindungs-ID")
        if not isinstance(self.enabled, bool):
            raise SpotifyRoutingConfigurationError(
                "Der Aktivstatus eines Spotify-Profils muss boolesch sein."
            )

        aliases = _unique_texts(self.aliases, "Profilalias", normalize=True)
        person_ids = _unique_texts(
            self.alexa_person_ids, "Alexa-Personen-ID", normalize=False
        )
        targets = _unique_texts(
            self.allowed_targets, "Wiedergabeziel", normalize=False
        )
        object.__setattr__(self, "aliases", aliases)
        object.__setattr__(self, "alexa_person_ids", person_ids)
        object.__setattr__(self, "allowed_targets", targets)

        if self.default_target is not None:
            _require_text(self.default_target, "Standard-Wiedergabeziel")
            if self.default_target not in targets:
                raise SpotifyRoutingConfigurationError(
                    "Das Standard-Wiedergabeziel muss in der Positivliste stehen."
                )


class SpotifyProfileRegistry:
    """Validated immutable lookup table for any number of Spotify profiles."""

    def __init__(
        self,
        profiles: Iterable[SpotifyProfile],
        *,
        default_profile_id: str | None = None,
    ) -> None:
        by_id: dict[str, SpotifyProfile] = {}
        aliases: dict[str, str] = {}
        people: dict[str, str] = {}

        for profile in profiles:
            if not isinstance(profile, SpotifyProfile):
                raise SpotifyRoutingConfigurationError(
                    "Die Profilverwaltung akzeptiert nur SpotifyProfile."
                )
            if profile.profile_id in by_id:
                raise SpotifyRoutingConfigurationError(
                    f"Doppelte Spotify-Profil-ID: {profile.profile_id}."
                )
            by_id[profile.profile_id] = profile

            names = (profile.profile_id, profile.display_name, *profile.aliases)
            for name in names:
                alias = normalize_alias(name)
                owner = aliases.get(alias)
                if owner is not None and owner != profile.profile_id:
                    raise SpotifyRoutingConfigurationError(
                        f"Mehrdeutiger Spotify-Profilalias: {name}."
                    )
                aliases[alias] = profile.profile_id

            for person_id in profile.alexa_person_ids:
                owner = people.get(person_id)
                if owner is not None and owner != profile.profile_id:
                    raise SpotifyRoutingConfigurationError(
                        "Eine Alexa-Personen-ID darf nur einem Spotify-Profil "
                        "zugeordnet sein."
                    )
                people[person_id] = profile.profile_id

        if not by_id:
            raise SpotifyRoutingConfigurationError(
                "Mindestens ein Spotify-Profil ist erforderlich."
            )
        if default_profile_id is not None:
            default = by_id.get(default_profile_id)
            if default is None or not default.enabled:
                raise SpotifyRoutingConfigurationError(
                    "Das Haushalts-Standardprofil muss vorhanden und aktiv sein."
                )

        self._profiles: Mapping[str, SpotifyProfile] = MappingProxyType(by_id)
        self._aliases: Mapping[str, str] = MappingProxyType(aliases)
        self._people: Mapping[str, str] = MappingProxyType(people)
        self.default_profile_id = default_profile_id

    @property
    def profiles(self) -> Mapping[str, SpotifyProfile]:
        """Return immutable profile metadata keyed by technical ID."""

        return self._profiles

    @property
    def enabled_profile_ids(self) -> tuple[str, ...]:
        """Return selectable profile IDs in configuration order."""

        return tuple(
            profile_id
            for profile_id, profile in self._profiles.items()
            if profile.enabled
        )

    def resolve_alias(self, alias: str) -> SpotifyProfile | None:
        """Resolve a spoken alias without exposing credentials."""

        profile_id = self._aliases.get(normalize_alias(alias))
        return self._profiles.get(profile_id) if profile_id is not None else None

    def profile_for_person(self, person_id: str) -> SpotifyProfile | None:
        """Resolve one pseudonymous Alexa person ID."""

        profile_id = self._people.get(person_id)
        return self._profiles.get(profile_id) if profile_id is not None else None


def load_spotify_profile_registry(
    path: str | Path,
    *,
    max_bytes: int = MAX_SPOTIFY_PROFILE_CONFIG_BYTES,
) -> SpotifyProfileRegistry:
    """Load non-secret profile metadata from a bounded local JSON file."""

    config_path = Path(path)
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 1:
        raise SpotifyRoutingConfigurationError(
            "Die maximale Spotify-Konfigurationsgröße muss positiv sein."
        )
    try:
        if not config_path.is_file():
            raise SpotifyRoutingConfigurationError(
                "Die lokale Spotify-Profilkonfiguration fehlt."
            )
        if config_path.stat().st_size > max_bytes:
            raise SpotifyRoutingConfigurationError(
                "Die lokale Spotify-Profilkonfiguration ist zu groß."
            )
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except SpotifyRoutingConfigurationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SpotifyRoutingConfigurationError(
            "Die lokale Spotify-Profilkonfiguration ist nicht lesbar."
        ) from exc

    return spotify_profile_registry_from_mapping(raw)


def spotify_profile_registry_from_mapping(
    raw: object,
) -> SpotifyProfileRegistry:
    """Build a registry from decoded JSON without accepting secret fields."""

    if not isinstance(raw, dict):
        raise SpotifyRoutingConfigurationError(
            "Die Spotify-Profilkonfiguration muss ein JSON-Objekt sein."
        )
    _reject_unknown_fields(raw, {"version", "default_profile_id", "profiles"})

    version = raw.get("version")
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version != SPOTIFY_PROFILE_CONFIG_VERSION
    ):
        raise SpotifyRoutingConfigurationError(
            "Die Spotify-Profilkonfigurationsversion wird nicht unterstützt."
        )

    profiles_raw = raw.get("profiles")
    if not isinstance(profiles_raw, list):
        raise SpotifyRoutingConfigurationError(
            "profiles muss eine JSON-Liste sein."
        )
    profiles = tuple(
        _spotify_profile_from_mapping(profile_raw) for profile_raw in profiles_raw
    )

    default_profile_id = raw.get("default_profile_id")
    if default_profile_id is not None:
        _require_text(default_profile_id, "Haushalts-Standardprofil")

    return SpotifyProfileRegistry(
        profiles,
        default_profile_id=default_profile_id,
    )


@dataclass(frozen=True, slots=True)
class SpotifyRoutingRequest:
    """Already parsed inputs for a single playback routing decision."""

    explicit_profile: str | None = None
    session_profile_id: str | None = None
    alexa_person_id: str | None = None
    requested_target: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "explicit_profile",
            "session_profile_id",
            "alexa_person_id",
            "requested_target",
        ):
            value = getattr(self, name)
            if value is not None:
                _require_text(value, name)


@dataclass(frozen=True, slots=True)
class SpotifyRoutingDecision:
    """Safe routing outcome; it never starts playback itself."""

    status: SpotifyRoutingStatus
    profile_id: str | None = None
    connection_id: str | None = None
    target_id: str | None = None
    source: SpotifyRoutingSource | None = None
    available_profile_ids: tuple[str, ...] = ()


def route_spotify_profile(
    registry: SpotifyProfileRegistry,
    request: SpotifyRoutingRequest,
) -> SpotifyRoutingDecision:
    """Select profile and target with explicit, safe precedence rules."""

    profile: SpotifyProfile | None
    source: SpotifyRoutingSource | None

    if request.explicit_profile is not None:
        profile = registry.resolve_alias(request.explicit_profile)
        source = SpotifyRoutingSource.EXPLICIT
        if profile is None or not profile.enabled:
            return _unresolved(
                SpotifyRoutingStatus.UNKNOWN_PROFILE,
                registry,
            )
    elif request.session_profile_id is not None:
        profile = registry.profiles.get(request.session_profile_id)
        source = SpotifyRoutingSource.SESSION
        if profile is None or not profile.enabled:
            return _unresolved(
                SpotifyRoutingStatus.UNKNOWN_PROFILE,
                registry,
            )
    elif request.alexa_person_id is not None:
        profile = registry.profile_for_person(request.alexa_person_id)
        source = SpotifyRoutingSource.VOICE_ID
        if profile is None or not profile.enabled:
            return _unresolved(SpotifyRoutingStatus.NEEDS_PROFILE, registry)
    elif registry.default_profile_id is not None:
        profile = registry.profiles[registry.default_profile_id]
        source = SpotifyRoutingSource.HOUSEHOLD_DEFAULT
    else:
        return _unresolved(SpotifyRoutingStatus.NEEDS_PROFILE, registry)

    target_id = request.requested_target or profile.default_target
    if target_id is not None and target_id not in profile.allowed_targets:
        return SpotifyRoutingDecision(
            status=SpotifyRoutingStatus.TARGET_NOT_ALLOWED,
            profile_id=profile.profile_id,
            source=source,
            available_profile_ids=registry.enabled_profile_ids,
        )

    return SpotifyRoutingDecision(
        status=SpotifyRoutingStatus.ROUTED,
        profile_id=profile.profile_id,
        connection_id=profile.connection_id,
        target_id=target_id,
        source=source,
        available_profile_ids=registry.enabled_profile_ids,
    )


def normalize_alias(value: str) -> str:
    """Normalize a configured or spoken profile alias."""

    _require_text(value, "Profilalias")
    return " ".join(value.casefold().split())


def _unresolved(
    status: SpotifyRoutingStatus,
    registry: SpotifyProfileRegistry,
) -> SpotifyRoutingDecision:
    return SpotifyRoutingDecision(
        status=status,
        available_profile_ids=registry.enabled_profile_ids,
    )


def _spotify_profile_from_mapping(raw: object) -> SpotifyProfile:
    if not isinstance(raw, dict):
        raise SpotifyRoutingConfigurationError(
            "Jedes Spotify-Profil muss ein JSON-Objekt sein."
        )
    _reject_unknown_fields(raw, PROFILE_CONFIG_FIELDS)
    for field_name in ("profile_id", "display_name", "connection_id"):
        if field_name not in raw:
            raise SpotifyRoutingConfigurationError(
                f"Dem Spotify-Profil fehlt das Feld {field_name}."
            )

    default_target = raw.get("default_target")
    if default_target is not None:
        _require_text(default_target, "Standard-Wiedergabeziel")

    return SpotifyProfile(
        profile_id=_required_config_text(raw, "profile_id"),
        display_name=_required_config_text(raw, "display_name"),
        connection_id=_required_config_text(raw, "connection_id"),
        aliases=_config_text_tuple(raw, "aliases"),
        alexa_person_ids=_config_text_tuple(raw, "alexa_person_ids"),
        allowed_targets=_config_text_tuple(raw, "allowed_targets"),
        default_target=default_target,
        enabled=raw.get("enabled", True),
    )


def _reject_unknown_fields(
    raw: Mapping[str, object],
    allowed_fields: set[str] | frozenset[str],
) -> None:
    unknown = set(raw) - allowed_fields
    if unknown:
        fields = ", ".join(sorted(str(field) for field in unknown))
        raise SpotifyRoutingConfigurationError(
            f"Unbekannte Felder in der Spotify-Profilkonfiguration: {fields}."
        )


def _required_config_text(raw: Mapping[str, object], field_name: str) -> str:
    value = raw[field_name]
    _require_text(value, field_name)
    return value


def _config_text_tuple(
    raw: Mapping[str, object],
    field_name: str,
) -> tuple[str, ...]:
    value = raw.get(field_name, [])
    if not isinstance(value, list):
        raise SpotifyRoutingConfigurationError(
            f"{field_name} muss eine JSON-Liste sein."
        )
    for item in value:
        _require_text(item, field_name)
    return tuple(value)


def _require_text(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise SpotifyRoutingConfigurationError(f"{label} darf nicht leer sein.")


def _unique_texts(
    values: tuple[str, ...],
    label: str,
    *,
    normalize: bool,
) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise SpotifyRoutingConfigurationError(f"{label}-Werte müssen ein Tupel sein.")
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        _require_text(value, label)
        item = normalize_alias(value) if normalize else value.strip()
        if item in seen:
            raise SpotifyRoutingConfigurationError(f"Doppelter {label}: {value}.")
        seen.add(item)
        result.append(item)
    return tuple(result)
