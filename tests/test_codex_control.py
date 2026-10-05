import hashlib
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "smarthome_cockpit"))
from dashboard.codex_control import ControlBroker, mac

SECRET = "a" * 32
CONFIG = SimpleNamespace(entities={"lamp": "light.lamp", "heat": "climate.heat", "camera": "camera.private"}, write_allowlist={"lamp", "heat", "camera"}, allow_writes=True, blocked_domains=set())

def signed(payload, nonce="b" * 32, path="/api/codex/control", **overrides):
    raw = json.dumps(payload, separators=(",", ":")).encode()
    headers = {"Content-Type": "application/json", "X-Control-Timestamp": "1000", "X-Control-Nonce": nonce}
    headers["X-Control-Signature"] = mac(SECRET, "POST", path, "1000", nonce, hashlib.sha256(raw).hexdigest())
    headers.update(overrides)
    return headers, raw

def broker():
    calls = []
    adapter = SimpleNamespace(set_light=lambda *args: calls.append(args), set_temperature=lambda *args: calls.append(args))
    return ControlBroker(SECRET, lambda: CONFIG, lambda _: adapter, lambda: 1000, lambda: 1000), calls

def payload(action):
    return {"request_id": "c" * 32, "action": action}

@pytest.mark.parametrize("action", [
    {"operation": "set_light", "alias": "lamp", "is_on": 1},
    {"operation": "set_temperature", "alias": "heat", "temperature": True},
    {"operation": "set_temperature", "alias": "heat", "temperature": 31},
    {"operation": "set_light", "alias": "camera", "is_on": True},
    {"operation": "set_light", "alias": "foreign", "is_on": True},
    {"operation": "set_light", "alias": "lamp", "is_on": True, "shell": "rm"},
    {"operation": "music_play", "uri": "file:///private"},
    {"operation": "shell", "command": "whoami"},
])
def test_invalid_actions_have_no_effect(action):
    instance, calls = broker()
    headers, raw = signed(payload(action))
    assert instance.process("/api/codex/control", headers, raw)[0] == 403
    assert calls == []

@pytest.mark.parametrize("override", [{"X-Control-Signature": "x"}, {"X-Control-Timestamp": "999" * 4}, {"Content-Encoding": "gzip"}, {"Content-Type": "text/plain"}, {"Origin": "http://evil"}, {"Transfer-Encoding": "chunked"}])
def test_auth_rejection(override):
    instance, calls = broker()
    headers, raw = signed(payload({"operation": "set_light", "alias": "lamp", "is_on": True}), **override)
    assert instance.process("/api/codex/control", headers, raw)[0] == 403
    assert not calls

def test_dedupe_replay_changed_and_response_signature():
    instance, calls = broker()
    value = payload({"operation": "set_light", "alias": "lamp", "is_on": True})
    headers, raw = signed(value)
    status, body, signature = instance.process("/api/codex/control", headers, raw)
    assert status == 200
    assert signature == mac(SECRET, "RESPONSE", "/api/codex/control", "b" * 32, "200", hashlib.sha256(body).hexdigest())
    assert instance.process("/api/codex/control", headers, raw)[0] == 403
    headers, raw = signed(value, "d" * 32)
    assert instance.process("/api/codex/control", headers, raw)[0] == 200
    value["action"]["is_on"] = False
    headers, raw = signed(value, "e" * 32)
    assert instance.process("/api/codex/control", headers, raw)[0] == 403
    assert calls == [("lamp", True)]

def test_concurrent_same_operation_executes_once():
    instance, calls = broker()
    value = payload({"operation": "set_light", "alias": "lamp", "is_on": True})
    def run(index):
        headers, raw = signed(value, f"{index:032x}")
        return instance.process("/api/codex/control", headers, raw)[0]
    with ThreadPoolExecutor(8) as pool:
        assert list(pool.map(run, range(8))) == [200] * 8
    assert len(calls) == 1

def test_ambiguous_failure_is_cached_and_capacity_keeps_old_operations():
    instance, _ = broker()
    attempts = []
    def fail(*args):
        attempts.append(1)
        raise TimeoutError()
    instance.execute = fail
    value = payload({"operation": "set_light", "alias": "lamp", "is_on": True})
    for nonce in ("d" * 32, "e" * 32):
        headers, raw = signed(value, nonce)
        assert instance.process("/api/codex/control", headers, raw)[0] == 503
    assert len(attempts) == 1
    instance.operations.update({f"{i:032x}": (1600, "digest", 503, {}) for i in range(31)})
    value["request_id"] = "f" * 32
    headers, raw = signed(value, "f" * 32)
    assert instance.process("/api/codex/control", headers, raw)[0] == 403
    assert len(attempts) == 1

def test_context_excludes_camera_and_absent_explicit_allowlist():
    instance, _ = broker()
    assert set(instance.context()["devices"]) == {"lamp", "heat"}
    instance.config_factory = lambda: SimpleNamespace(**{**vars(CONFIG), "write_allowlist": None})
    assert not instance.context()["devices"]


def test_spotify_requires_explicit_profile_grant(monkeypatch):
    monkeypatch.setenv("SPOTIFY_BRIDGE_URL", "http://example.invalid:8766")
    monkeypatch.setenv("SPOTIFY_BRIDGE_SECRET", SECRET)
    monkeypatch.delenv("CODEX_SPOTIFY_PROFILES", raising=False)
    instance, _ = broker()
    assert instance.context()["spotify"]["enabled"] is False
    monkeypatch.setenv("CODEX_SPOTIFY_PROFILES", "")
    assert instance.context()["spotify"]["enabled"] is False
    manifest = (Path(__file__).parents[1] / "smarthome_cockpit/config.yaml").read_text()
    assert "  codex_spotify_profiles: []\n" in manifest
    monkeypatch.setenv("CODEX_SPOTIFY_PROFILES", "Andreas,Erlene")
    assert instance.context()["spotify"]["enabled"] is True

@pytest.mark.parametrize("path", ["/api/codex/control?x=1", "/api/codex/%63ontrol", "/api/codex/control/"])
def test_nonexact_path(path):
    instance, calls = broker()
    headers, raw = signed(payload({"operation": "none"}), path=path)
    assert instance.process(path, headers, raw)[0] == 403
    assert not calls

def test_clock_changes_do_not_expire_operation_cache():
    instance, calls = broker()
    value = payload({"operation": "set_light", "alias": "lamp", "is_on": True})
    headers, raw = signed(value)
    assert instance.process("/api/codex/control", headers, raw)[0] == 200
    instance.clock = lambda: 2000
    headers, raw = signed(value, "e" * 32)
    headers["X-Control-Timestamp"] = "2000"
    headers["X-Control-Signature"] = mac(SECRET, "POST", "/api/codex/control", "2000", "e" * 32, hashlib.sha256(raw).hexdigest())
    assert instance.process("/api/codex/control", headers, raw)[0] == 200
    assert len(calls) == 1

@pytest.mark.parametrize("status", ["failed", "unknown_profile", "needs_profile", "target_not_allowed"])
def test_spotify_200_failure_is_not_confirmation(monkeypatch, status):
    import io
    import dashboard.codex_control as module
    monkeypatch.setenv("SPOTIFY_BRIDGE_URL", "http://example.invalid:8766")
    monkeypatch.setenv("SPOTIFY_BRIDGE_SECRET", SECRET)
    monkeypatch.setenv("CODEX_SPOTIFY_PROFILES", "Andreas")
    class Response(io.BytesIO):
        status = 200
        headers = {"Content-Type": "application/json"}
        def geturl(self):
            return "http://example.invalid:8766/api/spotify/command"
    monkeypatch.setattr(module, "build_opener", lambda *_: SimpleNamespace(open=lambda *args, **kwargs: Response(json.dumps({"status": status}).encode())))
    instance, calls = broker()
    value = payload({"operation": "spotify_play", "profile": "Andreas", "target": "wohnzimmer", "query": "Test title"})
    headers, raw = signed(value)
    response_status, body, _ = instance.process("/api/codex/control", headers, raw)
    assert response_status == 503 and "receipt" not in json.loads(body)
    assert not calls

def test_disabled_broker_and_duplicate_json_reject_before_effect():
    instance, calls = broker()
    value = payload({"operation": "set_light", "alias": "lamp", "is_on": True})
    headers, raw = signed(value)
    instance.secret = ""
    assert instance.process("/api/codex/control", headers, raw)[0] == 403
    instance.secret = SECRET
    raw = raw.replace(b'"is_on":true', b'"is_on":false,"is_on":true')
    headers["X-Control-Signature"] = mac(SECRET, "POST", "/api/codex/control", "1000", "b" * 32, hashlib.sha256(raw).hexdigest())
    assert instance.process("/api/codex/control", headers, raw)[0] == 403
    assert not calls

def test_http_routing_has_no_cors_and_rejects_encoded_query_and_get(monkeypatch):
    import threading
    from http.server import ThreadingHTTPServer
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError
    import dashboard.codex_control as module
    from dashboard.server import CockpitHandler
    instance, calls = broker()
    monkeypatch.setattr(module, "BROKER", instance)
    with ThreadingHTTPServer(("127.0.0.1", 0), CockpitHandler) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            for path, method in (("/api/codex/control", "GET"), ("/api/codex/unknown", "POST"), ("/api/codex/%63ontrol", "POST"), ("/api/codex/control?x=1", "POST"), ("/api/codex/control", "OPTIONS")):
                headers, raw = signed(payload({"operation": "none"}), path=path)
                request = Request(base + path, raw if method == "POST" else None, headers=headers, method=method)
                with pytest.raises(HTTPError) as exc:
                    urlopen(request, timeout=2)
                assert exc.value.code in {403, 404, 405}
                assert exc.value.headers.get("Access-Control-Allow-Origin") is None
                exc.value.close()
        finally:
            server.shutdown()
            worker.join(2)
    assert not calls
