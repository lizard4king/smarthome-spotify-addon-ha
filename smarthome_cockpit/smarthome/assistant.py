"""Bounded assistant interface; the MVP always uses the local parser."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable

from smarthome.command_parser import CommandAction, CommandParseError, parse_command
from smarthome.devices import DeviceController


ALLOWED_ACTIONS = frozenset({"set_light", "set_temperature", "get_status"})


@dataclass(frozen=True, slots=True)
class AssistantResult:
    """Result suitable for display by the user interface."""

    success: bool
    message: str
    changed: bool = False


class SmartHomeAssistant:
    """Interpret text through a bounded provider and execute allowed actions."""

    def __init__(
        self,
        controller: DeviceController,
        interpreter: Callable[[str], CommandAction] = parse_command,
    ) -> None:
        self.controller = controller
        self.interpreter = interpreter

    @property
    def openai_configured(self) -> bool:
        """Report whether a key exists for a future, currently inactive adapter."""

        return bool(os.getenv("OPENAI_API_KEY"))

    def handle(self, command: str) -> AssistantResult:
        """Interpret and execute one command with controlled errors."""

        try:
            action = self.interpreter(command)
            return self.execute(action)
        except (CommandParseError, ValueError) as exc:
            return AssistantResult(False, str(exc))

    def execute(self, action: CommandAction) -> AssistantResult:
        """Validate and execute one structured action."""

        if action.action not in ALLOWED_ACTIONS:
            raise ValueError("Diese Aktion ist nicht zugelassen.")

        if action.action == "set_light":
            if action.device_id is None or not isinstance(action.value, bool):
                raise ValueError("Der Lichtbefehl ist unvollstaendig.")
            device = self.controller.set_light(action.device_id, action.value)
            return AssistantResult(
                True,
                f"{device.name} wurde {'eingeschaltet' if action.value else 'ausgeschaltet'}.",
                changed=True,
            )

        if action.action == "set_temperature":
            if action.device_id is None or isinstance(action.value, bool) or not isinstance(
                action.value, (int, float)
            ):
                raise ValueError("Der Temperaturbefehl ist unvollstaendig.")
            device = self.controller.set_temperature(action.device_id, float(action.value))
            return AssistantResult(
                True,
                f"{device.name} wurde auf {device.target_temperature:.1f} °C gesetzt.",
                changed=True,
            )

        active_only = action.detail == "active_devices"
        return AssistantResult(True, self.controller.status_text(active_only=active_only))

