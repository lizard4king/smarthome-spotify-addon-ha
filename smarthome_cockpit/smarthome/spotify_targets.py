"""Local logical targets resolved against the current Spotify Connect catalog."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping


TARGET_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SPOTIFY_TARGET_CONFIG_VERSION = 1
MAX_SPOTIFY_TARGET_CONFIG_BYTES = 32 * 1024


class SpotifyTargetConfigurationError(ValueError):
    """Raised when local playback-target configuration is unsafe."""


@dataclass(frozen=True, slots=True)
class SpotifyPlaybackTarget:
    """Non-secret target metadata; household device name stays out of repr."""

    target_id: str
    spotify_device_name: str = field(repr=False)
    aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.target_id, str) or not TARGET_ID_PATTERN.fullmatch(
            self.target_id
        ):
            raise SpotifyTargetConfigurationError(
                "Die Spotify-Ziel-ID muss ein stabiler technischer Name sein."
            )
        _require_text(self.spotify_device_name, "Spotify-Gerätename")
        if not isinstance(self.aliases, tuple):
            raise SpotifyTargetConfigurationError("Spotify-Zielaliasse müssen ein Tupel sein.")
        normalized = {_normalize_name(self.spotify_device_name)}
        for alias in self.aliases:
            _require_text(alias, "Spotify-Zielalias")
            key = _normalize_name(alias)
            if key in normalized:
                raise SpotifyTargetConfigurationError("Doppelter Spotify-Zielalias.")
            normalized.add(key)


class SpotifyTargetRegistry:
    """Validated immutable mapping shared by every Spotify profile."""

    def __init__(self, targets: Iterable[SpotifyPlaybackTarget]) -> None:
        by_id: dict[str, SpotifyPlaybackTarget] = {}
        device_names: set[str] = set()
        for target in targets:
            if not isinstance(target, SpotifyPlaybackTarget):
                raise SpotifyTargetConfigurationError(
                    "Die Zielverwaltung akzeptiert nur SpotifyPlaybackTarget."
                )
            if target.target_id in by_id:
                raise SpotifyTargetConfigurationError(
                    "Die Spotify-Ziel-ID ist doppelt vergeben."
                )
            normalized_name = _normalize_name(target.spotify_device_name)
            if normalized_name in device_names:
                raise SpotifyTargetConfigurationError(
                    "Der Spotify-Gerätename ist doppelt vergeben."
                )
            by_id[target.target_id] = target
            device_names.add(normalized_name)
        if not by_id:
            raise SpotifyTargetConfigurationError(
                "Mindestens ein Spotify-Wiedergabeziel ist erforderlich."
            )
        self._targets: Mapping[str, SpotifyPlaybackTarget] = MappingProxyType(by_id)

    def require(self, target_id: str) -> SpotifyPlaybackTarget:
        if not isinstance(target_id, str):
            raise SpotifyTargetConfigurationError(
                "Das Spotify-Wiedergabeziel ist ungültig."
            )
        target = self._targets.get(target_id)
        if target is None:
            raise SpotifyTargetConfigurationError(
                "Das Spotify-Wiedergabeziel ist nicht konfiguriert."
            )
        return target

    def resolve_alias(self, alias: str) -> str | None:
        """Resolve a spoken target name or stable target ID to its ID."""

        if not isinstance(alias, str) or not alias.strip():
            return None
        normalized = _normalize_name(alias)
        for target_id, target in self._targets.items():
            if normalized in {
                _normalize_name(target_id),
                _normalize_name(target.spotify_device_name),
                *(_normalize_name(item) for item in target.aliases),
            }:
                return target_id
        return None

    def match_device_name(self, target_id: str, device_name: str) -> bool:
        """Match a refreshed Connect name against the logical target aliases."""

        target = self.require(target_id)
        normalized = _normalize_name(device_name)
        return normalized in {
            _normalize_name(target.spotify_device_name),
            *(_normalize_name(alias) for alias in target.aliases),
        }


def load_spotify_target_registry(
    path: str | Path,
    *,
    max_bytes: int = MAX_SPOTIFY_TARGET_CONFIG_BYTES,
) -> SpotifyTargetRegistry:
    """Load bounded non-secret target metadata from a local JSON file."""

    config_path = Path(path)
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 1:
        raise SpotifyTargetConfigurationError(
            "Die maximale Spotify-Zielkonfigurationsgröße muss positiv sein."
        )
    try:
        if not config_path.is_file():
            raise SpotifyTargetConfigurationError(
                "Die lokale Spotify-Zielkonfiguration fehlt."
            )
        if config_path.stat().st_size > max_bytes:
            raise SpotifyTargetConfigurationError(
                "Die lokale Spotify-Zielkonfiguration ist zu groß."
            )
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except SpotifyTargetConfigurationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SpotifyTargetConfigurationError(
            "Die lokale Spotify-Zielkonfiguration ist nicht lesbar."
        ) from exc
    return spotify_target_registry_from_mapping(raw)


def spotify_target_registry_from_mapping(raw: object) -> SpotifyTargetRegistry:
    if not isinstance(raw, dict) or set(raw) != {"version", "targets"}:
        raise SpotifyTargetConfigurationError(
            "Die Spotify-Zielkonfiguration ist ungültig."
        )
    version = raw["version"]
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version != SPOTIFY_TARGET_CONFIG_VERSION
    ):
        raise SpotifyTargetConfigurationError(
            "Die Spotify-Zielkonfigurationsversion wird nicht unterstützt."
        )
    entries = raw["targets"]
    if not isinstance(entries, list):
        raise SpotifyTargetConfigurationError("targets muss eine JSON-Liste sein.")
    targets: list[SpotifyPlaybackTarget] = []
    for entry in entries:
        if not isinstance(entry, dict) or not {"target_id", "spotify_device_name"}.issubset(entry):
            raise SpotifyTargetConfigurationError(
                "Ein Spotify-Wiedergabeziel ist ungültig."
            )
        if set(entry) - {"target_id", "spotify_device_name", "aliases"}:
            raise SpotifyTargetConfigurationError("Ein Spotify-Wiedergabeziel ist ungültig.")
        aliases = entry.get("aliases", [])
        if not isinstance(aliases, list):
            raise SpotifyTargetConfigurationError("Spotify-Zielaliasse müssen eine Liste sein.")
        targets.append(
            SpotifyPlaybackTarget(
                target_id=entry["target_id"],
                spotify_device_name=entry["spotify_device_name"],
                aliases=tuple(aliases),
            )
        )
    return SpotifyTargetRegistry(targets)


def _require_text(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise SpotifyTargetConfigurationError(f"{label} darf nicht leer sein.")


def _normalize_name(value: str) -> str:
    return " ".join(value.casefold().split())
