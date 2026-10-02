"""Deterministic parser for the supported German smart-home commands."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal


ActionName = Literal["set_light", "set_temperature", "get_status"]


@dataclass(frozen=True, slots=True)
class CommandAction:
    """A structured and narrowly scoped action returned by the parser."""

    action: ActionName
    device_id: str | None = None
    value: bool | float | None = None
    detail: Literal["all", "active_devices"] | None = None


class CommandParseError(ValueError):
    """Raised when user text does not match a supported command."""


def parse_command(command: str) -> CommandAction:
    """Parse one supported German command into a validated action."""

    normalized = _normalize(command)
    if not normalized:
        raise CommandParseError("Bitte gib einen Befehl ein.")

    if normalized in {"wie ist der status", "status", "status anzeigen"}:
        return CommandAction("get_status", detail="all")
    if normalized in {
        "welche geraete sind eingeschaltet",
        "welche geräte sind eingeschaltet",
    }:
        return CommandAction("get_status", detail="active_devices")

    light_match = re.search(
        r"\b(wohnzimmerlicht|schlafzimmerlicht)\b.*\b(ein|an|aus|einschalten|anschalten|ausschalten)\b$",
        normalized,
    )
    if light_match:
        value = light_match.group(2) in {"ein", "an", "einschalten", "anschalten"}
        return CommandAction("set_light", light_match.group(1), value)

    temperature_match = re.search(
        r"\b(?:wohnzimmerheizung|heizung im wohnzimmer|heizung)\b.*?"
        r"(?:auf\s+)?(-?\d+(?:[,.]\d+)?)\s*(?:grad|°c|c)?$",
        normalized,
    )
    if temperature_match:
        value = float(temperature_match.group(1).replace(",", "."))
        if not 10.0 <= value <= 30.0:
            raise CommandParseError(
                "Die Temperatur muss zwischen 10,0 und 30,0 Grad liegen."
            )
        return CommandAction("set_temperature", "wohnzimmerheizung", value)

    raise CommandParseError(
        "Befehl nicht erkannt. Nutze einen Licht-, Heizungs- oder Statusbefehl."
    )


def _normalize(command: str) -> str:
    text = command.casefold().strip()
    text = re.sub(r"[.!?;:]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()

