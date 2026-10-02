"""Minimal, offline-only Alexa exposure policies for Home Assistant."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


LIGHT_ENTITY_PATTERN = re.compile(r"^light\.[a-z0-9_]+$")
EXPOSED_DEVICE_IDS = ("wohnzimmerlicht", "schlafzimmerlicht")
FRIENDLY_NAMES = MappingProxyType(
    {
        "wohnzimmerlicht": "Wohnzimmerlicht",
        "schlafzimmerlicht": "Schlafzimmerlicht",
    }
)


class AlexaExposureConfigurationError(ValueError):
    """Raised when an Alexa exposure plan is broader than intended."""


@dataclass(frozen=True, slots=True)
class AlexaExposurePolicy:
    """Represent the exact Home Assistant entities that Alexa may discover.

    This class only builds local configuration. It performs no network request,
    changes no Home Assistant setting, and never links an Amazon account.
    """

    entities: Mapping[str, str]

    def __post_init__(self) -> None:
        entities = dict(self.entities)
        if set(entities) != set(EXPOSED_DEVICE_IDS):
            raise AlexaExposureConfigurationError(
                "Alexa darf ausschließlich die zwei vorgesehenen Lampen erhalten."
            )

        entity_ids = tuple(entities.values())
        if len(set(entity_ids)) != len(entity_ids):
            raise AlexaExposureConfigurationError(
                "Alexa-Entity-IDs müssen eindeutig sein."
            )
        for device_id, entity_id in entities.items():
            if not isinstance(entity_id, str) or not LIGHT_ENTITY_PATTERN.fullmatch(
                entity_id
            ):
                raise AlexaExposureConfigurationError(
                    f"Ungültige Alexa-Licht-Entity für {device_id}."
                )

        normalized = {key: entities[key] for key in EXPOSED_DEVICE_IDS}
        object.__setattr__(self, "entities", MappingProxyType(normalized))

    @classmethod
    def from_environment(cls) -> AlexaExposurePolicy:
        """Read only the two approved light mappings from the environment."""

        return cls(
            {
                "wohnzimmerlicht": os.getenv(
                    "HOME_ASSISTANT_WOHNZIMMERLICHT", ""
                ),
                "schlafzimmerlicht": os.getenv(
                    "HOME_ASSISTANT_SCHLAFZIMMERLICHT", ""
                ),
            }
        )

    def to_home_assistant_cloud_config(self) -> dict[str, object]:
        """Return the legacy Home Assistant Cloud configuration structure."""

        include_entities = [self.entities[key] for key in EXPOSED_DEVICE_IDS]
        entity_config = {
            self.entities[key]: {"name": FRIENDLY_NAMES[key]}
            for key in EXPOSED_DEVICE_IDS
        }
        return {
            "cloud": {
                "alexa": {
                    "filter": {"include_entities": include_entities},
                    "entity_config": entity_config,
                }
            }
        }

    def to_home_assistant_manual_config(self) -> dict[str, object]:
        """Return a minimal configuration for a self-hosted Alexa skill."""

        include_entities = [self.entities[key] for key in EXPOSED_DEVICE_IDS]
        entity_config = {
            self.entities[key]: {"name": FRIENDLY_NAMES[key]}
            for key in EXPOSED_DEVICE_IDS
        }
        return {
            "alexa": {
                "smart_home": {
                    "locale": "de-DE",
                    "filter": {"include_entities": include_entities},
                    "entity_config": entity_config,
                }
            }
        }

    def render_home_assistant_cloud_yaml(self) -> str:
        """Render the legacy Home Assistant Cloud allowlist."""

        lines = [
            "cloud:",
            "  alexa:",
            "    filter:",
            "      include_entities:",
        ]
        lines.extend(
            f"        - {self.entities[key]}" for key in EXPOSED_DEVICE_IDS
        )
        lines.append("    entity_config:")
        for key in EXPOSED_DEVICE_IDS:
            lines.extend(
                (
                    f"      {self.entities[key]}:",
                    f"        name: {FRIENDLY_NAMES[key]}",
                )
            )
        return "\n".join(lines) + "\n"

    def render_home_assistant_manual_yaml(self) -> str:
        """Render the reviewed allowlist for a manual Smart Home skill."""

        lines = [
            "alexa:",
            "  smart_home:",
            "    locale: de-DE",
            "    filter:",
            "      include_entities:",
        ]
        lines.extend(
            f"        - {self.entities[key]}" for key in EXPOSED_DEVICE_IDS
        )
        lines.append("    entity_config:")
        for key in EXPOSED_DEVICE_IDS:
            lines.extend(
                (
                    f"      {self.entities[key]}:",
                    f"        name: {FRIENDLY_NAMES[key]}",
                )
            )
        return "\n".join(lines) + "\n"
