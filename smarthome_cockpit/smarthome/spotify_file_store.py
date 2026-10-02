"""Permission-restricted JSON token store for the Home Assistant add-on."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from smarthome.spotify_oauth import SpotifyTokenSet


class SpotifyFileStore:
    def __init__(self, directory: str | Path = "/data/spotify-tokens") -> None:
        self._directory = Path(directory)
        self._directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self._directory, 0o700)

    def load(self, connection_id: str) -> SpotifyTokenSet | None:
        path = self._path(connection_id)
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return SpotifyTokenSet(
                access_token=raw["access_token"],
                refresh_token=raw["refresh_token"],
                scopes=frozenset(raw["scopes"]),
                expires_at=datetime.fromisoformat(raw["expires_at"]),
            )
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            raise RuntimeError("Lokaler Spotify-Token-Eintrag ist ungültig.") from None

    def save(self, connection_id: str, tokens: SpotifyTokenSet) -> None:
        path = self._path(connection_id)
        path.write_text(
            json.dumps(
                {"access_token": tokens.access_token, "refresh_token": tokens.refresh_token,
                 "scopes": sorted(tokens.scopes), "expires_at": tokens.expires_at.isoformat()},
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        os.chmod(path, 0o600)

    def _path(self, connection_id: str) -> Path:
        if not isinstance(connection_id, str) or not connection_id.isidentifier():
            raise RuntimeError("Spotify-Verbindungs-ID ist ungültig.")
        return self._directory / f"{connection_id}.json"
