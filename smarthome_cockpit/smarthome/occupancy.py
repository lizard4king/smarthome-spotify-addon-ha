"""Pure, side-effect-free household presence and mode decisions."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping


class Presence(StrEnum):
    """Observed presence of one configured household member."""

    HOME = "home"
    AWAY = "away"
    UNKNOWN = "unknown"


class HomeMode(StrEnum):
    """Mutually exclusive operating mode selected for the home."""

    PARTY = "party"
    GUEST = "guest"
    HOME = "home"
    UNCERTAIN = "uncertain"
    AWAY = "away"


@dataclass(frozen=True, slots=True)
class HouseholdConfig:
    """Validated, provider-neutral household metadata."""

    persons: tuple[str, ...]
    aliases: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    zones: tuple[str, ...] = ()
    presence_sources: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    mode_priorities: tuple[HomeMode, ...] = (
        HomeMode.PARTY,
        HomeMode.GUEST,
        HomeMode.HOME,
        HomeMode.UNCERTAIN,
        HomeMode.AWAY,
    )

    def __post_init__(self) -> None:
        persons = tuple(name.strip() for name in self.persons)
        if not persons or len(set(persons)) != len(persons) or any(not name for name in persons):
            raise ValueError("Die Personenliste muss eindeutige Namen enthalten.")
        if len(set(self.mode_priorities)) != len(self.mode_priorities):
            raise ValueError("Modusprioritäten dürfen nicht doppelt vorkommen.")
        if set(self.mode_priorities) != set(HomeMode):
            raise ValueError("Alle Home-Modi müssen priorisiert werden.")
        if set(self.aliases) - set(persons) or set(self.presence_sources) - set(persons):
            raise ValueError("Aliases und Anwesenheitsquellen müssen Personen zugeordnet sein.")
        object.__setattr__(self, "persons", persons)
        object.__setattr__(self, "aliases", MappingProxyType(dict(self.aliases)))
        object.__setattr__(self, "presence_sources", MappingProxyType(dict(self.presence_sources)))


@dataclass(frozen=True, slots=True)
class HouseholdContext:
    """Inputs used to select a home mode without controlling any device."""

    presence: Mapping[str, Presence]
    active_modes: Mapping[HomeMode, bool] = field(default_factory=dict)
    mode_priorities: tuple[HomeMode, ...] = (
        HomeMode.PARTY,
        HomeMode.GUEST,
        HomeMode.HOME,
        HomeMode.UNCERTAIN,
        HomeMode.AWAY,
    )

    def __post_init__(self) -> None:
        if not self.presence:
            raise ValueError("Mindestens eine Person muss konfiguriert sein.")
        if any(not isinstance(status, Presence) for status in self.presence.values()):
            raise ValueError("Ungültiger Anwesenheitsstatus.")
        if set(self.mode_priorities) != set(HomeMode):
            raise ValueError("Alle Home-Modi müssen priorisiert werden.")
        if any(not isinstance(active, bool) for active in self.active_modes.values()):
            raise ValueError("Modusstatus muss boolesch sein.")

    @property
    def people_home(self) -> tuple[str, ...]:
        """Return configured household members currently reported at home."""

        return tuple(
            name for name, status in self.presence.items() if status is Presence.HOME
        )


def select_home_mode(context: HouseholdContext) -> HomeMode:
    """Select a conservative mode from configured presence and overrides."""

    for mode in context.mode_priorities:
        if mode in {HomeMode.HOME, HomeMode.UNCERTAIN, HomeMode.AWAY}:
            continue
        if context.active_modes.get(mode, False):
            return mode
    if context.people_home:
        return HomeMode.HOME
    if any(status is Presence.UNKNOWN for status in context.presence.values()):
        return HomeMode.UNCERTAIN
    return HomeMode.AWAY
