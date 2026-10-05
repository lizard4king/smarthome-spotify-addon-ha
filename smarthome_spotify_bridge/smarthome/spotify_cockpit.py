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
from urllib.parse import urlsplit, urlencode
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
ACTION_KEYS = {"pause": "pausing", "resume": "resuming", "next": "skipping_next",
               "previous": "skipping_prev", "seek": "seeking"}


class SpotifyCockpitError(RuntimeError):
    """Controlled error which never includes OAuth tokens or account IDs."""
    outcome = "not_sent"

    def __init__(self, message: str, *, outcome=None):
        super().__init__(message)
        if isinstance(outcome, str) and outcome in {"not_sent", "unknown"}:
            self.outcome = outcome


class SpotifyControlRejected(SpotifyCockpitError):
    """Spotify explicitly rejected a write; its outcome is known."""


class SpotifyControlUnknown(SpotifyCockpitError):
    """The write may have arrived; never automatically repeat it."""
    outcome = "unknown"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def _text(value: object, *, required: bool = False) -> str | None:
    if isinstance(value, str) and value.strip() and len(value) <= 512:
        return value.strip()
    if required:
        raise SpotifyCockpitError("Spotify lieferte unvollständige Wiedergabedaten.")
    return None


def _milliseconds(value: object) -> int | None:
    return value if type(value) is int and 0 <= value <= 2_147_483_647 else None


def validate_control(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise SpotifyCockpitError("Spotify-Steuerung benötigt ein JSON-Objekt.")
    action = payload.get("action")
    if not isinstance(action, str) or action not in {"pause", "resume", "next", "previous", "seek"}:
        raise SpotifyCockpitError("Die Spotify-Steueraktion ist ungültig.")
    required = {"profile", "target", "action"} | ({"position_ms", "track_uri"} if action == "seek" else set())
    if set(payload) != required or any(not _text(payload.get(k)) for k in ("profile", "target")):
        raise SpotifyCockpitError("Die Spotify-Steuerung enthält ungültige Felder.")
    if action == "seek" and _milliseconds(payload["position_ms"]) is None:
        raise SpotifyCockpitError("Die Spotify-Position muss eine nichtnegative ganze Millisekundenzahl sein.")
    if action == "seek" and (not _text(payload.get("track_uri")) or
                             not payload["track_uri"].startswith("spotify:track:")):
        raise SpotifyCockpitError("Die beobachtete Spotify-Titel-URI fehlt oder ist ungültig.")
    return payload


def _observed_start(pending: dict, profile_id: str, device_id: str, state: dict, *, account_id=None) -> bool:
    track = state.get("track")
    if (pending.get("accepted") is not True or pending["profile_id"] != profile_id or
            pending["device_id"] != device_id or state.get("is_playing") is not True):
        return False
    if account_id is not None and pending["account_id"] != account_id:
        return False
    if pending.get("expected_track_uri"):
        return isinstance(track, dict) and track.get("uri") == pending["expected_track_uri"]
    if pending.get("expected_context_uri"):
        return state.get("context_uri") == pending["expected_context_uri"]
    # A successful free-text start has no fixed URI exposed by the existing
    # command service. Acceptance plus this account's observed exact device
    # is sufficient; unsuccessful starts never enter this branch.
    return True


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

    def control(self, token: str, device_id: str, action: str, *, position_ms=None) -> None:
        if not _text(device_id):
            raise SpotifyCockpitError("Ein eindeutiges Spotify-Wiedergabeziel fehlt.")
        suffix, method = {"pause": ("pause", "PUT"), "resume": ("play", "PUT"),
                          "next": ("next", "POST"), "previous": ("previous", "POST"),
                          "seek": ("seek", "PUT")}[action]
        query = {"device_id": device_id}
        if action == "seek":
            query["position_ms"] = position_ms
        request = Request("https://api.spotify.com/v1/me/player/" + suffix + "?" + urlencode(query),
                          data=b"", method=method,
                          headers={"Authorization": "Bearer " + token, "Content-Length": "0"})
        try:
            with self.requester(request, timeout=self.timeout) as response:
                if response.getcode() != 204:
                    if 400 <= response.getcode() < 500:
                        raise SpotifyControlRejected("Spotify hat die Steueraktion ausdrücklich abgelehnt.")
                    raise SpotifyControlUnknown("Der Ausgang der Spotify-Steueraktion ist unbekannt. Prüfe den Wiedergabestatus; wiederhole den Auftrag nicht automatisch.")
        except HTTPError as exc:
            if 400 <= exc.code < 500:
                raise SpotifyControlRejected("Spotify hat die Steueraktion ausdrücklich abgelehnt.") from None
            raise SpotifyControlUnknown("Der Ausgang der Spotify-Steueraktion ist unbekannt. Prüfe den Wiedergabestatus; wiederhole den Auftrag nicht automatisch.") from None
        except (URLError, OSError, TimeoutError):
            raise SpotifyControlUnknown("Der Ausgang der Spotify-Steueraktion ist unbekannt. Prüfe den Wiedergabestatus; wiederhole den Auftrag nicht automatisch.") from None

    def state(self, token: str) -> dict:
        value = self._get("me/player", token)
        if value is None:
            return {"status": "idle", "is_playing": False, "active_device_name": None,
                    "track": None, "device_id": None, "progress_ms": None}
        if type(value.get("is_playing")) is not bool:
            raise SpotifyCockpitError("Spotify lieferte unvollständige Wiedergabedaten.")
        device = value.get("device")
        if not isinstance(device, dict):
            raise SpotifyCockpitError("Spotify lieferte kein Wiedergabeziel.")
        item = value.get("item")
        actions = value.get("actions") if isinstance(value.get("actions"), dict) else {}
        disallows = actions.get("disallows") if isinstance(actions.get("disallows"), dict) else actions
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
                                 and (name := _text(artist.get("name")))], "image_url": image_url,
                     "duration_ms": _milliseconds(item.get("duration_ms"))}
        return {"status": "playing" if value["is_playing"] else "paused",
                "is_playing": value["is_playing"], "active_device_name": _text(device.get("name"), required=True),
                "device_id": _text(device.get("id")), "track": track,
                "progress_ms": _milliseconds(value.get("progress_ms")),
                "context_uri": _text(value["context"].get("uri")) if isinstance(value.get("context"), dict) else None,
                "disallowed_actions": [action for action, key in ACTION_KEYS.items() if disallows.get(key) is True]}


class SpotifyCockpitService:
    """Keep independent profiles independent and reject ambiguous device use."""

    def __init__(self, registry, targets, tokens, commands, *, player=None, devices=None) -> None:
        self.registry, self.targets, self.tokens, self.commands = registry, targets, tokens, commands
        self.player = player or SpotifyPlayerStateClient()
        device_opener = build_opener(NoRedirect())
        self.devices = devices or SpotifyConnectClient(
            timeout_seconds=8, requester=lambda request, timeout: device_opener.open(request, timeout=timeout))
        # The guard protects only local reservations and caches, never network
        # requests. Writes reserve their account/device while doing I/O.
        self.lock = threading.RLock()
        self.sessions: dict[str, dict] = {}
        self.pending: dict[tuple[str, str], dict] = {}
        self.inflight: dict[object, list[dict]] = {}
        self.account_cache: dict[str, tuple[float, str]] = {}
        self.cache_generation = 0
        self.resource_activity: dict[tuple[str, str], tuple[float, int]] = {}
        self.activity_epoch = 0
        self.status_cache: tuple[float, dict] | None = None

    @contextmanager
    def _exclusive(self):
        if not self.lock.acquire(timeout=2):
            raise SpotifyCockpitError("Ein Spotify-Auftrag läuft bereits. Prüfe den Status, bevor Du erneut startest.")
        try:
            yield
        finally:
            self.lock.release()

    def _pending_snapshot(self) -> list[dict]:
        with self._exclusive():
            return [dict(p) for p in self.pending.values() if p["expires"] > time.monotonic()]

    def _known_account(self, profile_id: str) -> str | None:
        with self._exclusive():
            cached = self.account_cache.get(profile_id)
            return cached[1] if cached and time.monotonic() - cached[0] < 5 else None

    def _invalidate(self):
        self.status_cache = None
        self.cache_generation += 1

    @contextmanager
    def _operation(self, resources: list[dict], *, preflight_started: float, preflight_epoch: int):
        marker = object()
        keys = {(kind, resource[kind]) for resource in resources
                for kind in ("account_id", "device_id", "target_id")}
        with self._exclusive():
            current_time = time.monotonic()
            if current_time - preflight_started > 25:
                raise SpotifyCockpitError("Die Spotify-Vorprüfung dauerte zu lange; es wurde kein Auftrag gesendet.")
            self.resource_activity = {key: changed for key, changed in self.resource_activity.items()
                                      if current_time - changed[0] <= 45}
            if len(set(self.resource_activity) | keys) > 512:
                raise SpotifyCockpitError("Zu viele kürzlich geänderte Spotify-Ressourcen; prüfe den Status später erneut.")
            for requested in resources:
                if any(requested["account_id"] == busy["account_id"] or
                       requested["device_id"] == busy["device_id"] or
                       requested["target_id"] == busy["target_id"]
                       for group in self.inflight.values() for busy in group):
                    raise SpotifyCockpitError("Für dieses Spotify-Konto oder Wiedergabeziel läuft bereits ein Auftrag.")
            if any(self.resource_activity.get(key, (0, -1))[1] > preflight_epoch for key in keys):
                raise SpotifyCockpitError("Dieses Spotify-Konto oder Wiedergabeziel wurde während der Vorprüfung geändert. Aktualisiere den Status.")
            self.inflight[marker] = resources
            self.activity_epoch += 1
            self.resource_activity.update({key: (current_time, self.activity_epoch) for key in keys})
            self._invalidate()
        try:
            yield
        finally:
            with self._exclusive():
                self.inflight.pop(marker, None)
                finished = time.monotonic()
                self.activity_epoch += 1
                self.resource_activity.update({key: (finished, self.activity_epoch) for key in keys})
                self._invalidate()

    def _snapshot(self, profile, now, *, catalog=False, availability=False) -> dict:
        # Legacy profile allowlists can refer to an old logical target ID.
        # Do not invent an alias or stop the bridge: keep the observed account
        # state for collision checks, but never offer an unconfigured target.
        configured_targets = [target_id for target_id in profile.allowed_targets
                              if target_id in self.targets.targets]
        missing_targets = [target_id for target_id in profile.allowed_targets
                           if target_id not in self.targets.targets]
        public = {"profile_id": profile.profile_id, "display_name": profile.display_name,
                  "targets": configured_targets, "active_target_id": None, "controls_available": False,
                  "seek_available": False}
        if missing_targets:
            public.update(unavailable_targets=missing_targets,
                          configuration_error="Ein freigegebenes Spotify-Ziel fehlt in der Zielkonfiguration.")
        try:
            token = self.tokens.access_token(profile.connection_id, now=now)
            state = self.player.state(token)
            public.update({key: value for key, value in state.items() if key != "device_id"})
            matches = [target_id for target_id in configured_targets
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
                for target_id in configured_targets:
                    target = self.targets.require(target_id)
                    try:
                        live_devices.select_any((target.spotify_device_name, *target.aliases))
                    except RuntimeError:
                        continue
                    public["available_targets"].append(target_id)
                    if target_id == public["active_target_id"]:
                        selected = live_devices.select_any((target.spotify_device_name, *target.aliases))
                        public["controls_available"] = selected.device_id == state["device_id"]
                        if any(
                               (p["profile_id"] == profile.profile_id or p["device_id"] == selected.device_id) and
                               not _observed_start(p, profile.profile_id, selected.device_id, state)
                               for p in self._pending_snapshot()):
                            public["controls_available"] = False
                        public["seek_available"] = (public["controls_available"] and
                            "seek" not in state.get("disallowed_actions", []) and
                            isinstance(state.get("track"), dict) and
                            (_milliseconds(state["track"].get("duration_ms")) or 0) > 0)
            if catalog:
                result["account_id"] = self.player.account_id(token)
                result["catalog"] = live_devices
                with self._exclusive():
                    self.account_cache[profile.profile_id] = (time.monotonic(), result["account_id"])
            return result
        except (RuntimeError, ValueError):
            public.update(status="unavailable", is_playing=False, active_device_name=None, track=None,
                          active_target_id=None, controls_available=False, seek_available=False, progress_ms=None,
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
        with self._exclusive():
            cache = self.status_cache
            if cache and time.monotonic() - cache[0] < 5:
                return cache[1]
            generation = self.cache_generation
        states = self._snapshots(now, availability=True)
        unavailable = any(value["public"]["status"] == "unavailable" for value in states.values())
        with self._exclusive():
            busy_resources = [dict(resource) for group in self.inflight.values() for resource in group]
        for profile_id, selected in states.items():
            public = selected["public"]
            active_target = public["active_target_id"]
            known_account = self._known_account(profile_id)
            conflict = any(other_id != profile_id and (
                           (known_account is not None and known_account == self._known_account(other_id)) or
                           (other["public"]["is_playing"] and (
                            (selected.get("device_id") is not None and selected.get("device_id") == other.get("device_id")) or
                            (active_target is not None and active_target == other["public"]["active_target_id"]))))
                           for other_id, other in states.items())
            busy = any(resource["device_id"] == selected.get("device_id") or
                       resource["target_id"] == active_target or
                       (known_account is not None and resource["account_id"] == known_account)
                       for resource in busy_resources)
            if conflict or busy:
                public["controls_available"] = False
                public["seek_available"] = False
            if unavailable and public["status"] != "unavailable":
                public["disallowed_actions"] = sorted(set(public.get("disallowed_actions", [])) |
                                                       {"resume", "next", "previous"})
                public["degradation_note"] = "Ein anderes Profil ist nicht verfügbar. Nur Pause und Positionswechsel auf dem verifizierten Ziel sind freigegeben."
        targets = [{"target_id": target.target_id, "display_name": target.spotify_device_name,
                    "spotify_device_name": target.spotify_device_name, "aliases": list(target.aliases)}
                   for target in self.targets.targets.values()]
        result = {"available": any(v["public"]["status"] != "unavailable" for v in states.values()),
                  "profiles": [value["public"] for value in states.values()], "targets": targets, "sessions": []}
        with self._exclusive():
            if self.cache_generation == generation:
                self.status_cache = (time.monotonic(), result)
        return result

    def control(self, payload: object, *, now: datetime) -> dict:
        request = validate_control(payload)
        profile = self.registry.resolve_alias(request["profile"])
        target_id = self.targets.resolve_alias(request["target"])
        if not profile or not profile.enabled or not target_id or target_id not in profile.allowed_targets:
            raise SpotifyCockpitError("Spotify-Profil oder Ziel ist nicht freigegeben.")
        write_attempted = False
        try:
            with self._exclusive():
                started, preflight_epoch = time.monotonic(), self.activity_epoch
            states = self._snapshots(now, catalog=True)
            selected = states[profile.profile_id]
            action = request["action"]
            if selected["public"]["status"] == "unavailable" or (
                    action not in {"pause", "seek"} and
                    any(s["public"]["status"] == "unavailable" for s in states.values())):
                raise SpotifyCockpitError("Der Spotify-Status ist nicht vollständig verfügbar; die Steuerung wurde abgelehnt.")
            target = self.targets.require(target_id)
            device = selected["catalog"].select_any((target.spotify_device_name, *target.aliases))
            if (selected["public"]["active_target_id"] != target_id or
                    not selected["device_id"] or selected["device_id"] != device.device_id):
                raise SpotifyCockpitError("Das aktive Spotify-Ziel hat sich geändert oder ist nicht eindeutig. Aktualisiere den Status.")
            for other_id, other in states.items():
                other_account = other.get("account_id") or self._known_account(other_id)
                if other_id != profile.profile_id and (other_account == selected["account_id"] or
                        (other["public"]["is_playing"] and
                         (other.get("device_id") == device.device_id or
                          other["public"]["active_target_id"] == target_id))):
                    raise SpotifyCockpitError("Ein anderes Profil verwendet dieses Spotify-Konto oder Wiedergabeziel.")
            resources = [{"profile_id": profile.profile_id, "target_id": target_id,
                          "device_id": device.device_id, "account_id": selected["account_id"]}]
            with self._operation(resources, preflight_started=started, preflight_epoch=preflight_epoch):
                # This operation owns the selected account/device while network
                # reads and the explicitly addressed write run without the guard.
                current = self.player.state(selected["token"])
                if (current["device_id"] != device.device_id or
                        not current["active_device_name"] or
                        not self.targets.match_device_name(target_id, current["active_device_name"])):
                    raise SpotifyCockpitError("Das aktive Spotify-Ziel hat sich während der Prüfung geändert.")
                if any((p["device_id"] == device.device_id or p["account_id"] == selected["account_id"]) and
                       not _observed_start(p, profile.profile_id, device.device_id, current,
                                           account_id=selected["account_id"])
                       for p in self._pending_snapshot()):
                    raise SpotifyCockpitError("Ein Spotify-Start ist noch offen. Prüfe zuerst den Wiedergabestatus.")
                if action in current.get("disallowed_actions", []):
                    raise SpotifyCockpitError("Spotify erlaubt diese Steueraktion derzeit nicht.")
                if action == "seek":
                    track = current.get("track")
                    duration = _milliseconds(track.get("duration_ms")) if isinstance(track, dict) else None
                    if not duration or request["position_ms"] >= duration:
                        raise SpotifyCockpitError("Die Spotify-Position muss innerhalb des beobachteten Titels liegen.")
                    if track.get("uri") != request["track_uri"]:
                        raise SpotifyCockpitError("Der Spotify-Titel hat sich geändert. Aktualisiere den Status vor dem Positionswechsel.")
                if time.monotonic() - started > 25:
                    raise SpotifyCockpitError("Die Spotify-Vorprüfung dauerte zu lange; es wurde keine Steueraktion gesendet.")
                with self._exclusive():
                    for key, pending in list(self.pending.items()):
                        if _observed_start(pending, profile.profile_id, device.device_id, current,
                                           account_id=selected["account_id"]):
                            del self.pending[key]
                try:
                    write_attempted = True
                    self.player.control(selected["token"], device.device_id, action,
                                        position_ms=request.get("position_ms"))
                except (SpotifyControlUnknown, RuntimeError, ValueError) as exc:
                    if isinstance(exc, SpotifyControlRejected):
                        raise
                    with self._exclusive():
                        self.pending[(profile.profile_id, device.device_id)] = {
                            **resources[0], "accepted": False, "expires": time.monotonic() + 45}
                    if isinstance(exc, SpotifyControlUnknown):
                        raise
                    raise SpotifyControlUnknown("Der Ausgang der Spotify-Steueraktion ist unbekannt. Prüfe den Wiedergabestatus; wiederhole den Auftrag nicht automatisch.") from None
            return {"status": "accepted", "profile_id": profile.profile_id, "target_id": target_id,
                    "action": action, "playback_verified": False}
        except (RuntimeError, ValueError) as exc:
            if isinstance(exc, SpotifyControlRejected):
                raise
            if isinstance(exc, SpotifyCockpitError) and not write_attempted:
                raise
            if write_attempted:
                # Also covers an exception while releasing the local claim or
                # constructing a response after the provider call was attempted.
                try:
                    with self._exclusive():
                        self.pending[(profile.profile_id, device.device_id)] = {
                            **resources[0], "accepted": False, "expires": time.monotonic() + 45}
                except (RuntimeError, ValueError):
                    pass
                if isinstance(exc, SpotifyControlUnknown):
                    raise
                raise SpotifyControlUnknown("Der Ausgang der Spotify-Steueraktion ist unbekannt. Prüfe den Wiedergabestatus; wiederhole den Auftrag nicht automatisch.") from None
            raise SpotifyCockpitError("Spotify hat die Steueraktion abgelehnt. Prüfe den Wiedergabestatus.") from None

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
        with self._exclusive():
            started, preflight_epoch = time.monotonic(), self.activity_epoch
        # Network discovery must not hold the process-wide reservation guard.
        states = self._snapshots(now, catalog=True)
        unavailable = [self.registry.profiles[key].display_name for key, value in states.items()
                       if value["public"]["status"] == "unavailable"]
        if unavailable:
            raise SpotifyCockpitError("Der Wiedergabestatus von " + ", ".join(unavailable) +
                                      " ist nicht verfügbar; Zielkonflikte können nicht sicher geprüft werden.")
        resources, accounts, devices = [], set(), set()
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
            resources.append({"profile_id": request["profile_id"], "target_id": request["target_id"],
                              "device_id": device.device_id, "account_id": selected["account_id"]})
        with self._operation(resources, preflight_started=started, preflight_epoch=preflight_epoch):
            with self._exclusive():
                self.pending = {key: pending for key, pending in self.pending.items()
                                if pending["expires"] > time.monotonic()}
                for resource in resources:
                    for pending in self.pending.values():
                        if pending["profile_id"] == resource["profile_id"] and pending["device_id"] == resource["device_id"]:
                            continue
                        if (pending["target_id"] == resource["target_id"] or pending["device_id"] == resource["device_id"]
                                or pending["account_id"] == resource["account_id"]):
                            raise SpotifyCockpitError("Für diese Alexa oder dieses Spotify-Konto wurde gerade ein Start angefordert. Prüfe zuerst den Wiedergabestatus.")
                if time.monotonic() - started > 25:
                    raise SpotifyCockpitError("Die Spotify-Vorprüfung dauerte zu lange; es wurde kein Titel gestartet.")
                reservations = {}
                for request, resource in zip(requests, resources):
                    media = request["track"]
                    reservation = {**resource, "accepted": False,
                                   "expected_track_uri": media if media.startswith("spotify:track:") else None,
                                   "expected_context_uri": media if media.startswith(("spotify:album:", "spotify:playlist:", "spotify:artist:")) else None,
                                   "expires": time.monotonic() + 45}
                    self.pending[(resource["profile_id"], resource["device_id"])] = reservation
                    reservations[resource["profile_id"]] = reservation

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
            with self._exclusive():
                for result in results:
                    if result["status"] == "accepted":
                        self.sessions[result["profile_id"]] = dict(result)
                        reservations[result["profile_id"]]["expires"] = time.monotonic() + 45
                        reservations[result["profile_id"]]["accepted"] = True
            successes = sum(r["status"] == "accepted" for r in results)
            return {"status": "accepted" if successes == len(results) else "partial" if successes else "failed",
                    "assignments": results}
