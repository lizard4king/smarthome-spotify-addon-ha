"""Home Assistant add-on entry point for the Smart Home Cockpit."""

from __future__ import annotations

import os
import json
import runpy
import stat
from pathlib import Path
from urllib.parse import urlsplit

MUSIC_HANDOFF = Path("/config/private_smarthome_music/cockpit-ma.json")
MAX_HANDOFF = 16 * 1024


def _load_private_music_fallback(options: dict) -> None:
    """Use only a private, bounded MA handoff; existing options always win."""
    if options.get("music_assistant_token", "") != "":
        return
    # An absent token option never grants playback through this private fallback.
    os.environ["MUSIC_ASSISTANT_ALLOW_PLAYBACK"] = "false"
    if os.environ.get("MUSIC_ASSISTANT_TOKEN"):
        return
    directory_fd = None
    file_fd = None
    try:
        # Descriptor-relative open prevents a symlink or parent-replacement race.
        # Platforms without these Linux primitives deliberately fail closed.
        directory_fd = os.open(MUSIC_HANDOFF.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        directory_stat = os.fstat(directory_fd)
        if not stat.S_ISDIR(directory_stat.st_mode) or stat.S_IMODE(directory_stat.st_mode) != 0o700:
            return
        file_fd = os.open(MUSIC_HANDOFF.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                          dir_fd=directory_fd)
        handle = os.fdopen(file_fd, "rb")
        file_fd = None  # The file object now owns and closes this descriptor.
        with handle:
            file_stat = os.fstat(handle.fileno())
            if (not stat.S_ISREG(file_stat.st_mode) or stat.S_IMODE(file_stat.st_mode) != 0o600
                    or file_stat.st_size > MAX_HANDOFF):
                return
            raw = handle.read(MAX_HANDOFF + 1)
            if len(raw) > MAX_HANDOFF:
                return
        value = json.loads(raw.decode("utf-8"))
        if (not isinstance(value, dict) or type(value.get("schema")) is not int
                or value["schema"] != 1 or value.get("music_assistant_allow_playback") is not False):
            return
        url, token = value.get("music_assistant_url"), value.get("music_assistant_token")
        if (not isinstance(url, str) or not isinstance(token, str) or not url or not token
                or any(ord(c) < 32 for c in url + token)):
            return
        parsed = urlsplit(url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
            return
        existing_url = os.environ.get("MUSIC_ASSISTANT_URL", "")
        if existing_url and existing_url.rstrip("/") != url.rstrip("/"):
            return  # Never pair this token with a different configured server.
        if not existing_url:
            os.environ["MUSIC_ASSISTANT_URL"] = url
        os.environ["MUSIC_ASSISTANT_TOKEN"] = token
    except (OSError, ValueError, TypeError, AttributeError, RecursionError):
        # Never log handoff contents, token values or exception text.
        return
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if directory_fd is not None:
            os.close(directory_fd)

def _load_options_fallback() -> None:
    """Use HA's persisted options when the shell environment is incomplete."""
    try:
        with open("/data/options.json", "r", encoding="utf-8") as handle:
            options = json.load(handle)
    except (OSError, ValueError, TypeError):
        options = {}
    if not isinstance(options, dict):
        return
    for key, env_name in (
        ("home_assistant_token", "HOME_ASSISTANT_TOKEN"),
        ("bridge_secret", "SPOTIFY_BRIDGE_SECRET"),
        ("music_assistant_url", "MUSIC_ASSISTANT_URL"),
        ("music_assistant_token", "MUSIC_ASSISTANT_TOKEN"),
    ):
        value = options.get(key)
        if isinstance(value, str) and value:
            os.environ[env_name] = value
    if "allow_writes" in options:
        os.environ["HOME_ASSISTANT_ALLOW_WRITES"] = str(options["allow_writes"]).lower()
    if "music_assistant_allow_playback" in options:
        os.environ["MUSIC_ASSISTANT_ALLOW_PLAYBACK"] = str(options["music_assistant_allow_playback"]).lower()
    _load_private_music_fallback(options)

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
