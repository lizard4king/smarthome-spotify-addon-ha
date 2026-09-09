"""Minimal Spotify search adapter for the local command bridge."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from smarthome.spotify_playback import SpotifyPlaybackRequest


SEARCH_URL = "https://api.spotify.com/v1/search"


class SpotifySearchError(RuntimeError):
    """Raised when a spoken query cannot be resolved safely."""


class SpotifyMediaSearchClient:
    """Resolve a query to one bounded artist/album/playlist/track context."""

    def __init__(self, requester: Callable = urlopen, timeout_seconds: float = 15.0) -> None:
        self._requester = requester
        self._timeout = timeout_seconds

    def resolve(self, query: str, *, access_token: str) -> SpotifyPlaybackRequest:
        if not isinstance(query, str) or not query.strip() or len(query) > 256:
            raise SpotifySearchError("Die Spotify-Suche ist ungültig.")
        if not isinstance(access_token, str) or not access_token:
            raise SpotifySearchError("Für Spotify fehlt ein Zugriffstoken.")
        params = urlencode({"q": query.strip(), "type": "artist,album,playlist,track", "limit": 1})
        request = Request(
            f"{SEARCH_URL}?{params}",
            headers={"Accept": "application/json", "Authorization": f"Bearer {access_token}"},
        )
        try:
            with self._requester(request, timeout=self._timeout) as response:
                raw = response.read(64 * 1024)
        except Exception as exc:
            raise SpotifySearchError("Die Spotify-Suche ist nicht erreichbar.") from exc
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SpotifySearchError("Spotify lieferte keine gültige Suche.") from exc
        for key in ("artists", "albums", "playlists", "tracks"):
            item = _first_item(payload, key)
            if item is not None:
                uri = item.get("uri")
                if isinstance(uri, str):
                    return SpotifyPlaybackRequest(context_uri=uri) if ":track:" not in uri else SpotifyPlaybackRequest(track_uris=(uri,))
        raise SpotifySearchError("Spotify hat keinen passenden Inhalt gefunden.")


def _first_item(payload: object, key: str) -> Mapping[str, object] | None:
    if not isinstance(payload, dict):
        return None
    section = payload.get(key)
    if not isinstance(section, dict):
        return None
    items = section.get("items")
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        return None
    return items[0]
