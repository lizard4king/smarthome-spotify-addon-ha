import hashlib
import hmac
import io
import json
from email.message import Message
from pathlib import Path
import sys
import threading
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import mock_open, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dashboard import lumi_chat as module
from dashboard.lumi_chat import BridgeClient, ChatError, ChatService, COOKIE

KEY = "test-only-key-" + "a" * 32


class FakeBridge:
    def __init__(self):
        self.calls = []
        self.reply = {"state": "done", "answer": "Prüfantwort"}
        self.error = None

    def submit(self, *args):
        self.calls.append(args)
        if self.error:
            raise self.error
        return "b" * 32

    def result(self, *args):
        return self.reply


def service(clock=lambda: 0):
    client = FakeBridge()
    return ChatService(enabled=True, origin="https://cockpit.example", key=KEY, client=client, clock=clock), client


def data(n=0, text="Hallo"):
    return {"message_id": f"{n:032x}", "text": text}


def test_default_disabled_and_invalid_config_fail_closed():
    assert not ChatService().enabled
    for origin in ("http://cockpit.example", "https://x/path", "https://user:pass@x", "https://x?token=1", "https://x\n"):
        assert not ChatService(enabled=True, origin=origin, key=KEY).enabled
    assert not ChatService(enabled=True, origin="https://x", key="short").enabled


def test_server_generated_identity_and_two_turn_context_binding():
    app, bridge = service()
    one = app.session({}, create=True)
    two = app.session({}, create=True)
    assert one.sid != two.sid and one.principal != two.principal
    first = app.turn(one, data())
    assert app.result(one, {"turn_id": first["turn_id"]}) == bridge.reply
    app.turn(one, data(1, "Was habe ich gesagt?"))
    app.turn(two, data(2))
    assert bridge.calls[0][:2] == bridge.calls[1][:2]
    assert bridge.calls[2][:2] != bridge.calls[0][:2]
    assert all(len(item) == 4 and len(item[0]) == len(item[1]) == 64 for item in bridge.calls)


def test_foreign_result_extra_fields_and_csrf_are_rejected():
    app, bridge = service()
    one, two = app.session({}, create=True), app.session({}, create=True)
    turn = app.turn(one, data())
    with pytest.raises(ChatError) as error:
        app.result(two, {"turn_id": turn["turn_id"]})
    assert error.value.status == 404
    for key in ("principal", "person_key", "session_key", "allow_control", "job_id"):
        with pytest.raises(ChatError):
            app.turn(two, {**data(), key: "foreign"})
    with pytest.raises(ChatError):
        app.authorize({"X-Lumi-CSRF": one.csrf}, two)
    assert len(bridge.calls) == 1


def test_submit_deduplication_reservation_and_no_ambiguous_retry():
    app, bridge = service()
    session = app.session({}, create=True)
    first = app.turn(session, data())
    assert app.turn(session, data()) == first and len(bridge.calls) == 1
    with pytest.raises(ChatError):
        app.turn(session, data(text="Geänderter Text"))
    with pytest.raises(ChatError):
        app.turn(session, data(1))
    app.result(session, {"turn_id": first["turn_id"]})
    bridge.error = ChatError("unconfirmed", outcome="unknown")
    for _ in range(2):
        with pytest.raises(ChatError) as error:
            app.turn(session, data(2))
        assert error.value.outcome == "unknown"
    assert len(bridge.calls) == 2
    assert app.end(session) == {"ended": True, "pending": True}
    with pytest.raises(ChatError):
        app.turn(session, data(3))


def test_session_capacity_expiry_and_closed_cookie():
    now = [0]
    app, bridge = service(lambda: now[0])
    for _ in range(module.MAX_SESSIONS):
        app.session({}, create=True)
    with pytest.raises(ChatError):
        app.session({}, create=True)
    now[0] = module.IDLE_SECONDS + 1
    new = app.session({}, create=True)
    assert len(app.sessions) == 1
    assert app.session({"Cookie": f"{COOKIE}={new.sid}"}) is new
    app.end(new)
    with pytest.raises(ChatError):
        app.session({"Cookie": f"{COOKIE}={new.sid}"})


def test_deliberate_start_rotates_existing_session_without_cancel_claim():
    app, bridge = service()
    old = app.session({}, create=True)
    app.turn(old, data())
    new = app.session({"Cookie": f"{COOKIE}={old.sid}"}, create=True)
    assert new.sid != old.sid and new.memory_key != old.memory_key
    assert old.closed and len(bridge.calls) == 1


def test_inflight_result_is_not_returned_after_end():
    app, bridge = service()
    session = app.session({}, create=True)
    turn = app.turn(session, data())["turn_id"]
    entered, release = threading.Event(), threading.Event()
    def delayed_result(*args):
        entered.set()
        assert release.wait(2)
        return {"state": "done", "answer": "Später Text"}
    bridge.result = delayed_result
    errors = []
    def read_result():
        try: app.result(session, {"turn_id": turn})
        except ChatError as error: errors.append(error.code)
    worker = threading.Thread(target=read_result)
    worker.start()
    assert entered.wait(1)
    assert app.end(session)["pending"] is True
    release.set()
    worker.join(2)
    assert errors == ["session_expired"] and len(bridge.calls) == 1


@pytest.mark.parametrize("options", ["[]", "invalid json", "{}"])
def test_entrypoint_clears_stale_browser_feature_even_for_invalid_options(monkeypatch, options):
    import addon_entrypoint
    monkeypatch.setenv("LUMI_CHAT_ENABLED", "true")
    monkeypatch.setenv("LUMI_CHAT_ORIGIN", "https://old.example")
    monkeypatch.setenv("LUMI_VOICE_BRIDGE_SECRET", KEY)
    monkeypatch.setattr(addon_entrypoint, "_load_private_music_fallback", lambda _: None)
    with patch("builtins.open", mock_open(read_data=options)):
        addon_entrypoint._load_options_fallback()
    import os
    assert os.environ["LUMI_CHAT_ENABLED"] == "false"
    assert os.environ["LUMI_CHAT_ORIGIN"] == os.environ["LUMI_VOICE_BRIDGE_SECRET"] == ""


def test_bridge_protocol_authentication_fixed_origin_and_no_person_key():
    captured = []
    class Response:
        status = 202
        def __init__(self, req, corrupt=False):
            self.req = req
            self.raw = json.dumps({"job_id": "b" * 32, "state": "pending"}).encode()
            self.headers = Message()
            self.headers["Content-Type"] = "application/json"
            nonce = req.get_header("X-voice-nonce")
            signature = hmac.new(KEY.encode(), "\n".join(("RESPONSE", "/v1/submit", nonce, "202", hashlib.sha256(self.raw).hexdigest())).encode(), hashlib.sha256).hexdigest()
            self.headers["X-Voice-Response-Signature"] = "0" * 64 if corrupt else signature
        def read(self, _): return self.raw
        def geturl(self): return self.req.full_url
        def __enter__(self): return self
        def __exit__(self, *args): pass
    class Opener:
        corrupt = False
        def open(self, req, timeout):
            captured.append(req)
            return Response(req, self.corrupt)
    opener = Opener()
    client = BridgeClient(KEY, opener=opener, clock=lambda: 1000)
    assert client.submit("a" * 64, "c" * 64, "d" * 32, "Test") == "b" * 32
    req = captured[0]
    assert req.full_url == module.BRIDGE + "/v1/submit"
    value = json.loads(req.data)
    assert value["kind"] == "browser" and value["memory"] == {"person_key": "", "session_key": "c" * 64}
    canonical = "\n".join(("POST", "/v1/submit", "1000", req.get_header("X-voice-nonce"), hashlib.sha256(req.data).hexdigest()))
    assert req.get_header("X-voice-signature") == hmac.new(KEY.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    opener.corrupt = True
    with pytest.raises(ChatError) as error:
        client.submit("a" * 64, "c" * 64, "d" * 32, "Test")
    assert error.value.outcome == "unknown" and len(captured) == 2


@pytest.mark.parametrize("bad_state", [[], {}, 1, None])
def test_signed_malformed_submit_is_still_unconfirmed(bad_state):
    calls = []
    class Opener:
        def open(self, req, timeout):
            calls.append(req)
            response = io.BytesIO(json.dumps({"job_id": "b" * 32, "state": bad_state}).encode())
            response.status = 202
            response.geturl = lambda: req.full_url
            response.headers = Message()
            response.headers["Content-Type"] = "application/json"
            canonical = "\n".join(("RESPONSE", "/v1/submit", req.get_header("X-voice-nonce"), "202", hashlib.sha256(response.getvalue()).hexdigest()))
            response.headers["X-Voice-Response-Signature"] = hmac.new(KEY.encode(), canonical.encode(), hashlib.sha256).hexdigest()
            return response
    with pytest.raises(ChatError) as error:
        BridgeClient(KEY, opener=Opener()).submit("a" * 64, "c" * 64, "d" * 32, "Test")
    assert error.value.outcome == "unknown" and len(calls) == 1


@pytest.fixture
def http_app(monkeypatch):
    from dashboard.server import CockpitHandler
    server = ThreadingHTTPServer(("127.0.0.1", 0), CockpitHandler)
    origin = f"http://127.0.0.1:{server.server_port}"
    client = FakeBridge()
    app = ChatService(enabled=True, origin=origin, key=KEY, client=client, allow_http_loopback=True)
    monkeypatch.setattr(module, "SERVICE", app)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    yield origin, client
    server.shutdown()
    server.server_close()
    worker.join(2)


def post(origin, path, payload, cookie="", csrf="", **overrides):
    headers = {"Origin": origin, "Content-Type": "application/json", "X-Lumi-Client": "1"}
    if cookie: headers["Cookie"] = cookie
    if csrf: headers["X-Lumi-CSRF"] = csrf
    headers.update(overrides)
    req = Request(origin + path, json.dumps(payload).encode(), headers=headers)
    try: return urlopen(req, timeout=3)
    except HTTPError as error: return error


def test_http_session_cookie_bound_job_and_cors(http_app):
    origin, bridge = http_app
    with post(origin, "/api/lumi/session", {}) as response:
        assert response.status == 200
        cookie = response.headers["Set-Cookie"]
        assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
        assert response.headers.get("Access-Control-Allow-Origin") is None
        bootstrap = json.load(response)
    with post(origin, "/api/lumi/turn", data(), cookie, bootstrap["csrf"]) as response:
        assert response.status == 202
        turn = json.load(response)
    with post(origin, "/api/lumi/result", turn, cookie, bootstrap["csrf"]) as response:
        assert response.status == 400  # public result accepts only opaque turn_id
    with post(origin, "/api/lumi/result", {"turn_id": turn["turn_id"]}, cookie, bootstrap["csrf"]) as response:
        assert json.load(response)["answer"] == "Prüfantwort"
    with post(origin, "/api/lumi/turn", data(2), cookie, bootstrap["csrf"], Origin="https://foreign.example") as response:
        assert response.status == 403
    with post(origin, "/api/lumi/turn?bad=1", data(2), cookie, bootstrap["csrf"]) as response:
        assert response.status == 404
    assert len(bridge.calls) == 1


def test_http_csp_status_and_no_cors_options(http_app):
    origin, _ = http_app
    with urlopen(origin + "/lumi.html", timeout=3) as response:
        assert "script-src 'self'" in response.headers["Content-Security-Policy"]
        assert "microphone=(self)" in response.headers["Permissions-Policy"]
    req = Request(origin + "/api/lumi/session", method="OPTIONS")
    with pytest.raises(HTTPError) as error:
        urlopen(req, timeout=3)
    assert error.value.code == 405
    assert error.value.headers.get("Access-Control-Allow-Origin") is None


def test_actual_signed_bridge_two_turn_dialog_without_model_or_devices(http_app, monkeypatch, tmp_path):
    from codex_voice import app as runtime_module
    from codex_voice.voice_bridge import make_server
    monkeypatch.setattr(runtime_module, "LUMI_DATA_DIR", tmp_path / "never-created")
    runtime = runtime_module.CodexRuntime()
    runtime.authenticated = lambda: True
    prompts = []
    def model_stub(question, **kwargs):
        prompts.append(kwargs["_conversation_prompt"])
        return "Prüfantwort eins" if len(prompts) == 1 else "Prüfantwort zwei"
    runtime.query = model_stub
    private_server = make_server(runtime, KEY, host="127.0.0.1", port=0)
    worker = threading.Thread(target=private_server.serve_forever, daemon=True)
    worker.start()
    monkeypatch.setattr(module, "BRIDGE", f"http://127.0.0.1:{private_server.server_port}")
    module.SERVICE.client = BridgeClient(KEY)
    origin, _ = http_app
    try:
        with post(origin, "/api/lumi/session", {}) as response:
            cookie, bootstrap = response.headers["Set-Cookie"], json.load(response)
        for i, question in enumerate(("Unser Testwort ist Zeder", "Wie war das Testwort?")):
            with post(origin, "/api/lumi/turn", data(i, question), cookie, bootstrap["csrf"]) as response:
                assert response.status == 202
                turn = json.load(response)["turn_id"]
            for _ in range(100):
                with post(origin, "/api/lumi/result", {"turn_id": turn}, cookie, bootstrap["csrf"]) as response:
                    value = json.load(response)
                if value["state"] != "pending": break
                threading.Event().wait(.01)
            assert value == {"state": "done", "answer": "Prüfantwort " + ("eins" if i == 0 else "zwei")}
        assert "Unser Testwort ist Zeder" in prompts[1] and "Prüfantwort eins" in prompts[1]
        assert not (tmp_path / "never-created").exists()
        with post(origin, "/api/lumi/end", {}, cookie, bootstrap["csrf"]) as response:
            assert json.load(response) == {"ended": True, "pending": False}
            assert response.headers.get("Set-Cookie") is None
        with post(origin, "/api/lumi/result", {"turn_id": turn}, cookie, bootstrap["csrf"]) as response:
            assert response.status == 401
    finally:
        private_server.shutdown()
        private_server.server_close()
        worker.join(2)
