"""Safe operations for the simulated devices."""

from __future__ import annotations

from datetime import datetime

from smarthome.models import Device, DeviceType, SmartHomeState, validate_temperature


class DeviceController:
    """Validate and apply the limited set of supported device actions."""

    def __init__(self, state: SmartHomeState) -> None:
        self.state = state

    def set_light(self, device_id: str, is_on: bool) -> Device:
        """Switch a known simulated light on or off."""

        device = self._get_device(device_id, DeviceType.LIGHT)
        device.is_on = bool(is_on)
        self._log(f"{device.name}: {'ein' if device.is_on else 'aus'}")
        return device

    def set_temperature(self, device_id: str, temperature: float) -> Device:
        """Set a validated target temperature for a known thermostat."""

        device = self._get_device(device_id, DeviceType.THERMOSTAT)
        device.target_temperature = validate_temperature(temperature)
        self._log(f"{device.name}: {device.target_temperature:.1f} °C")
        return device

    def status_text(self, active_only: bool = False) -> str:
        """Return a human-readable status for all or only active devices."""

        if active_only:
            active = [
                device.name
                for device in self.state.devices.values()
                if device.device_type is DeviceType.LIGHT and device.is_on
            ]
            result = (
                "Eingeschaltet: " + ", ".join(active)
                if active
                else "Es ist kein Licht eingeschaltet."
            )
            self._log("Eingeschaltete Geraete abgefragt")
            return result

        parts: list[str] = []
        for device in self.state.devices.values():
            if device.device_type is DeviceType.LIGHT:
                parts.append(f"{device.name}: {'ein' if device.is_on else 'aus'}")
            else:
                parts.append(f"{device.name}: {device.target_temperature:.1f} °C")
        self._log("Status abgefragt")
        return "; ".join(parts)

    def _get_device(self, device_id: str, expected_type: DeviceType) -> Device:
        try:
            device = self.state.devices[device_id]
        except KeyError as exc:
            raise ValueError(f"Unbekanntes Geraet: {device_id}") from exc
        if device.device_type is not expected_type:
            raise ValueError(f"{device.name} unterstuetzt diese Aktion nicht.")
        return device

    def _log(self, message: str) -> None:
        timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
        self.state.action_log.append(f"{timestamp} – {message}")
        self.state.action_log = self.state.action_log[-50:]

