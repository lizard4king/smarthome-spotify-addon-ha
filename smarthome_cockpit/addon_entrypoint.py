"""Home Assistant add-on entry point for the Smart Home Cockpit."""

from __future__ import annotations

import os
import runpy

def main() -> None:
    os.environ.setdefault("HOME_ASSISTANT_URL", "http://homeassistant:8123")
    os.environ.setdefault("SPOTIFY_BRIDGE_URL", "http://7b071411-smarthome-spotify-bridge:8766")
    os.environ.setdefault("COCKPIT_HOST", "0.0.0.0")
    os.environ.setdefault("COCKPIT_PORT", "8767")
    os.chdir("/app")
    runpy.run_path("/app/dashboard/server.py", run_name="__main__")


if __name__ == "__main__":
    main()
