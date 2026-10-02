"""Side-effect-free Alexa personalization boundary for Spotify routing."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from smarthome.spotify_routing import (
    SpotifyProfileRegistry,
    SpotifyRoutingConfigurationError,
    SpotifyRoutingDecision,
    SpotifyRoutingRequest,
    SpotifyRoutingStatus,
    route_spotify_profile,
)


class AlexaVoiceIdentityStatus(StrEnum):
    """Locally classified Alexa personalization state."""

    VERIFIED = "verified"
    ABSENT = "absent"
    NOT_AUTHORIZED = "not_authorized"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class AlexaVoiceIdentity:
    """Pseudonymous identity result; it never contains a person's name."""

    status: AlexaVoiceIdentityStatus
    person_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, AlexaVoiceIdentityStatus):
            raise SpotifyRoutingConfigurationError(
                "Der Alexa-Personalisierungsstatus ist ungültig."
            )
        if self.status is AlexaVoiceIdentityStatus.VERIFIED:
            _require_optional_text(self.person_id, required=True)
        elif self.person_id is not None:
            raise SpotifyRoutingConfigurationError(
                "Eine nicht verifizierte Stimme darf keine Personen-ID liefern."
            )


@dataclass(frozen=True, slots=True)
class SpotifySkillSession:
    """Ephemeral profile choice for one Alexa skill session."""

    profile_id: str | None = None

    def __post_init__(self) -> None:
        if self.profile_id is not None:
            _require_optional_text(self.profile_id, required=True)


@dataclass(frozen=True, slots=True)
class AlexaSpotifyRoutingRequest:
    """Structured Alexa slots accepted by the local routing boundary."""

    voice_identity: AlexaVoiceIdentity
    explicit_profile: str | None = None
    requested_target: str | None = None
    remember_for_session: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.voice_identity, AlexaVoiceIdentity):
            raise SpotifyRoutingConfigurationError(
                "Die Alexa-Stimminformation fehlt."
            )
        if self.explicit_profile is not None:
            _require_optional_text(self.explicit_profile, required=True)
        if self.requested_target is not None:
            _require_optional_text(self.requested_target, required=True)
        if not isinstance(self.remember_for_session, bool):
            raise SpotifyRoutingConfigurationError(
                "Die Sitzungswahl muss boolesch sein."
            )
        if self.remember_for_session and self.explicit_profile is None:
            raise SpotifyRoutingConfigurationError(
                "Nur ein ausdrücklich genanntes Profil kann für die Sitzung gelten."
            )


@dataclass(frozen=True, slots=True)
class AlexaSpotifyRoutingResult:
    """Routing decision plus the updated ephemeral session."""

    decision: SpotifyRoutingDecision
    session: SpotifySkillSession


def route_alexa_spotify_request(
    registry: SpotifyProfileRegistry,
    request: AlexaSpotifyRoutingRequest,
    session: SpotifySkillSession | None = None,
) -> AlexaSpotifyRoutingResult:
    """Apply Alexa identity rules before generic Spotify profile routing."""

    active_session = session or SpotifySkillSession()
    has_direct_selection = (
        request.explicit_profile is not None or active_session.profile_id is not None
    )
    if (
        not has_direct_selection
        and request.voice_identity.status is not AlexaVoiceIdentityStatus.VERIFIED
    ):
        return AlexaSpotifyRoutingResult(
            decision=SpotifyRoutingDecision(
                status=SpotifyRoutingStatus.NEEDS_PROFILE,
                available_profile_ids=registry.enabled_profile_ids,
            ),
            session=active_session,
        )

    person_id = (
        request.voice_identity.person_id
        if request.voice_identity.status is AlexaVoiceIdentityStatus.VERIFIED
        else None
    )
    decision = route_spotify_profile(
        registry,
        SpotifyRoutingRequest(
            explicit_profile=request.explicit_profile,
            session_profile_id=active_session.profile_id,
            alexa_person_id=person_id,
            requested_target=request.requested_target,
        ),
    )
    updated_session = active_session
    if request.remember_for_session and decision.status is SpotifyRoutingStatus.ROUTED:
        updated_session = SpotifySkillSession(profile_id=decision.profile_id)

    return AlexaSpotifyRoutingResult(decision=decision, session=updated_session)


def _require_optional_text(value: object, *, required: bool) -> None:
    if required and (not isinstance(value, str) or not value.strip()):
        raise SpotifyRoutingConfigurationError("Ein Pflichtwert darf nicht leer sein.")
