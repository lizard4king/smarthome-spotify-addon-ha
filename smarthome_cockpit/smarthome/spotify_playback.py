"""Explicit, token-safe Spotify playback commands."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import BinaryIO, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from smarthome.spotify_connect import SpotifyConnectDevice


SPOTIFY_PLAYER_URL = "https://api.spotify.com/v1/me/player"
MAX_TRACKS_PER_REQUEST = 100
_CONTEXT_URI = re.compile(r"spotify:(album|artist|playlist):[A-Za-z0-9]+\Z")
_TRACK_URI = re.compile(r"spotify:track:[A-Za-z0-9]+\Z")
MAX_SPOTIFY_URI_LENGTH = 512


class SpotifyPlaybackError(RuntimeError):
    """Controlled playback failure without tokens, content, or device IDs."""


class HttpResponse(Protocol):
    def getcode(self) -> int:
        """Return the HTTP status code."""


HttpRequester = Callable[
    [Request, float], AbstractContextManager[HttpResponse]
]


def _open_request(
    request: Request,
    timeout: float,
) -> AbstractContextManager[BinaryIO]:
    return urlopen(request, timeout=timeout)


@dataclass(frozen=True, slots=True)
class SpotifyPlaybackRequest:
    """One explicit content selection; opaque URIs stay out of repr."""

    context_uri: str | None = None
    track_uris: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.track_uris, tuple):
            raise SpotifyPlaybackError("Die Spotify-Titelliste ist ungültig.")
        has_context = self.context_uri is not None
        has_tracks = bool(self.track_uris)
        if has_context == has_tracks:
            raise SpotifyPlaybackError(
                "Genau ein Spotify-Kontext oder eine Titelliste ist erforderlich."
            )
        if has_context and (
            not isinstance(self.context_uri, str)
            or len(self.context_uri) > MAX_SPOTIFY_URI_LENGTH
            or not _CONTEXT_URI.fullmatch(self.context_uri)
        ):
            raise SpotifyPlaybackError(
                "Der Spotify-Wiedergabekontext ist ungültig."
            )
        if has_tracks:
            if len(self.track_uris) > MAX_TRACKS_PER_REQUEST:
                raise SpotifyPlaybackError("Die Spotify-Titelliste ist zu lang.")
            if any(
                not isinstance(uri, str)
                or len(uri) > MAX_SPOTIFY_URI_LENGTH
                or not _TRACK_URI.fullmatch(uri)
                for uri in self.track_uris
            ):
                raise SpotifyPlaybackError("Die Spotify-Titelliste ist ungültig.")

    def __repr__(self) -> str:
        selection = "context" if self.context_uri is not None else "tracks"
        return f"SpotifyPlaybackRequest(selection={selection!r})"

    def as_body(self) -> dict[str, object]:
        if self.context_uri is not None:
            return {"context_uri": self.context_uri}
        return {"uris": list(self.track_uris)}


class SpotifyPlaybackClient:
    """Issue only explicit Spotify Player commands to a validated target."""

    def __init__(
        self,
        *,
        requester: HttpRequester = _open_request,
        timeout_seconds: float = 15.0,
    ) -> None:
        if (
            not isinstance(timeout_seconds, (int, float))
            or not 1 <= timeout_seconds <= 60
        ):
            raise SpotifyPlaybackError("Das Spotify-Netzwerkzeitlimit ist ungültig.")
        self._requester = requester
        self._timeout_seconds = float(timeout_seconds)

    def activate_target(
        self,
        access_token: str,
        target: SpotifyConnectDevice,
    ) -> None:
        """Transfer the player while preserving its current play/pause state."""

        device_id = _validated_device_id(target)
        self._put(
            access_token,
            SPOTIFY_PLAYER_URL,
            {"device_ids": [device_id], "play": False},
            "Das Spotify-Wiedergabeziel konnte nicht aktiviert werden.",
        )

    def start(
        self,
        access_token: str,
        target: SpotifyConnectDevice,
        playback: SpotifyPlaybackRequest,
    ) -> None:
        """Start explicitly selected content on exactly one validated target."""

        if not isinstance(playback, SpotifyPlaybackRequest):
            raise SpotifyPlaybackError("Der Spotify-Wiedergabeauftrag ist ungültig.")
        device_id = _validated_device_id(target)
        query = urlencode({"device_id": device_id})
        self._put(
            access_token,
            f"{SPOTIFY_PLAYER_URL}/play?{query}",
            playback.as_body(),
            "Die Spotify-Wiedergabe konnte nicht gestartet werden.",
        )

    def _put(
        self,
        access_token: str,
        url: str,
        body: dict[str, object],
        failure_message: str,
    ) -> None:
        if not isinstance(access_token, str) or not access_token:
            raise SpotifyPlaybackError("Für Spotify fehlt ein gültiger Zugriffstoken.")
        request = Request(
            url,
            data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            method="PUT",
        )
        try:
            with self._requester(request, self._timeout_seconds) as response:
                status = response.getcode()
        except (HTTPError, URLError, TimeoutError, OSError):
            raise SpotifyPlaybackError(failure_message) from None
        if status != 204:
            raise SpotifyPlaybackError(failure_message)


def _validated_device_id(target: SpotifyConnectDevice) -> str:
    if not isinstance(target, SpotifyConnectDevice):
        raise SpotifyPlaybackError("Das Spotify-Wiedergabeziel ist ungültig.")
    if (
        target.is_restricted is not False
        or not isinstance(target.device_id, str)
        or not target.device_id
        or len(target.device_id) > 512
    ):
        raise SpotifyPlaybackError(
            "Das Spotify-Wiedergabeziel erlaubt keine Fernsteuerung."
        )
    return target.device_id
