"""Optional OpenAI interpreter with strict, locally validated actions."""

from __future__ import annotations

import json
import os
from typing import Any, Protocol

from smarthome.command_parser import CommandAction, CommandParseError


DEFAULT_MODEL = "gpt-5-mini"
MAX_OUTPUT_TOKENS = 160
REQUEST_TIMEOUT_SECONDS = 10.0
ALLOWED_LIGHTS = frozenset({"wohnzimmerlicht", "schlafzimmerlicht"})


class ResponsesClient(Protocol):
    """Minimal Responses API surface used by the adapter."""

    def create(self, **kwargs: Any) -> Any:
        """Create one model response."""


class OpenAIConfigurationError(CommandParseError):
    """Raised when the optional OpenAI adapter is not configured."""


class OpenAIAdapterError(CommandParseError):
    """Raised when a model response cannot be used safely."""


ACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "action": {
            "type": "string",
            "enum": ["set_light", "set_temperature", "get_status"],
        },
        "device_id": {
            "anyOf": [
                {
                    "type": "string",
                    "enum": [
                        "wohnzimmerlicht",
                        "schlafzimmerlicht",
                        "wohnzimmerheizung",
                    ],
                },
                {"type": "null"},
            ]
        },
        "value": {
            "anyOf": [
                {"type": "boolean"},
                {"type": "number"},
                {"type": "null"},
            ]
        },
        "detail": {
            "anyOf": [
                {"type": "string", "enum": ["all", "active_devices"]},
                {"type": "null"},
            ]
        },
    },
    "required": ["action", "device_id", "value", "detail"],
}


INSTRUCTIONS = """Du uebersetzt deutsche Smart-Home-Befehle in genau eine Aktion.
Erlaubt sind ausschliesslich set_light, set_temperature und get_status.
Verwende nur die im Schema genannten Geraete. Erfinde keine Aktionen oder Geraete.
Lichtwerte sind boolesch. Temperaturen liegen zwischen 10,0 und 30,0 Grad.
Statusabfragen verwenden keine device_id und keinen value.
Fuehre selbst keine Aktion aus und gib keine Erklaerung aus."""


class OpenAICommandInterpreter:
    """Convert natural language to one validated action via the Responses API."""

    def __init__(
        self,
        responses: ResponsesClient,
        model: str = DEFAULT_MODEL,
    ) -> None:
        if not model.strip():
            raise OpenAIConfigurationError("OPENAI_MODEL darf nicht leer sein.")
        self.responses = responses
        self.model = model.strip()

    @classmethod
    def from_environment(cls) -> OpenAICommandInterpreter:
        """Build an adapter from environment variables without reading files."""

        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise OpenAIConfigurationError(
                "OPENAI_API_KEY ist nicht gesetzt. Nutze den lokalen Parser oder "
                "setze den Schlüssel nur in der lokalen Umgebung."
            )

        try:
            from openai import OpenAI
        except ImportError as exc:
            raise OpenAIConfigurationError(
                'Das optionale Paket fehlt. Installiere es mit: pip install -e ".[openai]"'
            ) from exc

        model = os.getenv("OPENAI_MODEL", DEFAULT_MODEL)
        client = OpenAI(
            api_key=api_key,
            timeout=REQUEST_TIMEOUT_SECONDS,
            max_retries=1,
        )
        return cls(client.responses, model=model)

    def interpret(self, command: str) -> CommandAction:
        """Request, decode, and locally validate one structured action."""

        if not command.strip():
            raise OpenAIAdapterError("Bitte gib einen Befehl ein.")

        try:
            response = self.responses.create(
                model=self.model,
                instructions=INSTRUCTIONS,
                input=command,
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "smart_home_action",
                        "strict": True,
                        "schema": ACTION_SCHEMA,
                    }
                },
                max_output_tokens=MAX_OUTPUT_TOKENS,
                store=False,
            )
            output_text = response.output_text
        except Exception as exc:
            raise OpenAIAdapterError(
                "Die OpenAI-Anfrage ist fehlgeschlagen. Es wurde keine Aktion ausgeführt."
            ) from exc

        if not isinstance(output_text, str) or not output_text.strip():
            raise OpenAIAdapterError(
                "Die OpenAI-Antwort enthielt keine verwendbare Aktion."
            )

        try:
            payload = json.loads(output_text)
        except json.JSONDecodeError as exc:
            raise OpenAIAdapterError(
                "Die OpenAI-Antwort war kein gültiges Aktionsformat."
            ) from exc
        return _validate_action(payload)


def _validate_action(payload: Any) -> CommandAction:
    if not isinstance(payload, dict) or set(payload) != {
        "action",
        "device_id",
        "value",
        "detail",
    }:
        raise OpenAIAdapterError("Die strukturierte Aktion ist unvollständig.")

    action = payload["action"]
    device_id = payload["device_id"]
    value = payload["value"]
    detail = payload["detail"]

    if action == "set_light":
        if (
            not isinstance(device_id, str)
            or device_id not in ALLOWED_LIGHTS
            or not isinstance(value, bool)
        ):
            raise OpenAIAdapterError("Der Lichtbefehl ist nicht zulässig.")
        if detail is not None:
            raise OpenAIAdapterError("Der Lichtbefehl enthält unerlaubte Zusatzdaten.")
        return CommandAction("set_light", device_id=device_id, value=value)

    if action == "set_temperature":
        if device_id != "wohnzimmerheizung":
            raise OpenAIAdapterError("Das Thermostat ist nicht zulässig.")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise OpenAIAdapterError("Die Temperatur muss eine Zahl sein.")
        temperature = float(value)
        if not 10.0 <= temperature <= 30.0:
            raise OpenAIAdapterError(
                "Die Temperatur muss zwischen 10,0 und 30,0 Grad liegen."
            )
        if detail is not None:
            raise OpenAIAdapterError(
                "Der Temperaturbefehl enthält unerlaubte Zusatzdaten."
            )
        return CommandAction(
            "set_temperature",
            device_id="wohnzimmerheizung",
            value=temperature,
        )

    if action == "get_status":
        if device_id is not None or value is not None:
            raise OpenAIAdapterError("Die Statusabfrage enthält unerlaubte Gerätedaten.")
        if not isinstance(detail, str) or detail not in {"all", "active_devices"}:
            raise OpenAIAdapterError("Die Statusabfrage ist unvollständig.")
        return CommandAction("get_status", detail=detail)

    raise OpenAIAdapterError("Diese Aktion ist nicht zugelassen.")
