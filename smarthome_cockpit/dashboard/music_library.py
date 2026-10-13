"""Read-only, provider-bound album and filesystem collections for Music Assistant."""
from __future__ import annotations

import math
import re
import time
from urllib.parse import unquote

from dashboard.music_assistant import LOCAL_DOMAINS, PAGE_SIZE, MusicAssistantError, _relative_path, _valid_uri, validate_search_page

MAX_TRACKS = 1000
MAX_FOLDERS = 100
MAX_DEPTH = 20
MAX_BROWSE_ITEMS = 5000
MAX_COLLECTION_SECONDS = 20


def _text(value, fallback=""):
    return value if isinstance(value, str) and value else fallback


def _artist(item):
    artists = item.get("artists")
    return ", ".join(_text(a.get("name")) for a in artists if isinstance(a, dict)) if isinstance(artists, list) else ""


def _natural(value):
    return tuple((1, len(p.lstrip("0")), p.lstrip("0")) if p.isascii() and p.isdecimal() else (0, p.casefold())
                 for p in re.split(r"([0-9]+)", value))


def _position(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _direct_child(parent, relative):
    """Return a decoded direct child's name, or an empty string for other paths."""
    relative = unquote(relative)
    tail = relative[len(parent) + 1:] if parent and relative.startswith(parent + "/") else relative if not parent else ""
    return tail if tail and "/" not in tail else ""


class MusicLibrary:
    """Never store media, change playback, or browse an unconfigured provider."""

    def __init__(self, client):
        self.client = client

    def _provider(self):
        provider = self.client.library_provider
        if not isinstance(provider, str) or not any(
            re.fullmatch(re.escape(domain) + r"--[A-Za-z0-9_-]+", provider)
            for domain in LOCAL_DOMAINS
        ):
            raise MusicAssistantError("Alben und Ordner benötigen eine konkrete lokale Provider-Instanz (Domain--Instanz-ID).")
        return provider

    def _folder_path(self, path):
        provider = self._provider()
        if path == "":
            return provider + "://"
        if not isinstance(path, str) or not path.startswith(provider + "://"):
            raise ValueError("Nur Ordner der konfigurierten Musikquelle sind erlaubt.")
        relative = path[len(provider) + 3:]
        if relative and (not _relative_path(relative) or any(not p for p in unquote(relative).split("/"))):
            raise ValueError("Ungültiger Musikordner.")
        return path

    def _track(self, item):
        if not isinstance(item, dict) or item.get("media_type") != "track" or item.get("available") is False:
            return None
        provider = self._provider()
        # Full Track objects have mappings; ItemMapping from filesystem browse has a URI.
        if "provider_mappings" in item:
            try:
                uri = self.client.local_uri(item)
            except (TypeError, ValueError, AttributeError):
                return None
        else:
            uri = item.get("uri")
            if item.get("provider") != provider:
                return None
        if not _valid_uri(uri) or not uri.startswith(provider + "://track/"):
            return None
        album = item.get("album")
        result = {"uri": uri, "title": _text(item.get("name"), "Ohne Titel"),
                  "artist": _artist(item), "album": _text(album.get("name")) if isinstance(album, dict) else ""}
        duration = item.get("duration")
        if not isinstance(duration, bool) and isinstance(duration, (int, float)) and 0 <= duration <= 1e12 and math.isfinite(duration):
            result["duration"] = duration
        return result

    def albums(self, query="", offset=0):
        validate_search_page(query, offset)
        provider = self._provider()
        values = self.client.call_library("music/albums/library_items", summary=False, provider=provider,
                                   limit=PAGE_SIZE, offset=offset, search=query or None)
        if not isinstance(values, list) or len(values) > PAGE_SIZE:
            raise MusicAssistantError("Ungültige Music-Assistant-Albenliste.")
        items = []
        for album in values:
            if not isinstance(album, dict) or album.get("media_type") != "album":
                continue
            uri = album.get("uri")
            mappings = album.get("provider_mappings")
            if not isinstance(uri, str) or not re.fullmatch(r"library://album/[0-9]+", uri):
                continue
            if not isinstance(mappings, list) or not any(
                isinstance(m, dict) and m.get("provider_instance") == provider
                and isinstance(m.get("provider_domain"), str) and m["provider_domain"] in LOCAL_DOMAINS
                and m.get("available") is not False
                and _relative_path(m.get("item_id")) for m in mappings
            ):
                continue
            items.append({"kind": "album", "uri": uri, "title": _text(album.get("name"), "Ohne Titel"),
                          "artist": _artist(album)})
        result = {"items": items}
        if len(values) == PAGE_SIZE:
            result["next_offset"] = offset + PAGE_SIZE
        return result

    @staticmethod
    def _budget(deadline):
        if deadline is not None and time.monotonic() >= deadline:
            raise MusicAssistantError("Das Zeitlimit für die Musiksammlung wurde überschritten.")

    def _browse(self, path, strict=False, deadline=None):
        self._budget(deadline)
        values = self.client.call_library("music/browse", path=self._folder_path(path))
        self._budget(deadline)
        if not isinstance(values, list) or len(values) > MAX_BROWSE_ITEMS:
            raise MusicAssistantError("Der Musikordner ist ungültig oder zu groß.")
        parent = unquote(path.split("://", 1)[1])
        items = []
        for item in values:
            # MA prepends navigation, not media. Match the complete server contract:
            # '..' alone is not sufficient evidence that a folder is navigation.
            if self._navigation(item, path):
                continue
            normalized = None
            if isinstance(item, dict) and item.get("media_type") == "folder":
                try:
                    folder = self._folder_path(item.get("path"))
                    # A browse result must be a direct descendant, never a sibling or parent.
                    tail = _direct_child(parent, folder.split("://", 1)[1])
                    if tail and item.get("provider") == self._provider():
                        normalized = {"kind": "folder", "uri": folder, "title": _text(item.get("name"), tail)}
                except ValueError:
                    pass
            elif isinstance(item, dict) and item.get("media_type") == "track":
                track = self._track(item)
                if track:
                    if _direct_child(parent, track["uri"].split("://track/", 1)[1]):
                        normalized = {"kind": "track", **track}
            if normalized:
                items.append(normalized)
            elif strict:
                # Playlist files are intentionally rejected too: a browse listing
                # does not prove their complete, provider-bound track contents.
                raise MusicAssistantError("Der Ordner enthält nicht vollständig nutzbare lokale Musikobjekte.")
        return sorted(items, key=lambda i: (_natural(i["title"]), i["uri"]))

    def _navigation(self, item, path):
        if not isinstance(item, dict) or item.get("media_type") != "folder" or item.get("name") != "..":
            return False
        relative = path.split("://", 1)[1]
        if not relative:
            return item.get("item_id") == "root" and item.get("provider") == "library" and item.get("path") == "root"
        parent = self._provider() + "://" + "/".join(relative.split("/")[:-1])
        return item.get("item_id") == "back" and item.get("provider") == self._provider() and item.get("path") == parent

    def folders(self, path="", query="", offset=0):
        validate_search_page(query, offset)
        path = self._folder_path(path)
        items = self._browse(path)
        if query:
            items = [i for i in items if query.casefold() in (i["title"] + " " + i.get("artist", "")).casefold()]
        result = {"path": path, "items": items[offset:offset + PAGE_SIZE]}
        if offset + PAGE_SIZE < len(items):
            result["next_offset"] = offset + PAGE_SIZE
        return result

    def collection(self, uri):
        self._provider()
        deadline = time.monotonic() + MAX_COLLECTION_SECONDS
        if isinstance(uri, str) and re.fullmatch(r"library://album/[0-9]+", uri):
            self._budget(deadline)
            values = self.client.call_library("music/albums/album_tracks", item_id=uri.rsplit("/", 1)[1],
                                       provider_instance_id_or_domain="library", in_library_only=True)
            self._budget(deadline)
            if not isinstance(values, list) or not values or len(values) > MAX_TRACKS:
                raise MusicAssistantError("Das Album ist leer, ungültig oder zu groß.")
            tracks = []
            for item in sorted(values, key=lambda t: (_position(t.get("disc_number")), _position(t.get("track_number")),
                                                      _natural(_text(t.get("name"))), _text(t.get("uri"))) if isinstance(t, dict) else (0, 0, (), "")):
                track = self._track(item)
                if not track:
                    raise MusicAssistantError("Das Album ist nicht vollständig in der lokalen Musikquelle verfügbar.")
                tracks.append(track)
            self._budget(deadline)
            return {"title": tracks[0]["album"] or "Album", "tracks": tracks, "complete": True}
        path = self._folder_path(uri)
        tracks = []
        visited = set()

        def visit(folder, depth):
            if folder in visited or depth > MAX_DEPTH or len(visited) >= MAX_FOLDERS:
                raise MusicAssistantError("Die Ordnersammlung ist zu groß oder zu tief verschachtelt.")
            visited.add(folder)
            for item in self._browse(folder, strict=True, deadline=deadline):
                if item["kind"] == "folder":
                    visit(item["uri"], depth + 1)
                else:
                    tracks.append({k: v for k, v in item.items() if k != "kind"})
                    if len(tracks) > MAX_TRACKS:
                        raise MusicAssistantError("Die Sammlung enthält mehr als 1000 Titel.")

        visit(path, 0)
        if not tracks:
            raise MusicAssistantError("Der Musikordner enthält keine Titel.")
        self._budget(deadline)
        return {"title": path.split("://", 1)[1].rsplit("/", 1)[-1] or "Musikbibliothek",
                "tracks": tracks, "complete": True}
