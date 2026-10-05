"""Observed profile playback and collision-safe independent cockpit requests.

Status is read from Spotify, never invented from the last command.  Playback
requests use existing profile/target allowlists and existing OAuth grants.
"""
from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

from smarthome.alexa_spotify import (AlexaVoiceIdentity, AlexaVoiceIdentityStatus,
                                    AlexaSpotifyRoutingRequest, route_alexa_spotify_request)
from smarthome.spotify_alexa_commands import SpotifyAlexaCommand, SpotifyAlexaIntent
from smarthome.spotify_connect import SpotifyConnectClient
from smarthome.spotify_command_service import SpotifyCommandResult
from smarthome.spotify_routing import SpotifyRoutingStatus

MAX_JSON = 128 * 1024
MAX_ASSIGNMENTS = 8
MAX_PROFILES = 16


class SpotifyCockpitError(RuntimeError):
    """Controlled error which never includes OAuth tokens or account IDs."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def _text(value: object, *, required: bool = False) -> str | None:
    if isinstance(value, str) and value.strip() and len(value) <= 512:
        return value.strip()
    if required:
        raise SpotifyCockpitError("Spotify lieferte unvollständige Wiedergabedaten.")
    return None


class SpotifyPlayerStateClient:
    """Read an account identity and player state without changing playback."""

    def __init__(self, *, requester=None, timeout_seconds: float = 8) -> None:
        self.requester = requester or build_opener(NoRedirect()).open
        self.timeout = timeout_seconds

    def _get(self, suffix: str, token: str) -> dict | None:
        request = Request("https://api.spotify.com/v1/" + suffix,
                          headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
        try:
            with self.requester(request, timeout=self.timeout) as response:
                if response.getcode() == 204:
                    return None
                if response.getcode() != 200:
                    raise SpotifyCockpitError("Spotify-Wiedergabestatus ist nicht verfügbar.")
                raw = response.read(MAX_JSON + 1)
        except (HTTPError, URLError, OSError, TimeoutError):
            raise SpotifyCockpitError("Spotify-Wiedergabestatus ist nicht erreichbar oder nicht freigegeben.") from None
        if len(raw) > MAX_JSON:
            raise SpotifyCockpitError("Spotify-Wiedergabestatus ist zu groß.")
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            raise SpotifyCockpitError("Spotify-Wiedergabestatus ist ungültig.") from None
        if not isinstance(value, dict):
            raise SpotifyCockpitError("Spotify-Wiedergabestatus ist ungültig.")
        return value

    def account_id(self, token: str) -> str:
        value = self._get("me", token)
        return _text(value.get("id") if value else None, required=True)

    def state(self, token: str) -> dict:
        value = self._get("me/player", token)
        if value is None:
            return {"status": "idle", "is_playing": False, "active_device_name": None,
                    "track": None, "device_id": None}
        if type(value.get("is_playing")) is not bool:
            raise SpotifyCockpitError("Spotify lieferte unvollständige Wiedergabedaten.")
        device = value.get("device")
        if not isinstance(device, dict):
            raise SpotifyCockpitError("Spotify lieferte kein Wiedergabeziel.")
        item = value.get("item")
        track = None
        if isinstance(item, dict):
            album = item.get("album") if isinstance(item.get("album"), dict) else {}
            images = album.get("images") if isinstance(album.get("images"), list) else []
            image_url = None
            for image in images[:8]:
                candidate = _text(image.get("url")) if isinstance(image, dict) else None
                if candidate and urlsplit(candidate).scheme == "https" and urlsplit(candidate).hostname:
                    image_url = candidate
                    break
            artists = item.get("artists") if isinstance(item.get("artists"), list) else []
            track = {"uri": _text(item.get("uri")), "title": _text(item.get("name")),
                     "artists": [name for artist in artists[:8] if isinstance(artist, dict)
                                 and (name := _text(artist.get("name")))], "image_url": image_url}
        return {"status": "playing" if value["is_playing"] else "paused",
                "is_playing": value["is_playing"], "active_device_name": _text(device.get("name"), required=True),
                "device_id": _text(device.get("id")), "track": track}


class SpotifyCockpitService:
    """Keep independent profiles independent and reject ambiguous device use."""

    def __init__(self, registry, targets, tokens, commands, *, player=None, devices=None) -> None:
        for profile in registry.profiles.values():
            if profile.enabled:
                for target_id in profile.allowed_targets:
                    try:
                        targets.require(target_id)
                    except ValueError:
                        raise SpotifyCockpitError("Ein freigegebenes Spotify-Ziel fehlt in der Zielkonfiguration.") from None
        self.registry, self.targets, self.tokens, self.commands = registry, targets, tokens, commands
        self.player = player or SpotifyPlayerStateClient()
        self.devices = devices or SpotifyConnectClient(timeout_seconds=8)
        # One shared lock covers preflight and every cockpit batch.  A second
        # browser cannot race an accepted batch's physical-target reservations.
        self.lock = threading.RLock()
        self.sessions: dict[str, dict] = {}
        self.pending: dict[tuple[str, str], dict] = {}
        self.status_cache: tuple[float, dict] | None = None

    @contextmanager
    def _exclusive(self):
        if not self.lock.acquire(timeout=2):
            raise SpotifyCockpitError("Ein Spotify-Auftrag läuft bereits. Prüfe den Status, bevor Du erneut startest.")
        try:
            yield
        finally:
            self.lock.release()

    def _snapshot(self, profile, now, *, catalog=False, availability=False) -> dict:
        public = {"profile_id": profile.profile_id, "display_name": profile.display_name,
                  "targets": list(profile.allowed_targets), "active_target_id": None}
        try:
            token = self.tokens.access_token(profile.connection_id, now=now)
            state = self.player.state(token)
            public.update({key: value for key, value in state.items() if key != "device_id"})
            matches = [target_id for target_id in profile.allowed_targets
                       if state["active_device_name"] and
                       self.targets.match_device_name(target_id, state["active_device_name"])]
            if len(matches) == 1:
                public["active_target_id"] = matches[0]
            if len(matches) > 1:
                raise SpotifyCockpitError("Das aktive Spotify-Ziel ist nicht eindeutig zugeordnet.")
            result = {"public": public, "token": token, "device_id": state["device_id"]}
            if catalog or availability:
                live_devices = self.devices.devices(token)
                public["available_targets"] = []
                for target_id in profile.allowed_targets:
                    target = self.targets.require(target_id)
                    try:
                        live_devices.select_any((target.spotify_device_name, *target.aliases))
                    except RuntimeError:
                        continue
                    public["available_targets"].append(target_id)
            if catalog:
                result["account_id"] = self.player.account_id(token)
                result["catalog"] = live_devices
            return result
        except (RuntimeError, ValueError):
            public.update(status="unavailable", is_playing=False, active_device_name=None, track=None,
                          available_targets=[], error="Spotify-Wiedergabestatus ist für dieses Profil nicht verfügbar.")
            return {"public": public}

    def _snapshots(self, now, *, catalog=False, availability=False) -> dict:
        profiles = [p for p in self.registry.profiles.values() if p.enabled]
        if len(profiles) > MAX_PROFILES:
            raise SpotifyCockpitError("Zu viele Spotify-Profile für die Cockpit-Steuerung.")
        with ThreadPoolExecutor(max_workers=4) as workers:
            values = list(workers.map(lambda p: self._snapshot(p, now, catalog=catalog, availability=availability), profiles))
        return {p.profile_id: value for p, value in zip(profiles, values)}

    def status(self, *, now: datetime) -> dict:
        # Cached public observations do not wait behind an in-flight write.
        cache = self.status_cache
        if cache and time.monotonic() - cache[0] < 5:
            return cache[1]
        with self._exclusive():
            cache = self.status_cache
            if cache and time.monotonic() - cache[0] < 5:
                return cache[1]
            states = self._snapshots(now, availability=True)
            targets = [{"target_id": target.target_id, "display_name": target.spotify_device_name,
                        "spotify_device_name": target.spotify_device_name, "aliases": list(target.aliases)}
                       for target in self.targets.targets.values()]
            result = {"available": any(v["public"]["status"] != "unavailable" for v in states.values()),
                    "profiles": [value["public"] for value in states.values()], "targets": targets,
                    # Keep the wire field for old clients but do not expose
                    # past searches/commands as if they were current playback.
                    "sessions": []}
            self.status_cache = (time.monotonic(), result)
            return result

    def _assignments(self, raw: object) -> list[dict]:
        if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_ASSIGNMENTS:
            raise SpotifyCockpitError("Ein bis acht unabhängige Spotify-Aufträge sind erforderlich.")
        result, seen_profiles, seen_targets = [], set(), set()
        for item in raw:
            if not isinstance(item, dict) or set(item) != {"profile", "target", "track"}:
                raise SpotifyCockpitError("Spotify-Aufträge benötigen genau Profil, Ziel und Titel.")
            if any(not isinstance(item[k], str) or not item[k].strip() or len(item[k]) > 512
                   for k in ("profile", "target", "track")):
                raise SpotifyCockpitError("Spotify-Profil, Ziel und Titel sind ungültig.")
            profile = self.registry.resolve_alias(item["profile"])
            target_id = self.targets.resolve_alias(item["target"])
            if not profile or not profile.enabled or not target_id or target_id not in profile.allowed_targets:
                raise SpotifyCockpitError("Spotify-Profil oder Ziel ist nicht freigegeben.")
            if profile.profile_id in seen_profiles:
                raise SpotifyCockpitError("Ein Spotify-Profil kann nur ein aktives Wiedergabeziel haben. Nutze eine Echo-Gruppe oder ein anderes Profil.")
            if target_id in seen_targets:
                raise SpotifyCockpitError("Zwei Profile können nicht gleichzeitig dieselbe Alexa verwenden.")
            seen_profiles.add(profile.profile_id)
            seen_targets.add(target_id)
            result.append({**item, "profile_id": profile.profile_id, "target_id": target_id,
                           "status": "not_attempted", "playback_verified": False})
        return result

    def play_command(self, command, *, voice_identity, session, now):
        """Legacy Alexa commands share the same target-collision boundary."""
        requested_target = (self.targets.resolve_alias(command.target_alias) or command.target_alias
                            if command.target_alias is not None else None)
        routed = route_alexa_spotify_request(self.registry, AlexaSpotifyRoutingRequest(
            voice_identity=voice_identity, explicit_profile=command.profile_alias,
            requested_target=requested_target, remember_for_session=command.remember_for_session), session)
        if routed.decision.status is not SpotifyRoutingStatus.ROUTED:
            return SpotifyCommandResult(routed.decision.status, routed.decision.profile_id, routed.decision.target_id)
        outcome = self.play([{"profile": routed.decision.profile_id, "target": routed.decision.target_id,
                              "track": command.media_query}], now=now)
        if outcome["status"] != "accepted":
            raise SpotifyCockpitError("Spotify hat den Wiedergabeauftrag abgelehnt.")
        return SpotifyCommandResult(SpotifyRoutingStatus.ROUTED, routed.decision.profile_id, routed.decision.target_id)

    def play(self, assignments: object, *, now: datetime) -> dict:
        requests = self._assignments(assignments)
        started = time.monotonic()
        with self._exclusive():
            # Reading *all* enabled profiles also protects an existing stream
            # in another room. An unknown profile state is not assumed idle.
            states = self._snapshots(now, catalog=True)
            unavailable = [self.registry.profiles[key].display_name for key, value in states.items()
                           if value["public"]["status"] == "unavailable"]
            if unavailable:
                raise SpotifyCockpitError("Der Wiedergabestatus von " + ", ".join(unavailable) +
                                          " ist nicht verfügbar; Zielkonflikte können nicht sicher geprüft werden.")
            pending_now = time.monotonic()
            self.pending = {key: pending for key, pending in self.pending.items()
                            if pending["expires"] > pending_now}
            accounts, devices = set(), set()
            for request in requests:
                selected = states[request["profile_id"]]
                if selected["account_id"] in accounts:
                    raise SpotifyCockpitError("Die gewählten Profile gehören zum selben Spotify-Konto; unabhängige Wiedergabe ist damit nicht möglich.")
                accounts.add(selected["account_id"])
                target = self.targets.require(request["target_id"])
                try:
                    device = selected["catalog"].select_any((target.spotify_device_name, *target.aliases))
                except RuntimeError:
                    raise SpotifyCockpitError("Dieses Echo ist in Spotify nicht sichtbar oder nicht steuerbar. Aktiviere es zuerst in Alexa oder Spotify.") from None
                if device.device_id in devices:
                    raise SpotifyCockpitError("Die gewählten Ziele verweisen auf dieselbe Alexa.")
                devices.add(device.device_id)
                for pending in self.pending.values():
                    if pending["profile_id"] == request["profile_id"] and pending["device_id"] == device.device_id:
                        continue
                    if (pending["target_id"] == request["target_id"] or pending["device_id"] == device.device_id
                            or pending["account_id"] == selected["account_id"]):
                        raise SpotifyCockpitError("Für diese Alexa oder dieses Spotify-Konto wurde gerade ein Start angefordert. Prüfe zuerst den Wiedergabestatus.")
                for other_id, other in states.items():
                    if other_id == request["profile_id"] or not other["public"]["is_playing"]:
                        continue
                    same_target = (other["public"]["active_target_id"] == request["target_id"] or
                                   other["device_id"] == device.device_id or
                                   (other["public"]["active_device_name"] and self.targets.match_device_name(
                                       request["target_id"], other["public"]["active_device_name"])))
                    if same_target:
                        raise SpotifyCockpitError("Auf diesem Ziel spielt bereits ein anderes Profil. Wähle eine andere Alexa.")
                    if other["account_id"] == selected["account_id"]:
                        raise SpotifyCockpitError("Ein anderes Profil verwendet bereits dasselbe Spotify-Konto.")

            # Never start a late command after a caller has reasonably timed
            # out during account discovery. Each subsequent transport is 8 s.
            if time.monotonic() - started > 25:
                raise SpotifyCockpitError("Die Spotify-Vorprüfung dauerte zu lange; es wurde kein Titel gestartet.")
            reservations = {}
            for request in requests:
                selected = states[request["profile_id"]]
                target = self.targets.require(request["target_id"])
                device = selected["catalog"].select_any((target.spotify_device_name, *target.aliases))
                reservation = {"profile_id": request["profile_id"], "target_id": request["target_id"],
                               "device_id": device.device_id, "account_id": selected["account_id"],
                               "expires": time.monotonic() + 45}
                # Replacing an explicit retry on the same physical device is
                # safe; an unresolved start on another device must not vanish.
                self.pending[(request["profile_id"], device.device_id)] = reservation
                reservations[request["profile_id"]] = reservation
            self.status_cache = None

            def start(request):
                try:
                    command = SpotifyAlexaCommand(SpotifyAlexaIntent.PLAY, media_query=request["track"],
                                                  profile_alias=request["profile_id"], target_alias=request["target_id"])
                    result = self.commands.play(command, voice_identity=AlexaVoiceIdentity(AlexaVoiceIdentityStatus.ABSENT), now=now)[0]
                    if getattr(result.status, "value", result.status) != "routed":
                        raise SpotifyCockpitError("Spotify hat den Wiedergabeauftrag abgelehnt.")
                    return {**request, "status": "accepted"}
                except (RuntimeError, ValueError):
                    # A transport exception may happen after a successful PUT.
                    # Keep its private reservation and verify the player rather
                    # than automatically retrying or taking over the same Echo.
                    return {**request, "status": "failed", "error": "Spotify hat den Start nicht bestätigt. Prüfe den Wiedergabestatus, bevor Du erneut startest."}

            # Every job belongs to a distinct verified Spotify account. Sending
            # the jobs independently does not transfer one account between rooms.
            with ThreadPoolExecutor(max_workers=4) as workers:
                results = list(workers.map(start, requests))
            for result in results:
                if result["status"] == "accepted":
                    self.sessions[result["profile_id"]] = dict(result)
                    # Spotify status is eventually consistent. Keep a short
                    # reservation while an accepted start becomes observable.
                    reservations[result["profile_id"]]["expires"] = time.monotonic() + 45
            successes = sum(r["status"] == "accepted" for r in results)
            return {"status": "accepted" if successes == len(results) else "partial" if successes else "failed",
                    "assignments": results}
