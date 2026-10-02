"""Pure parsing boundary for structured Spotify Alexa intents."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping


class SpotifyAlexaCommandError(ValueError):
    """Raised when an Alexa Spotify command is incomplete or malformed."""


class SpotifyAlexaIntent(StrEnum):
    PLAY = "SpotifyPlayIntent"
    PROFILE = "SpotifySelectProfileIntent"
    PLAYBACK = "SpotifyPlaybackIntent"
    STOP = "SpotifyStopIntent"


@dataclass(frozen=True, slots=True)
class SpotifyAlexaCommand:
    """Validated, token-free command data ready for the Spotify service."""

    intent: SpotifyAlexaIntent
    media_query: str | None = None
    profile_alias: str | None = None
    target_alias: str | None = None
    action: str | None = None
    remember_for_session: bool = False

    def __post_init__(self) -> None:
        if self.intent is SpotifyAlexaIntent.PLAY and not self.media_query:
            raise SpotifyAlexaCommandError("Dem Wiedergabebefehl fehlt der Inhalt.")
        if self.intent is SpotifyAlexaIntent.PROFILE and not self.profile_alias:
            raise SpotifyAlexaCommandError("Dem Profilbefehl fehlt der Profilname.")
        if self.intent is SpotifyAlexaIntent.PLAYBACK and not self.action:
            raise SpotifyAlexaCommandError("Dem Wiedergabebefehl fehlt die Aktion.")
        if self.remember_for_session and not self.profile_alias:
            raise SpotifyAlexaCommandError(
                "Eine Sitzungswahl braucht ein ausdrücklich genanntes Profil."
            )
        for value in (self.media_query, self.profile_alias, self.target_alias, self.action):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise SpotifyAlexaCommandError("Spotify-Slots dürfen nicht leer sein.")


def parse_spotify_alexa_intent(intent: Mapping[str, object]) -> SpotifyAlexaCommand:
    """Parse only named Spotify intents; never invokes a service or an LLM."""

    if not isinstance(intent, Mapping):
        raise SpotifyAlexaCommandError("Der Spotify-Intent ist ungültig.")
    try:
        intent_name = SpotifyAlexaIntent(intent.get("name"))
    except (TypeError, ValueError):
        raise SpotifyAlexaCommandError("Der Spotify-Intent ist nicht zugelassen.") from None
    slots = intent.get("slots", {})
    if not isinstance(slots, Mapping):
        raise SpotifyAlexaCommandError("Spotify-Slots sind ungültig.")

    def slot(name: str) -> str | None:
        value = slots.get(name)
        if not isinstance(value, Mapping):
            return None
        spoken = value.get("value")
        return spoken.strip() if isinstance(spoken, str) and spoken.strip() else None

    remember = (slot("RememberForSession") or "").casefold() in {
        "ja", "yes", "wahr", "true"
    }
    return SpotifyAlexaCommand(
        intent=intent_name,
        media_query=slot("MediaQuery"),
        profile_alias=slot("ProfileAlias"),
        target_alias=slot("TargetAlias"),
        action=slot("Action"),
        remember_for_session=remember,
    )
