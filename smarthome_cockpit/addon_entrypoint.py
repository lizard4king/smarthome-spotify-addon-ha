"""Home Assistant add-on entry point for the Smart Home Cockpit."""

from __future__ import annotations

import json
import os
from pathlib import Path
import runpy


OPTIONS = Path("/data/options.json")


def main() -> None:
    options = json.loads(OPTIONS.read_text(encoding="utf-8"))
    os.environ.setdefault("HOME_ASSISTANT_URL", "http://homeassistant:8123")
    os.environ["HOME_ASSISTANT_TOKEN"] = str(options.get("home_assistant_token", ""))
    os.environ["HOME_ASSISTANT_ALLOW_WRITES"] = "true" if options.get("allow_writes", False) else "false"
    os.environ.setdefault("SPOTIFY_BRIDGE_URL", "http://7b071411-smarthome-spotify-bridge:8766")
    os.environ["SPOTIFY_BRIDGE_SECRET"] = str(options.get("bridge_secret", ""))
    os.environ.setdefault("COCKPIT_HOST", "0.0.0.0")
    os.environ.setdefault("COCKPIT_PORT", "8767")
    os.chdir("/app")
    runpy.run_path("/app/dashboard/server.py", run_name="__main__")


if __name__ == "__main__":
    main()
