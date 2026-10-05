"""Bounded server-side Music Assistant REST adapter (no playback by default)."""
from __future__ import annotations

import json
import math
import os
import re
import time
from urllib.parse import unquote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

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
        self.library_provider = os.getenv("MUSIC_ASSISTANT_LIBRARY_PROVIDER", "").strip()
        self.configured = bool(self.url and self.token)
        try:
            parsed = urlsplit(self.url)
            self.valid_config = (parsed.scheme in {"http", "https"} and bool(parsed.hostname)
                                 and not parsed.username and not parsed.password
                                 and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment
                                 and not any(ord(c) < 32 for c in self.token + self.url)
                                 and (not self.library_provider or any(
                                     re.fullmatch(re.escape(domain) + r"(?:--[A-Za-z0-9_-]+)?", self.library_provider)
                                     for domain in LOCAL_DOMAINS)))
        except ValueError:
            self.valid_config = False
        self.opener = build_opener(ProxyHandler({}), _NoRedirect())

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
        values = self._player_states()
        return [{"id": p["player_id"], "name": str(p.get("name") or p["player_id"]),
                 "available": p.get("available") is True}
                for p in values]

    def _player_states(self):
        values = self._call("players/all")
        if not isinstance(values, list) or any(not isinstance(p, dict) for p in values):
            raise MusicAssistantError("Ungültige Music-Assistant-Playerliste.")
        return [p for p in values[:512] if isinstance(p.get("player_id"), str)]

    @staticmethod
    def _local_uri(track, preferred=None, provider_filter=None):
        if not isinstance(track, dict) or track.get("media_type") != "track":
            return None
        mappings = track.get("provider_mappings", [])
        if not isinstance(mappings, list):
            return None
        for mapping in mappings:
            if not isinstance(mapping, dict) or mapping.get("provider_domain") not in LOCAL_DOMAINS:
                continue
            provider, item = mapping.get("provider_instance"), mapping.get("item_id")
            if provider_filter and provider != provider_filter:
                continue
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
        args = {"search": query or None, "limit": PAGE_SIZE, "offset": offset, "summary": False}
        if self.library_provider:
            args["provider"] = self.library_provider
        values = self._call("music/tracks/library_items", **args)
        if not isinstance(values, list):
            raise MusicAssistantError("Ungültige Music-Assistant-Titelliste.")
        tracks = []
        for track in values[:PAGE_SIZE]:
            uri = self._local_uri(track, provider_filter=self.library_provider)
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
        local_uri = self._local_uri(track, None if uri.startswith("library://") else uri,
                                    self.library_provider)
        if not local_uri or (not uri.startswith("library://") and local_uri != uri):
            raise ValueError("Dieser Titel ist keine verfügbare lokale Musikdatei.")
        return track, local_uri

    def _verified_player(self, player_id):
        if not isinstance(player_id, str) or not player_id or len(player_id) > 256:
            raise ValueError("Ungültiger Music-Assistant-Player.")
        states = self._player_states()
        player = next((p for p in states if p["player_id"] == player_id and p.get("available") is True), None)
        if player is None:
            raise ValueError("Der Music-Assistant-Player ist unbekannt oder nicht verfügbar.")
        return player, states

    def _player(self, player_id, for_play=False):
        player, states = self._verified_player(player_id)
        # MA resolves the active queue for grouped players in this command.
        queue = self._call("player_queues/get_active_queue", player_id=player_id)
        if queue is None and for_play:
            # An external source has no active MA queue. Follow only known group/leader
            # relationships before explicitly replacing/adding local media.
            by_id = {p["player_id"]: p for p in states}
            seen = set()
            while player["player_id"] not in seen:
                seen.add(player["player_id"])
                related = next((player.get(key) for key in ("synced_to", "active_group")
                                if player.get(key) and player.get(key) != player["player_id"]), None)
                if not related:
                    break
                if related not in by_id or by_id[related].get("available") is not True:
                    raise MusicAssistantError("Das aktive Gruppenziel ist nicht verfügbar.")
                player = by_id[related]
            else:
                raise MusicAssistantError("Die Playergruppe ist ungültig.")
            queue = self._call("player_queues/get", queue_id=player["player_id"])
        if not isinstance(queue, dict) or not isinstance(queue.get("queue_id"), str) or not queue["queue_id"] or queue.get("available") is False:
            raise MusicAssistantError("Für diesen Player ist keine Musikwarteschlange verfügbar.")
        return queue["queue_id"]

    def play(self, uri, player_id, option="replace"):
        if not isinstance(option, str) or option not in {"replace", "add"}:
            raise ValueError("Ungültige Musikwarteschlangen-Option.")
        if not self.allow_playback:
            raise MusicAssistantError("Musikwiedergabe ist nicht freigegeben.")
        _, local_uri = self._track(uri)
        queue_id = self._player(player_id, for_play=True)
        self._call("player_queues/play_media", queue_id=queue_id, media=local_uri, option=option)
        return {"status": "ok"}

    def control(self, player_id, command, position=None):
        if not isinstance(command, str) or command not in {"pause", "resume", "stop", "previous", "next", "seek"}:
            raise ValueError("Ungültiger Musikbefehl.")
        if not self.allow_playback:
            raise MusicAssistantError("Musikwiedergabe ist nicht freigegeben.")
        if command == "seek" and (isinstance(position, bool) or not isinstance(position, int) or position < 0):
            raise ValueError("Ungültige Wiedergabeposition.")
        status = self.queue(player_id)
        if not status["controls"][command]:
            raise ValueError("Dieser Befehl ist für die lokale Musikwarteschlange nicht verfügbar.")
        args = {"queue_id": status["queue_id"]}
        if command == "seek":
            if position > status["current_track"]["duration"]:
                raise ValueError("Die Wiedergabeposition liegt außerhalb des Titels.")
            args["position"] = position
        self._call("player_queues/" + command, **args)
        return {"status": "ok"}

    @staticmethod
    def _number(value, default=0):
        return value if not isinstance(value, bool) and isinstance(value, (int, float)) and 0 <= value <= 1e12 and math.isfinite(value) else default

    def _queue_track(self, item, index=None):
        if not isinstance(item, dict) or item.get("available") is False:
            return None
        track = item.get("media_item")
        if not isinstance(track, dict):
            return None
        # A library item can have both cloud and local mappings. Stream provenance,
        # when present, determines which source is actually playing.
        stream = item.get("streamdetails")
        source = stream.get("provider") if isinstance(stream, dict) else None
        source = source or track.get("provider")
        if source and source != "library" and (not isinstance(source, str) or not any(
                source == domain or source.startswith(domain + "--") for domain in LOCAL_DOMAINS)):
            return None
        if self.library_provider and source and source != "library" and source != self.library_provider:
            return None
        if source in {None, "library"}:
            mappings = track.get("provider_mappings")
            if isinstance(mappings, list) and any(isinstance(mapping, dict) and
                    mapping.get("provider_domain") not in LOCAL_DOMAINS for mapping in mappings):
                return None
        source_filter = self.library_provider or (source if source != "library" else None)
        uri = self._local_uri(track, provider_filter=source_filter)
        if not uri:
            return None
        artists = track.get("artists")
        album = track.get("album")
        result = {"uri": uri, "title": str(track.get("name") or item.get("name") or "Ohne Titel"),
                  "artist": ", ".join(str(a.get("name") or "") for a in artists if isinstance(a, dict)) if isinstance(artists, list) else "",
                  "album": str(album.get("name") or "") if isinstance(album, dict) else "",
                  "duration": self._number(item.get("duration"), self._number(track.get("duration")))}
        if index is not None:
            result["index"] = index
        if self._image(track):
            result["artwork_url"] = "/api/music/artwork?" + urlencode({"uri": uri})
        return result

    def queue(self, player_id):
        player, states = self._verified_player(player_id)
        result = {"available": True, "player_id": player_id, "queue_id": None, "active": False,
                  "state": "idle", "own_music": False, "items": 0, "current_index": None,
                  "elapsed_time": 0, "current_track": None, "tracks": [],
                  "controls": {c: False for c in ("pause", "resume", "stop", "previous", "next", "seek")}}
        queue = self._call("player_queues/get_active_queue", player_id=player_id)
        if queue is None:
            return result
        if not isinstance(queue, dict) or not isinstance(queue.get("queue_id"), str) or not queue["queue_id"]:
            raise MusicAssistantError("Ungültige Music-Assistant-Warteschlange.")
        queue_id = queue["queue_id"]
        index = queue.get("current_index")
        index = index if isinstance(index, int) and not isinstance(index, bool) and index >= 0 else None
        count = queue.get("items")
        count = count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else 0
        offset = max(0, (index or 0) - 10)
        values = self._call("player_queues/items", queue_id=queue_id, limit=PAGE_SIZE, offset=offset) if count else []
        if not isinstance(values, list):
            raise MusicAssistantError("Ungültige Music-Assistant-Warteschlangenliste.")
        normalized = [self._queue_track(item, offset + i) for i, item in enumerate(values[:PAGE_SIZE])]
        current = self._queue_track(queue.get("current_item"))
        active = queue.get("active") is True and queue.get("available") is not False
        raw_state = queue.get("state")
        known_state = isinstance(raw_state, str) and raw_state in {"playing", "paused", "idle"}
        state = raw_state if known_state else "unknown"
        elapsed = self._number(queue.get("elapsed_time"))
        updated = self._number(queue.get("elapsed_time_last_updated"))
        if state == "playing" and updated and 0 <= time.time() - updated <= 60:
            elapsed += time.time() - updated
        if current and current["duration"]:
            elapsed = min(elapsed, current["duration"])
        own = current is not None and bool(normalized) and all(item is not None for item in normalized)
        result.update(queue_id=queue_id, active=active, state=state, own_music=own, items=count,
                      current_index=index, elapsed_time=elapsed, current_track=current,
                      tracks=[item for item in normalized if item is not None])
        if offset + len(values[:PAGE_SIZE]) < count:
            result["next_offset"] = offset + len(values[:PAGE_SIZE])
        if not (self.allow_playback and own and known_state and queue.get("available") is not False):
            return result
        queue_player = next((p for p in states if p["player_id"] == queue_id), player)
        features = queue_player.get("supported_features", [])
        features = features if isinstance(features, list) else []
        positions = {item["index"] for item in normalized if item is not None}
        result["controls"].update(pause=active and state == "playing" and "pause" in features,
                                  resume=state in {"paused", "idle"}, stop=active and state in {"playing", "paused"},
                                  previous=active and index is not None and (elapsed >= 5 or index - 1 in positions),
                                  next=active and index is not None and index + 1 in positions,
                                  seek=active and "seek" in features and current["duration"] > 0 and state in {"playing", "paused"})
        return result

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
