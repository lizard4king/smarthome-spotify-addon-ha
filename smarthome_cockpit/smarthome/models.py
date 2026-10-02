"""Validated domain models for simulated smart-home devices."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


MIN_TEMPERATURE = 10.0
MAX_TEMPERATURE = 30.0


class DeviceType(StrEnum):
    """Supported simulated device types."""

    LIGHT = "light"
    THERMOSTAT = "thermostat"


@dataclass(slots=True)
class Device:
    """A simulated smart-home device and its current state."""

    device_id: str
    name: str
    device_type: DeviceType
    is_on: bool | None = None
    target_temperature: float | None = None

    def __post_init__(self) -> None:
        if self.device_type is DeviceType.LIGHT:
            if not isinstance(self.is_on, bool):
                raise ValueError("Ein Licht benoetigt einen booleschen Zustand.")
            if self.target_temperature is not None:
                raise ValueError("Ein Licht darf keine Solltemperatur haben.")
            return

        if self.is_on is not None:
            raise ValueError("Ein Thermostat darf keinen Lichtzustand haben.")
        if self.target_temperature is None:
            raise ValueError("Ein Thermostat benoetigt eine Solltemperatur.")
        self.target_temperature = validate_temperature(self.target_temperature)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""

        data: dict[str, Any] = {
            "name": self.name,
            "type": self.device_type.value,
        }
        if self.device_type is DeviceType.LIGHT:
            data["is_on"] = self.is_on
        else:
            data["target_temperature"] = self.target_temperature
        return data

    @classmethod
    def from_dict(cls, device_id: str, data: dict[str, Any]) -> Device:
        """Create and validate a device from persisted JSON data."""

        try:
            device_type = DeviceType(data["type"])
            name = str(data["name"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Ungueltige Geraetedaten fuer {device_id}.") from exc

        if device_type is DeviceType.LIGHT:
            is_on = data.get("is_on")
            if not isinstance(is_on, bool):
                raise ValueError(f"Ungueltiger Lichtzustand fuer {device_id}.")
            return cls(device_id, name, device_type, is_on=is_on)

        temperature = data.get("target_temperature")
        if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
            raise ValueError(f"Ungueltige Solltemperatur fuer {device_id}.")
        return cls(device_id, name, device_type, target_temperature=float(temperature))


@dataclass(slots=True)
class SmartHomeState:
    """Complete persisted application state."""

    devices: dict[str, Device]
    action_log: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""

        return {
            "devices": {
                device_id: device.to_dict()
                for device_id, device in self.devices.items()
            },
            "action_log": list(self.action_log),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SmartHomeState:
        """Create and validate a complete state from JSON data."""

        devices_data = data.get("devices")
        action_log = data.get("action_log", [])
        if not isinstance(devices_data, dict) or not isinstance(action_log, list):
            raise ValueError("Ungueltiges Zustandsformat.")
        if not all(isinstance(item, str) for item in action_log):
            raise ValueError("Das Systemprotokoll enthaelt ungueltige Eintraege.")

        devices = {
            str(device_id): Device.from_dict(str(device_id), device_data)
            for device_id, device_data in devices_data.items()
            if isinstance(device_data, dict)
        }
        if len(devices) != len(devices_data):
            raise ValueError("Mindestens ein Geraet ist ungueltig.")
        return cls(devices=devices, action_log=action_log)


def validate_temperature(value: float) -> float:
    """Validate and normalize a thermostat target temperature."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Die Temperatur muss eine Zahl sein.")
    temperature = round(float(value), 1)
    if not MIN_TEMPERATURE <= temperature <= MAX_TEMPERATURE:
        raise ValueError(
            f"Die Temperatur muss zwischen {MIN_TEMPERATURE:.1f} und "
            f"{MAX_TEMPERATURE:.1f} Grad liegen."
        )
    return temperature


def default_state() -> SmartHomeState:
    """Create the defined initial state for all simulated devices."""

    return SmartHomeState(
        devices={
            "wohnzimmerlicht": Device(
                "wohnzimmerlicht", "Wohnzimmerlicht", DeviceType.LIGHT, is_on=False
            ),
            "schlafzimmerlicht": Device(
                "schlafzimmerlicht", "Schlafzimmerlicht", DeviceType.LIGHT, is_on=False
            ),
            "wohnzimmerheizung": Device(
                "wohnzimmerheizung",
                "Wohnzimmerheizung",
                DeviceType.THERMOSTAT,
                target_temperature=21.0,
            ),
        }
    )

