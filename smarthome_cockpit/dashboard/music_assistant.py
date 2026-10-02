"""Bounded server-side Music Assistant REST adapter (no playback by default)."""
from __future__ import annotations

import json
import os
import re
from urllib.parse import unquote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

PAGE_SIZE = 50
MAX_JSON = 2 * 1024 * 1024
MAX_IMAGE = 4 * 1024 * 1024
LOCAL_DOMAINS = {"filesystem_local", "filesystem_smb", "filesystem_nfs"}


class MusicAssistantError(Exception):
    """Safe user-facing error; upstream bodies and credentials never escape."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _relative_path(value: object) -> bool:
    if not isinstance(value, str) or not value or len(value) > 2048:
        return False
    decoded = unquote(value)
    return not (decoded.startswith(("/", "\\")) or "\\" in decoded or ":" in decoded
                or any(ord(c) < 32 for c in decoded)
                or any(p in {".", ".."} for p in decoded.split("/")))


def _valid_uri(uri: object) -> bool:
    if not isinstance(uri, str) or len(uri) > 2300:
        return False
    # MA generates case-sensitive shortuuid instance IDs (e.g. filesystem_smb--GyceRM3q).
    match = re.fullmatch(r"([A-Za-z0-9_-]+)://track/(.+)", uri)
    if not match:
        return False
    provider, item = match.groups()
    if provider == "library":
        return item.isascii() and item.isdecimal()
    return any(provider == domain or provider.startswith(domain + "--")
               for domain in LOCAL_DOMAINS) and _relative_path(item)


class MusicAssistant:
    def __init__(self):
        self.url = os.getenv("MUSIC_ASSISTANT_URL", "").rstrip("/")
        self.token = os.getenv("MUSIC_ASSISTANT_TOKEN", "")
        self.allow_playback = os.getenv("MUSIC_ASSISTANT_ALLOW_PLAYBACK", "").casefold() == "true"
        self.configured = bool(self.url and self.token)
        try:
            parsed = urlsplit(self.url)
            self.valid_config = (parsed.scheme in {"http", "https"} and bool(parsed.hostname)
                                 and not parsed.username and not parsed.password
                                 and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment
                                 and not any(ord(c) < 32 for c in self.token + self.url))
        except ValueError:
            self.valid_config = False
        self.opener = build_opener(_NoRedirect())

    def _request(self, path, data=None, limit=MAX_JSON):
        if not self.configured or not self.valid_config:
            raise MusicAssistantError("Music Assistant ist nicht gültig konfiguriert.")
        request = Request(self.url + path, data=data, headers={
            "Authorization": "Bearer " + self.token,
            "Content-Type": "application/json", "Accept": "application/json",
        }, method="POST" if data is not None else "GET")
        try:
            with self.opener.open(request, timeout=8) as response:
                body = response.read(limit + 1)
                if len(body) > limit:
                    raise MusicAssistantError("Music-Assistant-Antwort ist zu groß.")
                return body, response.headers.get("Content-Type", "").split(";", 1)[0]
        except MusicAssistantError:
            raise
        except (OSError, ValueError) as exc:
            raise MusicAssistantError("Music Assistant ist nicht erreichbar oder hat die Anfrage abgelehnt.") from exc

    def _call(self, command, **args):
        # HTTP REST returns the bare command result, unlike the WebSocket envelope.
        body, _ = self._request("/api", json.dumps({
            "message_id": "cockpit", "command": command, "args": args,
        }).encode("utf-8"))
        try:
            result = json.loads(body)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise MusicAssistantError("Ungültige Music-Assistant-Antwort.") from exc
        if isinstance(result, dict) and ("error" in result or "error_code" in result):
            raise MusicAssistantError("Music Assistant hat die Anfrage abgelehnt.")
        return result

    def status(self):
        try:
            self.players()
        except MusicAssistantError as exc:
            return {"configured": self.configured, "available": False, "error": str(exc)}
        return {"configured": True, "available": True, "allow_playback": self.allow_playback}

    def players(self):
        values = self._call("players/all")
        if not isinstance(values, list) or any(not isinstance(p, dict) for p in values):
            raise MusicAssistantError("Ungültige Music-Assistant-Playerliste.")
        return [{"id": p["player_id"], "name": str(p.get("name") or p["player_id"]),
                 "available": p.get("available") is True}
                for p in values[:512] if isinstance(p.get("player_id"), str)]

    @staticmethod
    def _local_uri(track, preferred=None):
        if not isinstance(track, dict) or track.get("media_type") != "track":
            return None
        mappings = track.get("provider_mappings", [])
        if not isinstance(mappings, list):
            return None
        for mapping in mappings:
            if not isinstance(mapping, dict) or mapping.get("provider_domain") not in LOCAL_DOMAINS:
                continue
            provider, item = mapping.get("provider_instance"), mapping.get("item_id")
            if not isinstance(provider, str) or not _relative_path(item) or mapping.get("available") is False:
                continue
            uri = provider + "://track/" + item
            if _valid_uri(uri) and (preferred is None or uri == preferred):
                return uri
        return None

    def tracks(self, query="", offset=0):
        if not isinstance(query, str) or len(query) > 200 or any(ord(c) < 32 for c in query):
            raise ValueError("Ungültiger Suchtext.")
        if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 100000:
            raise ValueError("Ungültiger Musik-Offset.")
        values = self._call("music/tracks/library_items", search=query or None,
                            limit=PAGE_SIZE, offset=offset, summary=False)
        if not isinstance(values, list):
            raise MusicAssistantError("Ungültige Music-Assistant-Titelliste.")
        tracks = []
        for track in values[:PAGE_SIZE]:
            uri = self._local_uri(track)
            if not uri:
                continue
            artists = track.get("artists", [])
            if not isinstance(artists, list):
                artists = []
            album = track.get("album")
            item = {"uri": uri, "title": str(track.get("name") or "Ohne Titel"),
                    "artist": ", ".join(str(a.get("name", "")) for a in artists if isinstance(a, dict)),
                    "album": str(album.get("name", "")) if isinstance(album, dict) else ""}
            if isinstance(track.get("duration"), (int, float)):
                item["duration"] = track["duration"]
            if self._image(track):
                item["artwork_url"] = "/api/music/artwork?" + urlencode({"uri": uri})
            tracks.append(item)
        result = {"tracks": tracks}
        if len(values) >= PAGE_SIZE:
            result["next_offset"] = offset + PAGE_SIZE
        return result

    def _track(self, uri):
        if not _valid_uri(uri):
            raise ValueError("Nur lokale Music-Assistant-Titel sind erlaubt.")
        track = self._call("music/item_by_uri", uri=uri)
        local_uri = self._local_uri(track, None if uri.startswith("library://") else uri)
        if not local_uri or (not uri.startswith("library://") and local_uri != uri):
            raise ValueError("Dieser Titel ist keine verfügbare lokale Musikdatei.")
        return track, local_uri

    def _player(self, player_id):
        if not isinstance(player_id, str) or not player_id or len(player_id) > 256:
            raise ValueError("Ungültiger Music-Assistant-Player.")
        if not any(p["id"] == player_id and p["available"] for p in self.players()):
            raise ValueError("Der Music-Assistant-Player ist unbekannt oder nicht verfügbar.")
        # MA resolves the active queue for grouped players in this command.
        queue = self._call("player_queues/get_active_queue", player_id=player_id)
        if not isinstance(queue, dict) or not isinstance(queue.get("queue_id"), str):
            raise MusicAssistantError("Für diesen Player ist keine Musikwarteschlange verfügbar.")
        return queue["queue_id"]

    def play(self, uri, player_id):
        if not self.allow_playback:
            raise MusicAssistantError("Musikwiedergabe ist nicht freigegeben.")
        _, local_uri = self._track(uri)
        queue_id = self._player(player_id)
        self._call("player_queues/play_media", queue_id=queue_id, media=local_uri, option="replace")
        return {"status": "ok"}

    def control(self, player_id, command):
        if not isinstance(command, str) or command not in {"pause", "resume", "stop"}:
            raise ValueError("Ungültiger Musikbefehl.")
        if not self.allow_playback:
            raise MusicAssistantError("Musikwiedergabe ist nicht freigegeben.")
        self._call("player_queues/" + command, queue_id=self._player(player_id))
        return {"status": "ok"}

    @staticmethod
    def _image(track):
        metadata = track.get("metadata") or {}
        images = metadata.get("images", []) if isinstance(metadata, dict) else []
        if not isinstance(images, list):
            return None
        for image in images:
            if isinstance(image, dict) and image.get("type") == "thumb":
                proxy_id = image.get("proxy_id")
                if isinstance(proxy_id, str) and re.fullmatch(r"[a-f0-9]{64}", proxy_id):
                    return "/imageproxy/" + proxy_id + "?size=256&fmt=png"
                # Older MA versions: accept only local provider-relative artwork.
                provider, path = image.get("provider"), image.get("path")
                if isinstance(provider, str) and any(provider == d or provider.startswith(d + "--")
                                                     for d in LOCAL_DOMAINS) and _relative_path(path):
                    return "/imageproxy?" + urlencode({"provider": provider, "path": path, "size": 256, "fmt": "png"})
        return None

    def artwork(self, uri):
        track, _ = self._track(uri)
        path = self._image(track)
        if not path:
            raise MusicAssistantError("Für diesen Titel ist kein lokales Cover verfügbar.")
        body, content_type = self._request(path, limit=MAX_IMAGE)
        if content_type not in {"image/png", "image/jpeg", "image/webp"}:
            raise MusicAssistantError("Ungültiges Music-Assistant-Cover.")
        return body, content_type
