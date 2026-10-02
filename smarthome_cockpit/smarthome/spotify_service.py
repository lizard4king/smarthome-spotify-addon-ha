"""Explicit orchestration boundary for one routed Spotify playback request."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from smarthome.spotify_connect import SpotifyConnectDevice, SpotifyDeviceCatalog
from smarthome.spotify_playback import SpotifyPlaybackRequest
from smarthome.spotify_routing import SpotifyRoutingDecision, SpotifyRoutingStatus
from smarthome.spotify_targets import SpotifyPlaybackTarget, SpotifyTargetRegistry


class SpotifyPlaybackServiceError(RuntimeError):
    """Raised before an incomplete request can cause playback."""


class TokenProvider(Protocol):
    def access_token(self, connection_id: str, *, now: datetime) -> str:
        """Return one usable locally stored token."""


class DeviceProvider(Protocol):
    def devices(self, access_token: str) -> SpotifyDeviceCatalog:
        """Return the account's currently visible Connect targets."""


class PlaybackStarter(Protocol):
    def start(
        self,
        access_token: str,
        target: SpotifyConnectDevice,
        playback: SpotifyPlaybackRequest,
    ) -> None:
        """Start an explicit content selection on an explicit target."""


@dataclass(frozen=True, slots=True)
class SpotifyPlaybackPlan:
    """Validated plan whose construction performs no I/O or device action."""

    profile_id: str
    target_id: str
    connection_id: str = field(repr=False)
    target: SpotifyPlaybackTarget = field(repr=False)
    playback: SpotifyPlaybackRequest = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.profile_id, str) or not self.profile_id:
            raise SpotifyPlaybackServiceError(
                "Dem Spotify-Wiedergabeplan fehlt ein gültiges Profil."
            )
        if not isinstance(self.connection_id, str) or not self.connection_id:
            raise SpotifyPlaybackServiceError(
                "Dem Spotify-Wiedergabeplan fehlt eine gültige Verbindung."
            )
        if (
            not isinstance(self.target, SpotifyPlaybackTarget)
            or not isinstance(self.target_id, str)
            or self.target.target_id != self.target_id
        ):
            raise SpotifyPlaybackServiceError(
                "Der Spotify-Wiedergabeplan enthält kein gültiges Ziel."
            )
        if not isinstance(self.playback, SpotifyPlaybackRequest):
            raise SpotifyPlaybackServiceError(
                "Der Spotify-Wiedergabeplan enthält keinen gültigen Inhalt."
            )


def prepare_spotify_playback(
    decision: SpotifyRoutingDecision,
    targets: SpotifyTargetRegistry,
    playback: SpotifyPlaybackRequest,
) -> SpotifyPlaybackPlan:
    """Bind a successful routing result to local target metadata without I/O."""

    if not isinstance(decision, SpotifyRoutingDecision):
        raise SpotifyPlaybackServiceError(
            "Die Spotify-Routingentscheidung ist ungültig."
        )
    if decision.status is not SpotifyRoutingStatus.ROUTED:
        raise SpotifyPlaybackServiceError(
            "Die Spotify-Anfrage ist noch nicht vollständig aufgelöst."
        )
    if not decision.profile_id or not decision.connection_id:
        raise SpotifyPlaybackServiceError(
            "Der Spotify-Anfrage fehlt ein gültiges Profil."
        )
    if not decision.target_id:
        raise SpotifyPlaybackServiceError(
            "Der Spotify-Anfrage fehlt ein Wiedergabeziel."
        )
    if not isinstance(targets, SpotifyTargetRegistry):
        raise SpotifyPlaybackServiceError(
            "Die Spotify-Zielverwaltung ist ungültig."
        )
    if not isinstance(playback, SpotifyPlaybackRequest):
        raise SpotifyPlaybackServiceError(
            "Der Spotify-Wiedergabeauftrag ist ungültig."
        )
    try:
        target = targets.require(decision.target_id)
    except ValueError:
        raise SpotifyPlaybackServiceError(
            "Das Spotify-Wiedergabeziel ist nicht lokal konfiguriert."
        ) from None
    return SpotifyPlaybackPlan(
        profile_id=decision.profile_id,
        target_id=decision.target_id,
        connection_id=decision.connection_id,
        target=target,
        playback=playback,
    )


class SpotifyPlaybackService:
    """Execute only an already prepared plan through injected collaborators."""

    def __init__(
        self,
        token_provider: TokenProvider,
        device_provider: DeviceProvider,
        playback_starter: PlaybackStarter,
    ) -> None:
        self._token_provider = token_provider
        self._device_provider = device_provider
        self._playback_starter = playback_starter

    def execute(self, plan: SpotifyPlaybackPlan, *, now: datetime) -> None:
        """Resolve local credentials and device state, then start once."""

        if not isinstance(plan, SpotifyPlaybackPlan):
            raise SpotifyPlaybackServiceError(
                "Der Spotify-Wiedergabeplan ist ungültig."
            )
        access_token = self._token_provider.access_token(
            plan.connection_id,
            now=now,
        )
        catalog = self._device_provider.devices(access_token)
        target = catalog.select_any(
            (plan.target.spotify_device_name, *plan.target.aliases)
        )
        self._playback_starter.start(access_token, target, plan.playback)
