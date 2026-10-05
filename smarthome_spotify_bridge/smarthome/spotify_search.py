"""Minimal Spotify search adapter for the local command bridge."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from smarthome.spotify_playback import SpotifyPlaybackRequest


SEARCH_URL = "https://api.spotify.com/v1/search"


class SpotifySearchError(RuntimeError):
    """Raised when a spoken query cannot be resolved safely."""


@dataclass(frozen=True, slots=True)
class SpotifySearchResult:
    """Safe, display-ready Spotify metadata without access tokens."""

    uri: str
    title: str
    subtitle: str
    image_url: str | None = None


class SpotifyMediaSearchClient:
    """Resolve a query to one bounded artist/album/playlist/track context."""

    def __init__(self, requester: Callable = urlopen, timeout_seconds: float = 15.0) -> None:
        self._requester = requester
        self._timeout = timeout_seconds

    def resolve(self, query: str, *, access_token: str) -> SpotifyPlaybackRequest:
        # Search-result selections already carry a validated Spotify URI. Do
        # not search the URI as spoken text and replace it with a different hit.
        if isinstance(query, str) and query.startswith("spotify:"):
            return (SpotifyPlaybackRequest(track_uris=(query,)) if query.startswith("spotify:track:")
                    else SpotifyPlaybackRequest(context_uri=query))
        results = self.search(query, access_token=access_token, limit=1)
        if not results:
            raise SpotifySearchError("Spotify hat keinen passenden Inhalt gefunden.")
        uri = results[0].uri
        return SpotifyPlaybackRequest(context_uri=uri) if ":track:" not in uri else SpotifyPlaybackRequest(track_uris=(uri,))

    def search(self, query: str, *, access_token: str, limit: int = 8) -> tuple[SpotifySearchResult, ...]:
        if not isinstance(query, str) or not query.strip() or len(query) > 256:
            raise SpotifySearchError("Die Spotify-Suche ist ungültig.")
        if not isinstance(access_token, str) or not access_token:
            raise SpotifySearchError("Für Spotify fehlt ein Zugriffstoken.")
        if not isinstance(limit, int) or not 1 <= limit <= 20:
            raise SpotifySearchError("Die Anzahl der Spotify-Treffer ist ungültig.")
        params = urlencode({"q": query.strip(), "type": "artist,album,playlist,track", "limit": limit})
        request = Request(
            f"{SEARCH_URL}?{params}",
            headers={"Accept": "application/json", "Authorization": f"Bearer {access_token}"},
        )
        try:
            with self._requester(request, timeout=self._timeout) as response:
                raw = response.read(64 * 1024)
        except Exception as exc:
            raise SpotifySearchError(
                f"Die Spotify-Suche ist nicht erreichbar ({type(exc).__name__}: {exc})."
            ) from exc
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SpotifySearchError("Spotify lieferte keine gültige Suche.") from exc
        results: list[SpotifySearchResult] = []
        for key in ("tracks", "albums", "playlists", "artists"):
            section = payload.get(key) if isinstance(payload, dict) else None
            items = section.get("items") if isinstance(section, dict) else None
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get("uri"), str):
                    continue
                title = item.get("name") or item.get("title")
                if not isinstance(title, str):
                    continue
                artists = item.get("artists")
                artist_names = ', '.join(str(a.get("name")) for a in artists if isinstance(a, dict) and isinstance(a.get("name"), str)) if isinstance(artists, list) else ''
                subtitle = artist_names or key.rstrip('s').capitalize()
                images = item.get("album", {}).get("images") if isinstance(item.get("album"), dict) else item.get("images")
                image_url = images[0].get("url") if isinstance(images, list) and images and isinstance(images[0], dict) and isinstance(images[0].get("url"), str) else None
                results.append(SpotifySearchResult(item["uri"], title, subtitle, image_url))
        return tuple(results[:limit])


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
