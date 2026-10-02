"""Spotify Connect device discovery with strict local validation."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import BinaryIO, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


SPOTIFY_DEVICES_URL = "https://api.spotify.com/v1/me/player/devices"
MAX_RESPONSE_BYTES = 128 * 1024
MAX_DEVICES = 128


class SpotifyConnectError(RuntimeError):
    """Controlled Spotify Connect failure without tokens or device IDs."""


class HttpResponse(Protocol):
    def read(self, amount: int = -1) -> bytes:
        """Read response bytes."""


HttpRequester = Callable[
    [Request, float], AbstractContextManager[HttpResponse]
]


def _open_request(
    request: Request,
    timeout: float,
) -> AbstractContextManager[BinaryIO]:
    return urlopen(request, timeout=timeout)


@dataclass(frozen=True, slots=True)
class SpotifyConnectDevice:
    """Validated device metadata; the opaque Spotify ID stays out of repr."""

    name: str
    device_type: str
    is_active: bool
    is_restricted: bool
    volume_percent: int | None
    device_id: str | None = field(repr=False)

    @property
    def controllable(self) -> bool:
        return self.device_id is not None and not self.is_restricted


@dataclass(frozen=True, slots=True)
class SpotifyDeviceCatalog:
    """One bounded snapshot of the account's visible Connect targets."""

    devices: tuple[SpotifyConnectDevice, ...]

    @classmethod
    def from_response(cls, response: object) -> SpotifyDeviceCatalog:
        if not isinstance(response, Mapping) or set(response) != {"devices"}:
            raise SpotifyConnectError("Spotify lieferte keine gültige Geräteliste.")
        raw_devices = response["devices"]
        if not isinstance(raw_devices, list) or len(raw_devices) > MAX_DEVICES:
            raise SpotifyConnectError("Spotify lieferte keine gültige Geräteliste.")
        return cls(tuple(_device_from_mapping(raw) for raw in raw_devices))

    def select(self, device_name: str) -> SpotifyConnectDevice:
        """Select one exact normalized device name and enforce controllability."""

        normalized = _normalize_name(device_name)
        matches = [
            device
            for device in self.devices
            if _normalize_name(device.name) == normalized
        ]
        if not matches:
            raise SpotifyConnectError(
                "Das gewünschte Spotify-Wiedergabeziel ist nicht verfügbar."
            )
        if len(matches) > 1:
            raise SpotifyConnectError(
                "Das Spotify-Wiedergabeziel ist nicht eindeutig."
            )
        selected = matches[0]
        if selected.is_restricted:
            raise SpotifyConnectError(
                "Das Spotify-Wiedergabeziel erlaubt keine Fernsteuerung."
            )
        if selected.device_id is None:
            raise SpotifyConnectError(
                "Spotify liefert für das Wiedergabeziel keine steuerbare ID."
            )
        return selected

    def select_any(self, device_names: tuple[str, ...]) -> SpotifyConnectDevice:
        """Select one current device by any configured logical alias."""

        if not isinstance(device_names, tuple) or not device_names:
            raise SpotifyConnectError("Das Spotify-Wiedergabeziel ist ungültig.")
        wanted = {_normalize_name(name) for name in device_names}
        matches = [device for device in self.devices if _normalize_name(device.name) in wanted]
        if len(matches) != 1:
            raise SpotifyConnectError(
                "Das Spotify-Wiedergabeziel ist nicht verfügbar oder nicht eindeutig."
            )
        selected = matches[0]
        if selected.is_restricted or selected.device_id is None:
            raise SpotifyConnectError(
                "Das Spotify-Wiedergabeziel erlaubt keine Fernsteuerung."
            )
        return selected


class SpotifyConnectClient:
    """Read the current user's Spotify Connect targets."""

    def __init__(
        self,
        *,
        requester: HttpRequester = _open_request,
        timeout_seconds: float = 15.0,
    ) -> None:
        if not isinstance(timeout_seconds, (int, float)) or not 1 <= timeout_seconds <= 60:
            raise SpotifyConnectError("Das Spotify-Netzwerkzeitlimit ist ungültig.")
        self._requester = requester
        self._timeout_seconds = float(timeout_seconds)

    def devices(self, access_token: str) -> SpotifyDeviceCatalog:
        """Fetch and validate devices without logging token or opaque IDs."""

        if not isinstance(access_token, str) or not access_token:
            raise SpotifyConnectError("Für Spotify fehlt ein gültiger Zugriffstoken.")
        request = Request(
            SPOTIFY_DEVICES_URL,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {access_token}",
            },
            method="GET",
        )
        try:
            with self._requester(request, self._timeout_seconds) as response:
                payload = response.read(MAX_RESPONSE_BYTES + 1)
        except (HTTPError, URLError, TimeoutError, OSError):
            raise SpotifyConnectError(
                "Die Spotify-Geräteliste ist derzeit nicht erreichbar."
            ) from None
        if len(payload) > MAX_RESPONSE_BYTES:
            raise SpotifyConnectError("Die Spotify-Geräteliste ist zu groß.")
        try:
            decoded = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise SpotifyConnectError(
                "Spotify lieferte keine gültige Geräteliste."
            ) from None
        return SpotifyDeviceCatalog.from_response(decoded)


def _device_from_mapping(raw: object) -> SpotifyConnectDevice:
    if not isinstance(raw, Mapping):
        raise SpotifyConnectError("Spotify lieferte ein ungültiges Gerät.")
    required = {
        "id",
        "is_active",
        "is_private_session",
        "is_restricted",
        "name",
        "supports_volume",
        "type",
        "volume_percent",
    }
    if not required.issubset(raw):
        raise SpotifyConnectError("Spotify lieferte ein unvollständiges Gerät.")
    device_id = raw["id"]
    if device_id is not None and (
        not isinstance(device_id, str) or not device_id or len(device_id) > 512
    ):
        raise SpotifyConnectError("Spotify lieferte eine ungültige Geräte-ID.")
    name = _required_text(raw["name"], "Gerätename")
    device_type = _required_text(raw["type"], "Gerätetyp")
    is_active = _required_bool(raw["is_active"], "Aktivstatus")
    is_restricted = _required_bool(raw["is_restricted"], "Steuerstatus")
    volume = raw["volume_percent"]
    if volume is not None and (
        not isinstance(volume, int)
        or isinstance(volume, bool)
        or not 0 <= volume <= 100
    ):
        raise SpotifyConnectError("Spotify lieferte eine ungültige Lautstärke.")
    return SpotifyConnectDevice(
        name=name,
        device_type=device_type,
        is_active=is_active,
        is_restricted=is_restricted,
        volume_percent=volume,
        device_id=device_id,
    )


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise SpotifyConnectError(f"Spotify lieferte keinen gültigen {label}.")
    return value


def _required_bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise SpotifyConnectError(f"Spotify lieferte keinen gültigen {label}.")
    return value


def _normalize_name(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SpotifyConnectError("Der Spotify-Gerätename darf nicht leer sein.")
    return re.sub(r"\s+", " ", value).strip().casefold()
