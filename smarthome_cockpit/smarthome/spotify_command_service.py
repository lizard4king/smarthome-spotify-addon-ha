"""Execution boundary for validated, structured Spotify commands."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from smarthome.alexa_spotify import (
    AlexaSpotifyRoutingRequest,
    AlexaVoiceIdentity,
    SpotifySkillSession,
    route_alexa_spotify_request,
)
from smarthome.spotify_alexa_commands import SpotifyAlexaCommand
from smarthome.spotify_playback import SpotifyPlaybackRequest
from smarthome.spotify_routing import SpotifyProfileRegistry, SpotifyRoutingStatus
from smarthome.spotify_service import SpotifyPlaybackService, prepare_spotify_playback
from smarthome.spotify_targets import SpotifyTargetRegistry


class SpotifyMediaResolver(Protocol):
    """Resolve spoken media to a validated Spotify playback request."""

    def resolve(self, query: str, *, access_token: str) -> SpotifyPlaybackRequest:
        """Return one explicit Spotify context or track list."""


class SpotifyCommandServiceError(RuntimeError):
    """Raised when a command cannot be safely executed."""


@dataclass(frozen=True, slots=True)
class SpotifyCommandResult:
    """Outcome that can be converted to a short Alexa response."""

    status: SpotifyRoutingStatus
    profile_id: str | None = None
    target_id: str | None = None


class SpotifyCommandService:
    """Route and execute Spotify commands without an LLM or implicit fallback."""

    def __init__(
        self,
        registry: SpotifyProfileRegistry,
        targets: SpotifyTargetRegistry,
        token_provider: object,
        media_resolver: SpotifyMediaResolver,
        playback_service: SpotifyPlaybackService,
    ) -> None:
        self._registry = registry
        self._targets = targets
        self._token_provider = token_provider
        self._media_resolver = media_resolver
        self._playback_service = playback_service

    def play(
        self,
        command: SpotifyAlexaCommand,
        *,
        voice_identity: AlexaVoiceIdentity,
        session: SpotifySkillSession | None = None,
        now: datetime,
    ) -> tuple[SpotifyCommandResult, SpotifySkillSession]:
        """Resolve one play command and start it on exactly one live target."""

        if not command.media_query:
            raise SpotifyCommandServiceError("Dem Spotify-Befehl fehlt der Inhalt.")
        if command.target_alias is None:
            requested_target = None
        else:
            # Keep an unknown explicit target explicit so it cannot silently
            # fall back to the profile default.
            requested_target = self._targets.resolve_alias(command.target_alias)
            if requested_target is None:
                requested_target = command.target_alias
        routing = route_alexa_spotify_request(
            self._registry,
            AlexaSpotifyRoutingRequest(
                voice_identity=voice_identity,
                explicit_profile=command.profile_alias,
                requested_target=requested_target,
                remember_for_session=command.remember_for_session,
            ),
            session,
        )
        if routing.decision.status is not SpotifyRoutingStatus.ROUTED:
            return (
                SpotifyCommandResult(
                    status=routing.decision.status,
                    profile_id=routing.decision.profile_id,
                    target_id=routing.decision.target_id,
                ),
                routing.session,
            )
        profile = self._registry.profiles[routing.decision.profile_id]
        access_token = self._token_provider.access_token(profile.connection_id, now=now)
        playback = self._media_resolver.resolve(command.media_query, access_token=access_token)
        plan = prepare_spotify_playback(routing.decision, self._targets, playback)
        self._playback_service.execute(plan, now=now)
        return (
            SpotifyCommandResult(
                status=SpotifyRoutingStatus.ROUTED,
                profile_id=plan.profile_id,
                target_id=plan.target_id,
            ),
            routing.session,
        )
