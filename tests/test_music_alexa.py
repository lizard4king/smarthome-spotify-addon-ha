"""Offline protocol/security tests; never contact Amazon, MA or real speakers."""
from __future__ import annotations

import base64
import asyncio
import json
import gzip
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

from music_alexa import security
from music_alexa.companion import Companion, Settings, json_limited, mappings
from music_alexa.security import AlexaVerifier, Grants, Rejected, certificate_url, stream_target, validate_chain

SKILL = "amzn1.ask.skill.12345678-1234-1234-1234-123456789abc"
ROWS = [{"device_id": "amzn1.ask.device.living", "user_id": "amzn1.ask.account.owner", "player_id": "Living Room"},
        {"device_id": "amzn1.ask.device.office", "user_id": "amzn1.ask.account.owner", "player_id": "Office"}]
ORIGIN = "http://ma.internal:8097"
PATH = "/flow/session/Living%20Room/item/Living%20Room.mp3"
CERT_URL = "https://s3.amazonaws.com/echo.api/test-cert.pem"


@pytest.fixture
def signer(tmp_path):
    now = datetime.now(timezone.utc)
    root_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Offline test root")])
    root = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(root_key.public_key())
            .serial_number(1).not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
            .sign(root_key, hashes.SHA256()))
    ca_file = tmp_path / "test-ca.pem"
    ca_file.write_bytes(root.public_bytes(serialization.Encoding.PEM))

    def make(san="echo-api.amazon.com", expired=False):
        leaf = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, san)]))
                .issuer_name(name).public_key(leaf_key.public_key()).serial_number(2)
                .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(seconds=-1 if expired else 3600))
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.SubjectAlternativeName([x509.DNSName(san)]), critical=False)
                .sign(root_key, hashes.SHA256()))
        return leaf.public_bytes(serialization.Encoding.PEM) + root.public_bytes(serialization.Encoding.PEM)
    return leaf_key, make, str(ca_file)


def payload(device=ROWS[0]["device_id"], user=ROWS[0]["user_id"], intent="PlayAudio", request_id="req-1"):
    return {"version": "1.0", "context": {"System": {"application": {"applicationId": SKILL},
                                                     "device": {"deviceId": device}, "user": {"userId": user}}},
            "request": {"type": "IntentRequest", "requestId": request_id,
                        "timestamp": datetime.now(timezone.utc).isoformat(), "intent": {"name": intent}}}


def signed(value, key):
    body = json.dumps(value, separators=(",", ":")).encode()
    sig = key.sign(body, padding.PKCS1v15(), hashes.SHA256())
    return body, {"SignatureCertChainUrl": CERT_URL, "Signature-256": base64.b64encode(sig).decode(),
                  "Content-Type": "application/json"}


def config(tmp_path, **changes):
    return Settings(skill_id=SKILL, app_username="private", app_password="a-test-password-32-characters",
                    ma_api_url="http://ma.internal:8095", ma_api_token="offline-test-token",
                    ma_stream_origin=ORIGIN, public_base_url="https://alexa.example.test",
                    device_mappings=ROWS, data_dir=tmp_path, **changes)


def test_certificate_chain_valid_and_untrusted(signer):
    _, make, ca_file = signer
    assert isinstance(validate_chain(make(), datetime.now(timezone.utc), ca_file=ca_file), rsa.RSAPublicKey)
    with pytest.raises(Rejected):
        validate_chain(make(), datetime.now(timezone.utc))


@pytest.mark.parametrize("san,expired", [("attacker.example", False), ("echo-api.amazon.com", True)])
def test_certificate_san_and_expiry(signer, san, expired):
    _, make, ca_file = signer
    with pytest.raises(Rejected):
        validate_chain(make(san, expired), datetime.now(timezone.utc), ca_file=ca_file)


@pytest.mark.parametrize("value", ["http://s3.amazonaws.com/echo.api/c.pem", "https://evil.test/echo.api/c.pem",
                                    "https://s3.amazonaws.com:80/echo.api/c.pem", "https://user@s3.amazonaws.com/echo.api/c.pem",
                                    "https://s3.amazonaws.com/echo.api/../../other.pem", "https://s3.amazonaws.com/ECHO.api/c.pem",
                                    "https://s3.amazonaws.com/echo.api/c.pem?url=evil", "", "https://s3.amazonaws.com/echo.api/%5cc.pem"])
def test_certificate_url_rejects(value):
    with pytest.raises(Rejected):
        certificate_url(value)


def test_certificate_url_normalized():
    assert certificate_url("https://S3.AMAZONAWS.COM:443/echo.api/../echo.api/c.pem#fragment") == "https://s3.amazonaws.com/echo.api/c.pem"


@pytest.mark.parametrize("value", ["http://other.internal:8097" + PATH, ORIGIN + "/command/session/Living%20Room/next.mp3",
                                    ORIGIN + "/source/session/source/Living%20Room.mp3", ORIGIN + PATH + "?url=evil",
                                    ORIGIN + PATH + "#frag", ORIGIN + PATH.replace("Living%20Room.mp3", "Unknown.mp3"),
                                    ORIGIN + PATH.replace("/item/", "/../"), ORIGIN + PATH.replace("/item/", "/%252e%252e/"),
                                    ORIGIN + PATH.replace("/item/", "/%2fitem/"), ORIGIN + PATH.replace(".mp3", ".flac"),
                                    ORIGIN + PATH.replace("/Living%20Room/item/", "/Office/item/")])
def test_stream_allowlist(value):
    with pytest.raises(Rejected):
        stream_target(value, ORIGIN, {"Living Room", "Office"})


def test_grants_expire_and_never_store_plain_token():
    clock = [0]
    grants = Grants(60, clock=lambda: clock[0])
    target = stream_target(ORIGIN + PATH, ORIGIN, {"Living Room"})
    token = grants.issue(target, ROWS[0]["device_id"])
    assert token not in grants.items
    assert grants.get(token).device_id == ROWS[0]["device_id"]
    clock[0] = 60
    with pytest.raises(Rejected):
        grants.get(token)
    with pytest.raises(Rejected):
        grants.get("../" + token)


def test_config_fails_closed_and_duplicate_mapping(tmp_path):
    settings = config(tmp_path)
    assert "offline-test-token" not in repr(settings)
    with pytest.raises(Rejected):
        Settings(skill_id="", app_username="x", app_password="", ma_api_url="http://ma:8095",
                 ma_api_token="", ma_stream_origin=ORIGIN, public_base_url="http://public")
    with pytest.raises(Rejected):
        mappings([ROWS[0], ROWS[0]])


@pytest_asyncio.fixture
async def service(tmp_path, signer, monkeypatch):
    key, make, ca_file = signer
    original_validate = security.validate_chain
    monkeypatch.setattr(security, "validate_chain", lambda pem, now: original_validate(pem, now, ca_file=ca_file))
    calls = []

    async def upstream_stream(request):
        calls.append((request.method, request.path, dict(request.headers)))
        if request.match_info["session"] == "slow" and request.method == "GET":
            response = web.StreamResponse(headers={"Content-Type": "audio/mpeg"})
            await response.prepare(request)
            try:
                for _ in range(200):
                    await response.write(b"audio")
                    await asyncio.sleep(0.01)
            except ConnectionResetError:
                pass
            return response
        if request.headers.get("Range") == "bytes=2-5":
            return web.Response(body=b"2345", status=206, headers={"Content-Range": "bytes 2-5/10", "Accept-Ranges": "bytes"}, content_type="audio/mpeg")
        return web.Response(body=b"0123456789", content_type="audio/mpeg")

    async def upstream_api(request):
        body = await request.json()
        calls.append(("API", body, dict(request.headers)))
        if body["command"] == "player_queues/get_active_queue":
            return web.json_response({"queue_id": body["args"]["player_id"], "current_index": 3})
        return web.json_response(None)

    upstream_app = web.Application()
    upstream_app.router.add_get("/flow/{session}/{queue}/{item}/{player}.mp3", upstream_stream)
    upstream_app.router.add_post("/api", upstream_api)
    async with TestServer(upstream_app) as upstream, aiohttp.ClientSession() as real_session:
        class OfflineSession:
            def request(self, method, url, **kwargs):
                assert url.startswith(ORIGIN + "/flow/")
                return real_session.request(method, upstream.make_url(url[len(ORIGIN):]), **kwargs)

            def post(self, url, **kwargs):
                assert url == "http://ma.internal:8095/api"
                return real_session.post(upstream.make_url("/api"), **kwargs)

            def get(self, *args, **kwargs):
                raise AssertionError("Tests must not download certificates or use live URLs")

        session = OfflineSession()
        verifier = AlexaVerifier(session, SKILL)
        verifier.cache[CERT_URL] = (time.monotonic() + 900, make())
        companion = Companion(config(tmp_path), session, verifier=verifier)
        async with TestClient(TestServer(companion.private_app())) as private, TestClient(TestServer(companion.public_app())) as public:
            yield companion, private, public, calls, key


def private_headers():
    raw = b"private:a-test-password-32-characters"
    return {"Authorization": "Basic " + base64.b64encode(raw).decode(), "Content-Type": "application/json"}


async def prepare(private, path=PATH):
    return await private.post("/ma/push-url", headers=private_headers(), json={"streamUrl": ORIGIN + path,
                              "title": "not publicly logged", "imageUrl": "https://do-not-expose.test/cover"})


@pytest.mark.asyncio
async def test_two_devices_never_get_other_rooms_stream(service):
    companion, private, public, calls, key = service
    assert (await prepare(private)).status == 200
    assert (await prepare(private, "/flow/session2/Office/item2/Office.mp3")).status == 200
    tokens = []
    for index, row in enumerate(ROWS):
        body, headers = signed(payload(row["device_id"], row["user_id"], request_id=f"room-{index}"), key)
        response = await public.post("/", data=body, headers=headers)
        assert response.status == 200
        stream = (await response.json())["response"]["directives"][0]["audioItem"]["stream"]
        token = stream["token"]
        tokens.append(token)
        assert companion.grants.get(token).target.player_id == row["player_id"]
        assert "Living" not in stream["url"] and "Office" not in stream["url"]
    assert tokens[0] != tokens[1]


@pytest.mark.asyncio
async def test_private_routes_do_not_exist_on_public_listener(service):
    _, private, public, _, _ = service
    assert (await private.get("/status")).status == 401
    assert (await private.get("/status", headers=private_headers())).status == 200
    for path in ("/status", "/devices", "/ma/push-url", "/alexa/intents", "/setup", "/simulator", "/docs", "/command/session/Office/next.mp3"):
        assert (await public.get(path)).status == 404
    assert (await public.get("/")).status == 405
    assert (await private.post("/", data=b"{}")).status == 401
    assert (await private.get("/alexa/intents", headers=private_headers())).status == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["tampered", "no_signature", "simulator", "old", "future", "skill", "user", "unpaired"])
async def test_public_validation_rejects(service, change):
    _, private, public, _, key = service
    await prepare(private)
    value = payload()
    if change in {"old", "future"}:
        value["request"]["timestamp"] = (datetime.now(timezone.utc) + timedelta(seconds=151 if change == "future" else -151)).isoformat()
    if change == "skill":
        value["context"]["System"]["application"]["applicationId"] += "invalid"
    if change == "user":
        value["context"]["System"]["user"]["userId"] = "amzn1.ask.account.other"
    if change == "unpaired":
        value["context"]["System"]["device"]["deviceId"] = "amzn1.ask.device.other"
    body, headers = signed(value, key)
    if change == "tampered":
        body += b" "
    if change == "no_signature":
        headers.pop("Signature-256")
    if change == "simulator":
        headers["X-Simulator-Bypass"] = "true"
    response = await public.post("/", data=body, headers=headers)
    assert response.status == (403 if change in {"user", "unpaired"} else 400)


@pytest.mark.asyncio
async def test_range_head_expiry_and_no_bearer_leak(service):
    companion, private, public, calls, key = service
    await prepare(private)
    body, headers = signed(payload(), key)
    result = await (await public.post("/", data=body, headers=headers)).json()
    token = result["response"]["directives"][0]["audioItem"]["stream"]["token"]
    route = "/stream/" + token + ".mp3"
    head = await public.head(route)
    assert head.status == 200 and await head.read() == b""
    response = await public.get(route, headers={"Range": "bytes=2-5", "Authorization": "Bearer evil"})
    assert response.status == 206 and await response.read() == b"2345"
    assert response.headers["Content-Range"] == "bytes 2-5/10"
    assert response.headers["Cache-Control"] == "no-store"
    assert calls[-1][2]["Range"] == "bytes=2-5" and "Authorization" not in calls[-1][2]
    assert (await public.get(route, headers={"Range": "bytes=1-2,3-4"})).status == 416
    companion.grants.clock = lambda: time.monotonic() + 901
    assert (await public.get(route)).status == 403


@pytest.mark.asyncio
async def test_command_retry_once_and_previous_index(service):
    _, private, public, calls, key = service
    await prepare(private)
    body, headers = signed(payload(intent="AMAZON.PreviousIntent"), key)
    first = await (await public.post("/", data=body, headers=headers)).json()
    assert await (await public.post("/", data=body, headers=headers)).json() == first
    api = [call for call in calls if call[0] == "API"]
    assert len(api) == 2
    assert api[1][1]["command"] == "player_queues/play_index"
    assert api[1][1]["args"] == {"queue_id": "Living Room", "index": 2}
    assert api[0][2]["Authorization"] == "Bearer offline-test-token"
    assert api[0][1]["message_id"]


@pytest.mark.asyncio
async def test_pause_never_calls_ma_recursively(service):
    _, _, public, calls, key = service
    body, headers = signed(payload(intent="AMAZON.PauseIntent"), key)
    result = await (await public.post("/", data=body, headers=headers)).json()
    assert result["response"]["directives"] == [{"type": "AudioPlayer.Stop"}]
    assert not calls


@pytest.mark.asyncio
@pytest.mark.parametrize("audio_state,expected", [
    pytest.param({"offsetInMilliseconds": 1250}, 1250, id="reported-offset"),
    pytest.param({"offsetInMilliseconds": 86400000}, 86400000, id="maximum-offset"),
    pytest.param("missing", 0, id="missing-state"),
    pytest.param({}, 0, id="missing-offset"),
    pytest.param(None, 0, id="null-state"),
    pytest.param([], 0, id="malformed-state"),
    pytest.param({"offsetInMilliseconds": None}, 0, id="null-offset"),
    pytest.param({"offsetInMilliseconds": -1}, 0, id="negative-offset"),
    pytest.param({"offsetInMilliseconds": 86400001}, 0, id="oversized-offset"),
    pytest.param({"offsetInMilliseconds": True}, 0, id="boolean-offset"),
    pytest.param({"offsetInMilliseconds": 1.5}, 0, id="float-offset"),
    pytest.param({"offsetInMilliseconds": "1250"}, 0, id="string-offset"),
])
async def test_resume_uses_audio_player_context_or_safe_zero(service, audio_state, expected):
    _, private, public, calls, key = service
    assert (await prepare(private)).status == 200
    value = payload(intent="AMAZON.ResumeIntent")
    # AudioPlayer is a sibling of System. A wrongly nested value must never win.
    value["context"]["System"]["AudioPlayer"] = {"offsetInMilliseconds": 9999}
    if audio_state != "missing":
        value["context"]["AudioPlayer"] = audio_state
    body, headers = signed(value, key)
    response = await public.post("/", data=body, headers=headers)
    assert response.status == 200
    result = await response.json()
    stream = result["response"]["directives"][0]["audioItem"]["stream"]
    assert stream["offsetInMilliseconds"] == expected
    assert not calls  # Resume does not recurse back into the native MA provider.


@pytest.mark.asyncio
async def test_private_intents_are_native_provider_commands_not_import_model(service):
    _, private, _, _, _ = service
    response = await private.get("/alexa/intents", headers=private_headers())
    assert response.status == 200
    data = await response.json()
    model = json.loads((Path(__file__).parents[1] / "music_alexa/models/de-DE.json").read_text(encoding="utf-8"))
    invocation = model["interactionModel"]["languageModel"]["invocationName"]
    assert data["locale"] == "de-DE" and data["invocationName"] == "sag " + invocation
    # MA get_intent_utterance() reads "intent"/"utterances", then concatenates.
    commands = {row["intent"]: data["invocationName"] + " " + row["utterances"][0] for row in data["intents"]}
    assert commands == {
        "AMAZON.PauseIntent": "sag music assistant pausieren",
        "AMAZON.ResumeIntent": "sag music assistant fortsetzen",
        "AMAZON.StopIntent": "sag music assistant stopp",
    }


@pytest.mark.asyncio
async def test_persistent_mapping_replacement_revokes_grants(service, tmp_path):
    companion, private, public, _, key = service
    await prepare(private)
    body, headers = signed(payload(), key)
    result = await (await public.post("/", data=body, headers=headers)).json()
    token = result["response"]["directives"][0]["audioItem"]["stream"]["token"]
    response = await private.post("/devices", headers=private_headers(), json=[ROWS[1]])
    assert response.status == 200
    assert json.loads((tmp_path / "devices.json").read_text())["mappings"] == [ROWS[1]]
    assert (await public.get("/stream/" + token + ".mp3")).status == 403
    assert not companion.pending


def test_german_model_contains_provider_phrase_and_controls():
    model = json.loads((Path(__file__).parents[1] / "music_alexa/models/de-DE.json").read_text(encoding="utf-8"))
    language = model["interactionModel"]["languageModel"]
    assert language["invocationName"] == "music assistant"
    intents = {row["name"]: row for row in language["intents"]}
    assert "spiele audio" in intents["PlayAudio"]["samples"]
    assert "AMAZON.NextIntent" in intents and "AMAZON.PreviousIntent" in intents


class TestContent:
    __test__ = False
    def __init__(self, data):
        self.data = data

    async def iter_chunked(self, size):
        for start in range(0, len(self.data), min(size, 100)):
            yield self.data[start:start + min(size, 100)]


class TestResponse:
    __test__ = False
    def __init__(self, data, *, status=200, headers=None):
        self.content = TestContent(data)
        self.status = status
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


@pytest.mark.asyncio
async def test_certificate_download_is_bounded_no_redirect_and_cached(signer, monkeypatch):
    key, make, ca_file = signer
    original = security.validate_chain
    monkeypatch.setattr(security, "validate_chain", lambda pem, now: original(pem, now, ca_file=ca_file))
    calls = []

    class Session:
        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            return TestResponse(make())

    verifier = AlexaVerifier(Session(), SKILL)
    value = payload()
    body, headers = signed(value, key)
    await verifier.verify(headers, body, value)
    await verifier.verify(headers, body, value)
    assert len(calls) == 1 and calls[0][1]["allow_redirects"] is False
    assert calls[0][1]["headers"] == {"Accept-Encoding": "identity"}


@pytest.mark.asyncio
@pytest.mark.parametrize("data,status,headers", [(b"x" * 65537, 200, {}), (b"", 302, {}),
                                                 (b"invalid", 200, {}), (b"x", 200, {"Content-Encoding": "gzip"})],
                         ids=["oversized", "redirect", "malformed", "compressed"])
async def test_bad_certificate_download_fails_closed(signer, data, status, headers):
    key, _, _ = signer
    class Session:
        def get(self, *args, **kwargs):
            return TestResponse(data, status=status, headers=headers)
    value = payload()
    body, signed_headers = signed(value, key)
    with pytest.raises(Rejected):
        await AlexaVerifier(Session(), SKILL).verify(signed_headers, body, value)


@pytest.mark.asyncio
async def test_ma_json_explicit_gzip_and_raw_and_decoded_limits():
    assert await json_limited(TestResponse(b'{"current_index":3}')) == {"current_index": 3}
    zipped = gzip.compress(b'{"current_index":3}')
    assert await json_limited(TestResponse(zipped, headers={"Content-Encoding": "gzip"})) == {"current_index": 3}
    for response in (TestResponse(b"x" * 1025),
                     TestResponse(gzip.compress(b"x" * 10000), headers={"Content-Encoding": "gzip"}),
                     TestResponse(zipped[:-4], headers={"Content-Encoding": "gzip"}),
                     TestResponse(zipped, headers={"Content-Encoding": "br"})):
        with pytest.raises(Rejected):
            await json_limited(response, limit=1024)


@pytest.mark.asyncio
async def test_unverified_requests_do_not_consume_verified_global_budget(service):
    companion, _, public, _, key = service
    for _ in range(61):
        assert (await public.post("/", data=b"{}", headers={"Content-Type": "application/json"})).status == 400
    assert not companion.request_times
    body, headers = signed(payload(intent="AMAZON.HelpIntent"), key)
    assert (await public.post("/", data=body, headers=headers)).status == 200


@pytest.mark.asyncio
async def test_parallel_retry_executes_command_once(service):
    _, private, public, calls, key = service
    await prepare(private)
    body, headers = signed(payload(intent="AMAZON.NextIntent"), key)
    replies = await asyncio.gather(*(public.post("/", data=body, headers=headers) for _ in range(2)))
    assert all(reply.status == 200 for reply in replies)
    assert len([call for call in calls if call[0] == "API"]) == 2
    assert [call[1]["command"] for call in calls if call[0] == "API"] == ["player_queues/get_active_queue", "player_queues/next"]


@pytest.mark.asyncio
async def test_replacing_mapping_closes_active_stream(service):
    companion, private, public, _, key = service
    await prepare(private, PATH.replace("/session/", "/slow/"))
    body, headers = signed(payload(), key)
    result = await (await public.post("/", data=body, headers=headers)).json()
    token = result["response"]["directives"][0]["audioItem"]["stream"]["token"]
    stream = await public.get("/stream/" + token + ".mp3")
    assert await stream.content.read(5)
    assert companion.open_streams
    assert (await private.post("/devices", headers=private_headers(), json=[ROWS[1]])).status == 200
    with pytest.raises(aiohttp.ClientPayloadError):
        await asyncio.wait_for(stream.read(), timeout=1)
    for _ in range(50):
        if not companion.open_streams:
            break
        await asyncio.sleep(0.01)
    assert not companion.open_streams and companion.active_streams == 0


class QueuedLock(asyncio.Lock):
    """Expose acquisition attempts without sleeps or private asyncio internals."""
    def __init__(self):
        super().__init__()
        self.attempts = asyncio.Queue()

    async def acquire(self):
        self.attempts.put_nowait(asyncio.current_task())
        return await super().acquire()

    async def attempted(self):
        return await asyncio.wait_for(self.attempts.get(), timeout=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("intent", ["AMAZON.NextIntent", "AMAZON.PreviousIntent"])
@pytest.mark.parametrize("replacement", ["removed", "other_player"])
async def test_mapping_commit_rejects_prevalidated_queued_command(service, intent, replacement):
    companion, private, public, calls, key = service
    lock = companion.dispatch_lock = QueuedLock()
    await lock.acquire()
    await lock.attempted()
    selected = [ROWS[1]] if replacement == "removed" else [dict(ROWS[0], player_id="New Room"), ROWS[1]]
    update = asyncio.ensure_future(private.post("/devices", headers=private_headers(), json=selected))
    await lock.attempted()  # The admin commit is queued first.
    body, headers = signed(payload(intent=intent), key)
    command = asyncio.ensure_future(public.post("/", data=body, headers=headers))
    await lock.attempted()  # Signature and old mapping are already validated.
    lock.release()
    changed, denied = await asyncio.wait_for(asyncio.gather(update, command), timeout=2)
    assert changed.status == 200 and denied.status == 403
    assert not calls  # Neither the old nor the newly associated player is touched.


@pytest.mark.asyncio
async def test_mapping_commit_waits_for_inflight_command_effect(service, monkeypatch):
    companion, private, public, calls, key = service
    lock = companion.dispatch_lock = QueuedLock()
    queue_read, continue_command = asyncio.Event(), asyncio.Event()
    original = companion.ma_command

    async def gated(command, args):
        result = await original(command, args)
        if command == "player_queues/get_active_queue":
            queue_read.set()
            await continue_command.wait()
        return result

    monkeypatch.setattr(companion, "ma_command", gated)
    body, headers = signed(payload(intent="AMAZON.PreviousIntent"), key)
    command = asyncio.ensure_future(public.post("/", data=body, headers=headers))
    await lock.attempted()
    await asyncio.wait_for(queue_read.wait(), timeout=2)
    update = asyncio.ensure_future(private.post("/devices", headers=private_headers(), json=[ROWS[1]]))
    await lock.attempted()
    assert not update.done() and companion.devices[ROWS[0]["device_id"]] == ROWS[0]
    assert len(calls) == 1
    continue_command.set()
    controlled, changed = await asyncio.wait_for(asyncio.gather(command, update), timeout=2)
    assert controlled.status == changed.status == 200
    assert [call[1]["command"] for call in calls] == ["player_queues/get_active_queue", "player_queues/play_index"]
    assert ROWS[0]["device_id"] not in companion.devices
    assert all(call[1]["args"].get("queue_id", "Living Room") == "Living Room" for call in calls)


@pytest.mark.asyncio
async def test_mapping_commit_prevents_waiting_stream_registration(service):
    companion, private, public, calls, key = service
    await prepare(private)
    body, headers = signed(payload(), key)
    result = await (await public.post("/", data=body, headers=headers)).json()
    token = result["response"]["directives"][0]["audioItem"]["stream"]["token"]
    lock = companion.dispatch_lock = QueuedLock()
    await lock.acquire()
    await lock.attempted()
    update = asyncio.ensure_future(private.post("/devices", headers=private_headers(), json=[ROWS[1]]))
    await lock.attempted()
    stream = asyncio.ensure_future(public.get("/stream/" + token + ".mp3"))
    await lock.attempted()
    lock.release()
    changed, denied = await asyncio.wait_for(asyncio.gather(update, stream), timeout=2)
    assert changed.status == 200 and denied.status == 403
    assert not calls and not companion.open_streams
    assert companion.active_streams == 0 and not companion.active_grants
