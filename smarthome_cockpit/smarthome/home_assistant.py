"""Restrictive Home Assistant REST adapter with writes disabled by default."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import base64
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen

from smarthome.models import validate_temperature


DEFAULT_TIMEOUT_SECONDS = 5.0
DEFAULT_BLOCKED_DOMAINS = frozenset(
    {"alarm_control_panel", "camera", "lock", "script"}
)
ENTITY_PATTERN = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
DOMAIN_PATTERN = re.compile(r"^[a-z0-9_]+$")


class HomeAssistantError(ValueError):
    """Base error for safe Home Assistant failures."""


class HomeAssistantConfigurationError(HomeAssistantError):
    """Raised when local configuration is missing or unsafe."""


class HomeAssistantWriteBlocked(HomeAssistantError):
    """Raised before a write when explicit write permission is absent."""


class HomeAssistantConnectionError(HomeAssistantError):
    """Raised for controlled HTTP, network, or response errors."""


class JsonTransport(Protocol):
    """Small injectable HTTP boundary used by the adapter."""

    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any] | None,
        timeout: float,
    ) -> Any:
        """Send one JSON request and return decoded JSON."""


class UrllibJsonTransport:
    """Standard-library JSON transport that never includes secrets in errors."""

    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any] | None,
        timeout: float,
    ) -> Any:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=timeout) as response:
                status = response.status
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            raise HomeAssistantConnectionError(
                f"Home Assistant antwortete mit HTTP {exc.code}."
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise HomeAssistantConnectionError(
                "Home Assistant ist nicht erreichbar. Es wurde keine Aktion bestätigt."
            ) from exc

        if status not in {200, 201}:
            raise HomeAssistantConnectionError(
                f"Home Assistant antwortete mit HTTP {status}."
            )
        try:
            return json.loads(body) if body else None
        except json.JSONDecodeError as exc:
            raise HomeAssistantConnectionError(
                "Home Assistant lieferte keine gültige JSON-Antwort."
            ) from exc


@dataclass(frozen=True, slots=True)
class HomeAssistantConfig:
    """Validated environment-backed Home Assistant configuration."""

    base_url: str
    token: str
    entities: dict[str, str]
    allow_writes: bool = False
    write_allowlist: frozenset[str] | None = None
    blocked_domains: frozenset[str] = DEFAULT_BLOCKED_DOMAINS
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_url", _validate_base_url(self.base_url))
        if not self.token.strip():
            raise HomeAssistantConfigurationError(
                "HOME_ASSISTANT_TOKEN ist nicht gesetzt."
            )
        if not 0 < self.timeout_seconds <= 30:
            raise HomeAssistantConfigurationError(
                "Das Home-Assistant-Zeitlimit muss zwischen 0 und 30 Sekunden liegen."
            )

        if not isinstance(self.entities, dict):
            raise HomeAssistantConfigurationError(
                "Die Home-Assistant-Entity-Zuordnung muss ein Objekt sein."
            )
        for device_id, entity_id in self.entities.items():
            if (
                not isinstance(device_id, str)
                or not device_id.strip()
                or not isinstance(entity_id, str)
                or not ENTITY_PATTERN.fullmatch(entity_id)
            ):
                raise HomeAssistantConfigurationError(
                    f"Ungültige Home-Assistant-Entity-Zuordnung für {device_id}."
                )
        if self.write_allowlist is not None and not self.write_allowlist <= set(
            self.entities
        ):
            raise HomeAssistantConfigurationError(
                "Die Schreib-Positivliste enthält unbekannte Entity-Schlüssel."
            )
        if not all(DOMAIN_PATTERN.fullmatch(domain) for domain in self.blocked_domains):
            raise HomeAssistantConfigurationError(
                "Die gesperrten Home-Assistant-Domains sind ungültig."
            )

    @classmethod
    def from_environment(cls) -> HomeAssistantConfig:
        """Read configuration only from process environment variables."""

        entities = {
            key: value
            for key, value in {
                "wohnzimmerlicht": os.getenv("HOME_ASSISTANT_WOHNZIMMERLICHT", ""),
                "schlafzimmerlicht": os.getenv(
                    "HOME_ASSISTANT_SCHLAFZIMMERLICHT", ""
                ),
                "wohnzimmerheizung": os.getenv(
                    "HOME_ASSISTANT_WOHNZIMMERHEIZUNG", ""
                ),
            }.items()
            if value
        }
        raw_entities = os.getenv("HOME_ASSISTANT_ENTITIES_JSON", "").strip()
        if raw_entities:
            try:
                configured = json.loads(raw_entities)
            except json.JSONDecodeError as exc:
                raise HomeAssistantConfigurationError(
                    "HOME_ASSISTANT_ENTITIES_JSON ist kein gültiges JSON."
                ) from exc
            if not isinstance(configured, dict):
                raise HomeAssistantConfigurationError(
                    "HOME_ASSISTANT_ENTITIES_JSON muss ein Objekt sein."
                )
            entities = configured
        raw_allowlist = os.getenv("HOME_ASSISTANT_WRITE_ALLOWLIST", "").strip()
        write_allowlist = (
            frozenset(item.strip() for item in raw_allowlist.split(",") if item.strip())
            if raw_allowlist
            else None
        )
        raw_blocked = os.getenv("HOME_ASSISTANT_BLOCKED_DOMAINS", "").strip()
        blocked_domains = (
            frozenset(item.strip() for item in raw_blocked.split(",") if item.strip())
            if raw_blocked
            else DEFAULT_BLOCKED_DOMAINS
        )
        return cls(
            base_url=os.getenv("HOME_ASSISTANT_URL", ""),
            token=os.getenv("HOME_ASSISTANT_TOKEN", ""),
            entities=entities,
            allow_writes=os.getenv("HOME_ASSISTANT_ALLOW_WRITES", "").casefold()
            == "true",
            write_allowlist=write_allowlist,
            blocked_domains=blocked_domains,
        )


class HomeAssistantAdapter:
    """Call only the explicitly supported Home Assistant REST services."""

    def __init__(
        self,
        config: HomeAssistantConfig,
        transport: JsonTransport | None = None,
    ) -> None:
        self.config = config
        self.transport = transport or UrllibJsonTransport()

    def healthcheck(self) -> bool:
        """Perform a read-only API availability check."""

        response = self._request("GET", "api/", None)
        return isinstance(response, dict) and response.get("message") == "API running."

    def set_light(self, device_id: str, is_on: bool) -> None:
        """Call turn_on or turn_off for one mapped light."""

        self._require_writes()
        if not isinstance(is_on, bool):
            raise HomeAssistantError("Der Home-Assistant-Lichtbefehl ist ungültig.")
        entity_id = self._writable_entity(device_id, "light")
        service = "turn_on" if is_on else "turn_off"
        self._request(
            "POST",
            f"api/services/light/{service}",
            {"entity_id": entity_id},
        )

    def set_temperature(self, device_id: str, temperature: float) -> None:
        """Call climate.set_temperature for the mapped thermostat."""

        self._require_writes()
        target = validate_temperature(temperature)
        entity_id = self._writable_entity(device_id, "climate")
        self._request(
            "POST",
            "api/services/climate/set_temperature",
            {
                "entity_id": entity_id,
                "temperature": target,
            },
        )

    def send_alexa_text_command(self, device_id: str, message: str) -> None:
        """Send a text command to one Alexa device as if spoken aloud."""

        self._require_writes()
        if not isinstance(device_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{8,128}", device_id):
            raise HomeAssistantError("Die Alexa-Geräte-ID ist ungültig.")
        text = message.strip() if isinstance(message, str) else ""
        if not text or len(text) > 500:
            raise HomeAssistantError("Die Alexa-Ansage muss 1 bis 500 Zeichen enthalten.")
        self._request(
            "POST",
            "api/services/alexa_devices/send_text_command",
            {
                "device_id": device_id,
                "text_command": text,
            },
        )

    def camera_image(self, entity_id: str) -> dict[str, Any]:
        """Return one current camera frame without persisting it."""

        if not isinstance(entity_id, str) or not ENTITY_PATTERN.fullmatch(entity_id) or not entity_id.startswith("camera."):
            raise HomeAssistantError("Das Kamera-Ziel ist ungültig.")
        response = self._request("GET", f"api/camera_proxy/{entity_id}", None, raw=True)
        return response

    def discover_entities(self) -> list[dict[str, Any]]:
        """Return Home Assistant states without creating permissions."""

        response = self._request("GET", "api/states", None)
        if not isinstance(response, list):
            raise HomeAssistantConnectionError(
                "Home Assistant lieferte kein Entity-Inventar."
            )
        return [item for item in response if isinstance(item, dict)]

    def _writable_entity(self, device_id: str, domain: str) -> str:
        self._require_writes()
        entity_id = self.config.entities.get(device_id)
        if not entity_id or not entity_id.startswith(f"{domain}."):
            raise HomeAssistantError("Die konfigurierte Home-Assistant-Entity ist ungültig.")
        if domain in self.config.blocked_domains:
            raise HomeAssistantWriteBlocked(
                f"Schreibzugriffe auf die Domain {domain} sind gesperrt."
            )
        if (
            self.config.write_allowlist is not None
            and device_id not in self.config.write_allowlist
        ):
            raise HomeAssistantWriteBlocked(
                f"Schreibzugriff für Entity {device_id} ist nicht freigegeben."
            )
        return entity_id

    def _require_writes(self) -> None:
        if not self.config.allow_writes:
            raise HomeAssistantWriteBlocked(
                "Home-Assistant-Schreibzugriffe sind deaktiviert. Setze "
                "HOME_ASSISTANT_ALLOW_WRITES erst nach ausdrücklicher Freigabe auf true."
            )

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
        *,
        raw: bool = False,
    ) -> Any:
        url = urljoin(f"{self.config.base_url}/", path)
        headers = {
            "Authorization": f"Bearer {self.config.token}",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (SmartHome-Cockpit)",
        }
        if method != "GET":
            headers["Content-Type"] = "application/json"
        if raw:
            request = Request(url, headers=headers, method=method)
            try:
                with urlopen(request, timeout=self.config.timeout_seconds) as response:
                    return {"content_type": response.headers.get_content_type(), "body": response.read(8 * 1024 * 1024)}
            except (HTTPError, URLError, TimeoutError, OSError) as exc:
                raise HomeAssistantConnectionError("Das Kamerabild ist nicht erreichbar.") from exc
        return self.transport.request(
            method,
            url,
            headers,
            payload,
            self.config.timeout_seconds,
        )


def _validate_base_url(value: str) -> str:
    url = value.strip().rstrip("/")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HomeAssistantConfigurationError(
            "HOME_ASSISTANT_URL muss eine vollständige HTTP- oder HTTPS-URL sein."
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise HomeAssistantConfigurationError(
            "HOME_ASSISTANT_URL darf keine Zugangsdaten, Query oder Fragment enthalten."
        )
    if parsed.path not in {"", "/"}:
        raise HomeAssistantConfigurationError(
            "HOME_ASSISTANT_URL darf keinen zusätzlichen Pfad enthalten."
        )

    if parsed.scheme == "http" and not _is_local_hostname(parsed.hostname):
        raise HomeAssistantConfigurationError(
            "Unverschlüsseltes HTTP ist nur für lokale Home-Assistant-Adressen erlaubt."
        )
    return url


def _is_local_hostname(hostname: str) -> bool:
    host = hostname.casefold()
    if host == "localhost" or host.endswith(".local"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_private or address.is_loopback
