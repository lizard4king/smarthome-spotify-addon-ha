"""Home Assistant add-on entry point for the Smart Home Cockpit."""

from __future__ import annotations

import os
import json
import runpy

def _load_options_fallback() -> None:
    """Use HA's persisted options when the shell environment is incomplete."""
    try:
        with open("/data/options.json", "r", encoding="utf-8") as handle:
            options = json.load(handle)
    except (OSError, ValueError, TypeError):
        return
    for key, env_name in (
        ("home_assistant_token", "HOME_ASSISTANT_TOKEN"),
        ("bridge_secret", "SPOTIFY_BRIDGE_SECRET"),
    ):
        value = options.get(key)
        if isinstance(value, str) and value:
            os.environ[env_name] = value
    if "allow_writes" in options:
        os.environ["HOME_ASSISTANT_ALLOW_WRITES"] = str(options["allow_writes"]).lower()

def main() -> None:
    _load_options_fallback()
    print(
        "Cockpit-Python: HA-Token=%s, Bridge-Secret=%s, Schreibzugriff=%s"
        % (
            "gesetzt" if os.environ.get("HOME_ASSISTANT_TOKEN") else "fehlt",
            "gesetzt" if os.environ.get("SPOTIFY_BRIDGE_SECRET") else "fehlt",
            os.environ.get("HOME_ASSISTANT_ALLOW_WRITES", ""),
        ),
        flush=True,
    )
    os.environ.setdefault("HOME_ASSISTANT_URL", "http://192.168.178.20")
    os.environ.setdefault("SPOTIFY_BRIDGE_URL", "http://7b071411-smarthome-spotify-bridge:8766")
    os.environ.setdefault("COCKPIT_HOST", "0.0.0.0")
    os.environ.setdefault("COCKPIT_PORT", "8767")
    os.chdir("/app")
    runpy.run_path("/app/dashboard/server.py", run_name="__main__")


if __name__ == "__main__":
    main()
