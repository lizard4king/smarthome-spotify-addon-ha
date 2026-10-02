"""Validated, secret-free SmartHome deployment profiles."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


class ProfileConfigurationError(ValueError):
    """Raised when a SmartHome profile is incomplete or contains secrets."""


SECRET_KEYS = frozenset(
    {"token", "password", "secret", "api_key", "client_secret", "access_token"}
)


@dataclass(frozen=True, slots=True)
class SmartHomeProfile:
    """Normalized profile structure used by deployment and adapters."""

    version: int
    household: Mapping[str, Any]
    devices: tuple[Mapping[str, Any], ...]
    services: Mapping[str, Any]
    policies: Mapping[str, Any]

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> SmartHomeProfile:
        if not isinstance(raw, Mapping):
            raise ProfileConfigurationError("Das SmartHome-Profil muss ein Objekt sein.")
        _reject_secret_keys(raw)
        version = raw.get("version")
        household = raw.get("household")
        devices = raw.get("devices")
        services = raw.get("services", {})
        policies = raw.get("policies", {})
        if version != 1:
            raise ProfileConfigurationError("Nur Profilversion 1 wird unterstützt.")
        if not isinstance(household, Mapping) or not isinstance(services, Mapping):
            raise ProfileConfigurationError("Haushalt und Services müssen Objekte sein.")
        if not isinstance(devices, list) or not devices:
            raise ProfileConfigurationError("Das Profil benötigt mindestens ein Gerät.")
        if not isinstance(policies, Mapping):
            raise ProfileConfigurationError("Policies müssen ein Objekt sein.")
        _validate_household(household)
        normalized_devices = tuple(_validate_device(device) for device in devices)
        return cls(version, household, normalized_devices, services, policies)

    @classmethod
    def from_json(cls, path: str | Path) -> SmartHomeProfile:
        profile_path = Path(path)
        try:
            raw = json.loads(profile_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ProfileConfigurationError("SmartHome-Profil wurde nicht gefunden.") from exc
        except json.JSONDecodeError as exc:
            raise ProfileConfigurationError("SmartHome-Profil enthält kein gültiges JSON.") from exc
        return cls.from_mapping(raw)


def _reject_secret_keys(value: Any, path: str = "profile") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if normalized in SECRET_KEYS or normalized.endswith("_token"):
                raise ProfileConfigurationError(
                    f"Secrets dürfen nicht im Profil stehen: {path}.{key}"
                )
            _reject_secret_keys(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_secret_keys(child, f"{path}[{index}]")


def _validate_household(household: Mapping[str, Any]) -> None:
    persons = household.get("persons")
    zones = household.get("zones")
    if not isinstance(persons, list) or not persons:
        raise ProfileConfigurationError("household.persons benötigt mindestens eine Person.")
    if not isinstance(zones, list) or not zones:
        raise ProfileConfigurationError("household.zones benötigt mindestens einen Raum.")
    person_ids = []
    for person in persons:
        if not isinstance(person, Mapping) or not isinstance(person.get("id"), str):
            raise ProfileConfigurationError("Jede Person benötigt eine ID.")
        person_ids.append(person["id"])
    if len(set(person_ids)) != len(person_ids):
        raise ProfileConfigurationError("Personen-IDs müssen eindeutig sein.")
    if len(set(zones)) != len(zones) or not all(isinstance(zone, str) for zone in zones):
        raise ProfileConfigurationError("Raumnamen müssen eindeutig und Text sein.")


def _validate_device(device: Any) -> Mapping[str, Any]:
    if not isinstance(device, Mapping):
        raise ProfileConfigurationError("Jedes Gerät muss ein Objekt sein.")
    for field in ("id", "type", "zone"):
        if not isinstance(device.get(field), str) or not device[field].strip():
            raise ProfileConfigurationError(f"Gerätefeld {field} fehlt.")
    capabilities = device.get("capabilities", [])
    if not isinstance(capabilities, list) or not all(
        isinstance(item, str) for item in capabilities
    ):
        raise ProfileConfigurationError("Gerätefähigkeiten müssen eine Textliste sein.")
    return dict(device)
