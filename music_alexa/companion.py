"""Private Music Assistant adapter and separately routed public Alexa service."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import signal
import secrets
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web

try:
    from .security import AlexaVerifier, Grants, Rejected, origin, stream_target
except ImportError:
    from security import AlexaVerifier, Grants, Rejected, origin, stream_target

LOG = logging.getLogger("music_alexa")
MAX_BODY = 32768
MAX_MA_RESPONSE = 2 * 1024 * 1024


async def json_limited(response, limit=MAX_MA_RESPONSE):
    """Bound raw and explicitly decompressed HTTP API data before parsing JSON."""
    encoding = response.headers.get("Content-Encoding", "identity").lower()
    if encoding not in {"identity", "gzip"}:
        raise Rejected("Invalid MA response")
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS) if encoding == "gzip" else None
    chunks, raw_size, decoded_size = [], 0, 0
    try:
        async for raw in response.content.iter_chunked(8192):
            raw_size += len(raw)
            if raw_size > limit:
                raise Rejected("Invalid MA response")
            chunk = decoder.decompress(raw, limit - decoded_size + 1) if decoder else raw
            decoded_size += len(chunk)
            if decoded_size > limit or (decoder and decoder.unconsumed_tail):
                raise Rejected("Invalid MA response")
            chunks.append(chunk)
        if decoder and (not decoder.eof or decoder.unused_data):
            raise Rejected("Invalid MA response")
        return json.loads(b"".join(chunks))
    except (ValueError, zlib.error, UnicodeError):
        raise Rejected("Invalid MA response") from None


def mappings(value) -> dict[str, dict[str, str]]:
    """An explicit device/user/player association is required for every Echo."""
    if not isinstance(value, list) or len(value) > 32:
        raise Rejected("Invalid mappings")
    result, players = {}, set()
    for row in value:
        if (not isinstance(row, dict) or set(row) != {"device_id", "user_id", "player_id"}
                or not all(isinstance(v, str) and 0 < len(v) <= 1024 for v in row.values())
                or not row["device_id"].startswith("amzn1.ask.device.")
                or not row["user_id"].startswith("amzn1.ask.account.")
                or row["device_id"] in result or row["player_id"] in players
                or any(c in row["player_id"] for c in ("/", "\\", "%", "\r", "\n"))):
            raise Rejected("Invalid mappings")
        result[row["device_id"]] = dict(row)
        players.add(row["player_id"])
    return result


@dataclass(repr=False)
class Settings:
    skill_id: str
    app_username: str
    app_password: str
    ma_api_url: str
    ma_api_token: str
    ma_stream_origin: str
    public_base_url: str
    stream_grant_ttl: int = 900
    device_mappings: list = field(default_factory=list)
    data_dir: Path = Path("/data")

    def __post_init__(self):
        if (not re.fullmatch(r"amzn1\.ask\.skill\.[0-9a-fA-F-]{36}", self.skill_id)
                or not self.app_username or ":" in self.app_username
                or len(self.app_password) < 16 or not self.ma_api_token
                or not isinstance(self.stream_grant_ttl, int)
                or not 60 <= self.stream_grant_ttl <= 3600):
            raise Rejected("Incomplete or invalid configuration")
        self.ma_api_url = origin(self.ma_api_url)
        self.ma_stream_origin = origin(self.ma_stream_origin)
        self.public_base_url = origin(self.public_base_url, https=True)
        if urlsplit(self.ma_stream_origin).port != 8097 or urlsplit(self.ma_api_url).port != 8095:
            raise Rejected("Unexpected MA port")
        mappings(self.device_mappings)

    @classmethod
    def load(cls, path: Path):
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("locale", "de-DE") != "de-DE":
            raise Rejected("Invalid configuration")
        keys = {key: raw[key] for key in ("skill_id", "app_username", "app_password", "ma_api_url",
                                         "ma_api_token", "ma_stream_origin", "public_base_url")}
        return cls(**keys, stream_grant_ttl=raw.get("stream_grant_ttl", 900),
                   device_mappings=raw.get("device_mappings", []))


def speech(text: str) -> dict:
    return {"version": "1.0", "response": {"outputSpeech": {"type": "PlainText", "text": text},
                                               "shouldEndSession": True}}


def directive(value: dict | None = None) -> dict:
    response = {"shouldEndSession": True}
    if value:
        response["directives"] = [value]
    return {"version": "1.0", "response": response}


class Companion:
    def __init__(self, settings: Settings, session: aiohttp.ClientSession, *, verifier=None):
        self.settings, self.session = settings, session
        self.verifier = verifier or AlexaVerifier(session, settings.skill_id)
        self.grants = Grants(settings.stream_grant_ttl)
        self.devices = mappings(settings.device_mappings)
        persisted = settings.data_dir / "devices.json"
        if persisted.exists():
            saved = json.loads(persisted.read_text(encoding="utf-8"))
            if not isinstance(saved, dict) or saved.get("version") != 1:
                raise Rejected("Invalid persistent mapping")
            # Changing HA options deliberately supersedes an older admin mapping.
            if saved.get("options_digest") == self.options_digest():
                self.devices = mappings(saved["mappings"])
        self.seen: dict[str, str] = {}
        self.pending: dict[str, dict] = {}
        self.replies: dict[str, tuple[float, str, dict]] = {}
        self.request_times: list[float] = []
        self.pre_auth_times: dict[str, list[float]] = {}
        self.verifications = 0
        self.active_streams = 0
        self.active_grants: dict[str, int] = {}
        self.command_lock = asyncio.Lock()
        self.dispatch_lock = asyncio.Lock()
        self.open_streams: dict[asyncio.Task, tuple[str, object]] = {}

    def options_digest(self) -> str:
        value = json.dumps(self.settings.device_mappings, sort_keys=True).encode()
        return hashlib.sha256(value).hexdigest()

    @web.middleware
    async def private_auth(self, request, handler):
        try:
            auth = request.headers.get("Authorization", "")
            if not auth.startswith("Basic "):
                raise Rejected("Authentication required")
            supplied = base64.b64decode(auth[6:], validate=True)
            expected = (self.settings.app_username + ":" + self.settings.app_password).encode()
            if not hmac.compare_digest(supplied, expected):
                raise Rejected("Authentication required")
            # No browser-initiated cross-origin writes; admin calls use explicit JSON.
            if request.method != "GET" and request.headers.get("Origin"):
                raise Rejected("Origin not allowed")
        except Exception:
            return web.json_response({"error": "Authentication required"}, status=401,
                                     headers={"WWW-Authenticate": 'Basic realm="Music Alexa"'})
        return await handler(request)

    def private_app(self) -> web.Application:
        app = web.Application(client_max_size=MAX_BODY, middlewares=[safe_errors, self.private_auth])
        app.router.add_get("/", self.status)
        app.router.add_get("/status", self.status)
        app.router.add_get("/devices", self.get_devices)
        app.router.add_post("/devices", self.set_devices)
        app.router.add_get("/alexa/intents", self.intents)
        app.router.add_post("/ma/push-url", self.push)
        return app

    def public_app(self) -> web.Application:
        app = web.Application(client_max_size=MAX_BODY, middlewares=[safe_errors])
        app.router.add_post("/", self.alexa)
        app.router.add_get("/stream/{token}.mp3", self.stream)
        return app

    async def status(self, request):
        return web.json_response({"service": "music-alexa", "configured_devices": len(self.devices),
                                  "pending_players": len(self.pending), "active_streams": self.active_streams,
                                  "live_playback_verified": False})

    async def get_devices(self, request):
        return web.json_response({"mappings": list(self.devices.values()),
                                  "seen": [{"device_id": key, "user_id": value} for key, value in self.seen.items()]})

    async def set_devices(self, request):
        if request.content_type != "application/json":
            raise web.HTTPBadRequest(text="JSON required")
        try:
            value = await request.json()
            selected = mappings(value)
        except (ValueError, TypeError):
            raise web.HTTPBadRequest(text="Invalid mappings") from None
        # A successful admin response is a boundary: no dispatch on the old
        # mapping can remain in flight, and no old grant can register afterward.
        async with self.dispatch_lock:
            self.settings.data_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.settings.data_dir / "devices.json.tmp"
            tmp.write_text(json.dumps({"version": 1, "options_digest": self.options_digest(),
                                       "mappings": value}), encoding="utf-8")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.settings.data_dir / "devices.json")
            affected = {device for device in self.devices if self.devices[device] != selected.get(device)}
            self.devices = selected
            for task, (device, transport) in list(self.open_streams.items()):
                if device in affected:
                    if transport:
                        transport.close()
                    task.cancel()
            self.pending.clear()
            self.grants.items.clear()
            self.replies.clear()
        return web.json_response({"status": "ok", "configured_devices": len(selected)})

    async def intents(self, request):
        # MA concatenates this spoken-command prefix with the first utterance.
        # It is not the interaction-model invocationName ("music assistant").
        return web.json_response({"locale": "de-DE", "invocationName": "sag music assistant", "intents": [
            {"intent": "AMAZON.PauseIntent", "utterances": ["pausieren"]},
            {"intent": "AMAZON.ResumeIntent", "utterances": ["fortsetzen"]},
            {"intent": "AMAZON.StopIntent", "utterances": ["stopp"]},
        ]})

    async def push(self, request):
        if request.content_type != "application/json":
            raise web.HTTPBadRequest(text="JSON required")
        try:
            payload = await request.json()
            target = stream_target(payload["streamUrl"], self.settings.ma_stream_origin,
                                   {row["player_id"] for row in self.devices.values()})
            # Keep only short display metadata; never proxy imageUrl/public cover APIs.
            self.pending[target.player_id] = {"target": target,
                                              "received": time.monotonic()}
        except (KeyError, ValueError, TypeError):
            raise web.HTTPBadRequest(text="Invalid stream") from None
        return web.json_response({"status": "ok"})

    async def ma_command(self, command: str, args: dict):
        async with self.session.post(self.settings.ma_api_url + "/api",
                                     headers={"Authorization": "Bearer " + self.settings.ma_api_token,
                                              "Accept-Encoding": "identity"},
                                     json={"message_id": secrets.token_hex(8), "command": command, "args": args}, allow_redirects=False,
                                     timeout=aiohttp.ClientTimeout(total=2), auto_decompress=False) as response:
            if response.status != 200:
                raise Rejected("MA command failed")
            result = await json_limited(response)
            if isinstance(result, dict) and result.get("error_code"):
                raise Rejected("MA command failed")
            # HTTP /api returns the result directly, unlike the WebSocket envelope.
            return result

    def play(self, row: dict, offset: int = 0) -> dict:
        pending = self.pending.get(row["player_id"])
        if not pending or time.monotonic() - pending["received"] > 3600:
            return speech("Für diesen Lautsprecher ist noch keine Wiedergabe vorbereitet.")
        token = self.grants.issue(pending["target"], row["device_id"])
        return directive({"type": "AudioPlayer.Play", "playBehavior": "REPLACE_ALL", "audioItem": {
            "stream": {"token": token, "url": self.settings.public_base_url + "/stream/" + token + ".mp3",
                       "offsetInMilliseconds": offset}}})

    async def alexa(self, request):
        now = time.monotonic()
        peer = request.remote or "unknown"
        self.pre_auth_times = {key: values for key, values in self.pre_auth_times.items()
                               if values and now - values[-1] < 60}
        if peer not in self.pre_auth_times and len(self.pre_auth_times) >= 256:
            return web.json_response({"error": "Request limit"}, status=429)
        times = [t for t in self.pre_auth_times.get(peer, []) if now - t < 60]
        self.pre_auth_times[peer] = times
        if len(times) >= 120:
            return web.json_response({"error": "Request limit"}, status=429)
        times.append(now)
        if self.verifications >= 4:
            return web.json_response({"error": "Verifier busy"}, status=503)
        try:
            body = await request.read()
            payload = json.loads(body)
            if self.verifications >= 4:
                return web.json_response({"error": "Verifier busy"}, status=503)
            self.verifications += 1
            try:
                await self.verifier.verify(request.headers, body, payload)
            finally:
                self.verifications -= 1
            context = payload["context"]
            system = context["System"]
            device, user = system["device"]["deviceId"], system["user"]["userId"]
            if not isinstance(device, str) or not isinstance(user, str) or len(device) > 1024 or len(user) > 1024:
                raise Rejected("Invalid Alexa request")
            if len(self.seen) < 64:
                self.seen[device] = user
            row = self.devices.get(device)
            if not row or not hmac.compare_digest(row["user_id"], user):
                return web.json_response({"error": "Device not paired"}, status=403)
            self.request_times = [t for t in self.request_times if now - t < 60]
            if len(self.request_times) >= 60:
                return web.json_response({"error": "Request limit"}, status=429)
            self.request_times.append(now)
            req = payload["request"]
            request_id = req["requestId"]
            if not isinstance(request_id, str) or len(request_id) > 1024:
                raise Rejected("Invalid Alexa request")
            digest = hashlib.sha256(body).hexdigest()
            self.replies = {key: value for key, value in self.replies.items() if value[0] > now}
            # Serialize effects and mapping commits, including duplicate detection.
            # Recheck after waiting: an already verified request must not use a stale
            # association or silently switch to a newly associated player.
            async with self.dispatch_lock:
                if self.devices.get(device) != row:
                    return web.json_response({"error": "Device mapping changed"}, status=403)
                cached = self.replies.get(request_id)
                if cached:
                    if cached[1] != digest:
                        raise Rejected("Invalid Alexa request")
                    return web.json_response(cached[2])
                answer = await self.handle(req, context, row)
                if len(self.replies) < 256:
                    self.replies[request_id] = (now + 150, digest, answer)
            return web.json_response(answer)
        except (Rejected, KeyError, TypeError, ValueError, UnicodeError):
            return web.json_response({"error": "Invalid Alexa request"}, status=400)

    async def handle(self, req: dict, context: dict, row: dict) -> dict:
        request_type = req.get("type")
        if not isinstance(request_type, str):
            raise Rejected("Invalid Alexa request")
        if request_type.startswith("AudioPlayer.") or request_type == "SessionEndedRequest":
            return {"version": "1.0", "response": {}}
        intent = req.get("intent", {}).get("name") if request_type == "IntentRequest" else None
        if request_type == "LaunchRequest" or intent == "PlayAudio":
            return self.play(row)
        if intent in {"AMAZON.StopIntent", "AMAZON.CancelIntent", "AMAZON.PauseIntent"}:
            # Native MA stop/pause itself invokes these intents: never echo a MA command back.
            return directive({"type": "AudioPlayer.Stop"})
        if intent == "AMAZON.ResumeIntent":
            audio_state = context.get("AudioPlayer")
            offset = audio_state.get("offsetInMilliseconds", 0) if isinstance(audio_state, dict) else 0
            if not isinstance(offset, int) or isinstance(offset, bool) or not 0 <= offset <= 86400000:
                offset = 0
            return self.play(row, offset)
        if intent in {"AMAZON.NextIntent", "AMAZON.PreviousIntent", "AMAZON.StartOverIntent"}:
            try:
                async with self.command_lock:
                    queue = await self.ma_command("player_queues/get_active_queue", {"player_id": row["player_id"]})
                    if (not isinstance(queue, dict) or queue.get("queue_id") != row["player_id"]
                            or not isinstance(queue.get("current_index"), int)):
                        raise Rejected("No active queue")
                    if intent == "AMAZON.NextIntent":
                        # Queue-specific control cannot spill into an external active source.
                        await self.ma_command("player_queues/next", {"queue_id": queue["queue_id"]})
                    else:
                        index = max(0, queue["current_index"] - (intent == "AMAZON.PreviousIntent"))
                        await self.ma_command("player_queues/play_index", {"queue_id": queue["queue_id"], "index": index})
                # MA pushes the new stream and invokes PlayAudio itself; no stale replay here.
                return directive()
            except (Rejected, aiohttp.ClientError, asyncio.TimeoutError, KeyError):
                return speech("Music Assistant konnte den Befehl nicht ausführen.")
        return speech("Wähle die Musik und den Raum im Cockpit. Hier kannst du pausieren, fortsetzen oder weiterschalten.")

    async def stream(self, request):
        response = None
        token = request.match_info["token"]
        headers = {"Accept-Encoding": "identity"}
        if "Range" in request.headers:
            value = request.headers["Range"]
            if not re.fullmatch(r"bytes=(?:[0-9]{1,15}-[0-9]{0,15}|-[0-9]{1,15})", value):
                return web.json_response({"error": "Invalid range"}, status=416)
            headers["Range"] = value
        task = asyncio.current_task()
        # Grant validation and registration are indivisible with respect to a
        # mapping commit. Downloads never hold this lock; the commit can cancel
        # every registered transport belonging to an affected association.
        async with self.dispatch_lock:
            try:
                grant = self.grants.get(token)
                row = self.devices.get(grant.device_id)
                if not row or row["player_id"] != grant.target.player_id:
                    raise Rejected("Invalid grant")
            except Rejected:
                return web.json_response({"error": "Invalid stream grant"}, status=403)
            if self.active_streams >= 16 or self.active_grants.get(token, 0) >= 2:
                return web.json_response({"error": "Stream limit"}, status=429)
            self.active_streams += 1
            self.active_grants[token] = self.active_grants.get(token, 0) + 1
            if task:
                self.open_streams[task] = (grant.device_id, request.transport)
        try:
            async with self.session.request(request.method, self.settings.ma_stream_origin + grant.target.path,
                                            headers=headers, allow_redirects=False,
                                            timeout=aiohttp.ClientTimeout(total=None, sock_connect=5, sock_read=30)) as upstream:
                if upstream.status not in {200, 206, 416}:
                    return web.json_response({"error": "Stream unavailable"}, status=502)
                if upstream.status == 416:
                    return web.Response(status=416, headers={"Content-Range": upstream.headers.get("Content-Range", "bytes */0")})
                if upstream.content_type not in {"audio/mpeg", "audio/mp3"}:
                    return web.json_response({"error": "Stream unavailable"}, status=502)
                forwarded = {key: upstream.headers[key] for key in
                             ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges")
                             if key in upstream.headers}
                forwarded["Cache-Control"] = "no-store"
                forwarded["X-Content-Type-Options"] = "nosniff"
                response = web.StreamResponse(status=upstream.status, headers=forwarded)
                await response.prepare(request)
                if request.method == "GET":
                    async for chunk in upstream.content.iter_chunked(16384):
                        await response.write(chunk)
                await response.write_eof()
                return response
        except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionResetError, BrokenPipeError):
            if response is not None and response.prepared:
                if request.transport:
                    request.transport.close()
                return response
            return web.json_response({"error": "Stream unavailable"}, status=502)
        finally:
            if task:
                self.open_streams.pop(task, None)
            self.active_streams -= 1
            self.active_grants[token] -= 1
            if not self.active_grants[token]:
                self.active_grants.pop(token)


@web.middleware
async def safe_errors(request, handler):
    try:
        response = await handler(request)
        response.headers["Cache-Control"] = "no-store"
        return response
    except web.HTTPException as exc:
        return web.json_response({"error": "Request rejected"}, status=exc.status)
    except asyncio.CancelledError:
        raise
    except Exception:
        # Tracebacks from HTTP libraries can embed tokens and URLs; log no exception text.
        LOG.warning("Request failed")
        return web.json_response({"error": "Request failed"}, status=500)


async def main():
    settings = Settings.load(Path(os.environ.get("OPTIONS_PATH", "/data/options.json")))
    connector = aiohttp.TCPConnector(limit=40)
    async with aiohttp.ClientSession(connector=connector, trust_env=False, auto_decompress=False) as session:
        service = Companion(settings, session)
        runners = [web.AppRunner(app, access_log=None, auto_decompress=False)
                   for app in (service.private_app(), service.public_app())]
        try:
            for runner, port in zip(runners, (5000, 5001)):
                await runner.setup()
                await web.TCPSite(runner, "0.0.0.0", port).start()
            LOG.info("Private and public listeners ready; live playback not verified")
            stop = asyncio.Event()
            for signum in (signal.SIGINT, signal.SIGTERM):
                asyncio.get_running_loop().add_signal_handler(signum, stop.set)
            await stop.wait()
        finally:
            for runner in runners:
                await runner.cleanup()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("aiohttp").setLevel(logging.CRITICAL)
    try:
        asyncio.run(main())
    except Exception:
        LOG.error("Startup failed: check configuration and persistent mapping")
        raise SystemExit(1) from None
