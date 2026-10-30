import io
import json
import os
import stat
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import codex_voice.app as app_module

_REAL_TERMINATE_PROCESS = app_module._terminate_process

from codex_voice.app import (
    CODEX_HOME,
    CODEX_BIN,
    DISABLED_FEATURES,
    INGRESS_PEER,
    WORK_DIR,
    CodexRuntime,
    IngressHandler,
    RuntimeErrorSafe,
    build_safe_environment,
    make_query_command,
    parse_login_challenge,
    _read_bounded,
    _read_query_pipe,
)


class WritableBytes(io.BytesIO):
    def close(self):
        self.was_closed = True


class FakeProcess:
    next_process = None
    next_stderr = None

    def __init__(self, command, **kwargs):
        self.command = command
        self.kwargs = kwargs
        self.stdin = WritableBytes()
        self.stdout = io.BytesIO(self.next_process)
        self.stderr = io.BytesIO(self.next_stderr) if isinstance(self.next_stderr, bytes) else self.next_stderr
        self.returncode = None
        self.pid = 12345

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9


@pytest.fixture(autouse=True)
def fake_process_termination(monkeypatch):
    FakeProcess.next_stderr = None

    def terminate_fake_process(process):
        assert isinstance(process, FakeProcess)
        process.terminate()

    monkeypatch.setattr(app_module, "_terminate_process", terminate_fake_process)


def make_runtime(output, stderr=None):
    calls = []

    def factory(command, **kwargs):
        process = FakeProcess(command, **kwargs)
        calls.append((command, kwargs, process))
        return process

    FakeProcess.next_process = output
    FakeProcess.next_stderr = stderr
    return CodexRuntime(process_factory=factory), calls


def set_private_paths(monkeypatch, tmp_path):
    runtime_home = tmp_path / "runtime" / "codex"
    persistent_dir = tmp_path / "data" / "codex"
    monkeypatch.setattr(app_module, "CODEX_HOME", runtime_home)
    monkeypatch.setattr(app_module, "PERSISTENT_CODEX_DIR", persistent_dir)
    monkeypatch.setattr(app_module, "PERSISTENT_AUTH_FILE", persistent_dir / "auth.json")
    monkeypatch.setattr(app_module, "WORK_DIR", tmp_path / "runtime" / "work")
    monkeypatch.setattr(app_module, "HOME_DIR", tmp_path / "runtime" / "home")
    monkeypatch.setattr(app_module, "TMP_DIR", tmp_path / "runtime" / "tmp")
    monkeypatch.setattr(app_module, "LOG_DIR", tmp_path / "runtime" / "logs")
    return runtime_home, persistent_dir, persistent_dir / "auth.json"


def message_stream(answer):
    return (
        json.dumps(
            {
                "type": "item.completed",
                "item": {"id": "item_1", "type": "agent_message", "text": answer},
            }
        )
        + "\n"
        + json.dumps({"type": "turn.completed"})
        + "\n"
    ).encode()


def test_query_uses_stdin_and_only_literal_arguments():
    question = "What is a diffraction grating?"
    runtime, calls = make_runtime(message_stream("A periodic optical structure."))

    assert runtime.query(question) == "A periodic optical structure."

    command, options, process = calls[0]
    assert command[:4] == [CODEX_BIN, "--ask-for-approval", "never", "exec"]
    assert command[-1] == "-"
    assert question not in command
    assert "--ephemeral" in command
    assert "--json" in command
    assert "--ignore-user-config" in command
    assert "--ignore-rules" in command
    assert "--sandbox" in command and command[command.index("--sandbox") + 1] == "read-only"
    assert "--config" in command
    assert 'web_search="disabled"' in command
    disabled = {
        command[index + 1]
        for index, value in enumerate(command[:-1])
        if value == "--disable"
    }
    assert set(DISABLED_FEATURES) <= disabled
    assert "history.persistence=\"none\"" in command
    assert options["shell"] is False
    assert options["cwd"] == str(WORK_DIR)
    assert b"Question:\n" + question.encode() in process.stdin.getvalue()
    assert process.stdin.was_closed
    assert options["env"]["CODEX_HOME"] == str(CODEX_HOME)
    assert "SUPERVISOR_TOKEN" not in options["env"]
    assert "OPENAI_API_KEY" not in options["env"]
    assert set(options["env"]) == {
        "PATH",
        "HOME",
        "CODEX_HOME",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "TERM",
        "NO_COLOR",
    }
    assert options["start_new_session"] is True


@pytest.mark.skipif(os.name != "posix", reason="runtime symlink behavior requires POSIX")
def test_runtime_home_is_ephemeral_and_preserves_legacy_arg0_cache(monkeypatch, tmp_path):
    runtime_home, persistent_dir, auth_file = set_private_paths(monkeypatch, tmp_path)
    persistent_dir.mkdir(parents=True)
    auth_file.write_bytes(b"existing-auth-bytes")
    legacy_arg0 = persistent_dir / "tmp" / "arg0" / "codex-arg0legacy"
    legacy_arg0.mkdir(parents=True)
    legacy_link = legacy_arg0 / "apply_patch"
    legacy_target = tmp_path / "old-codex"
    legacy_target.write_text("existing target")
    if os.name == "posix":
        legacy_link.symlink_to(legacy_target)

    app_module.prepare_private_directories()
    app_module.prepare_private_directories()

    assert runtime_home == app_module.CODEX_HOME
    assert os.lstat(runtime_home / "auth.json")
    assert os.path.islink(runtime_home / "auth.json")
    assert os.readlink(runtime_home / "auth.json") == str(auth_file)
    assert auth_file.read_bytes() == b"existing-auth-bytes"
    assert legacy_arg0.is_dir()
    if os.name == "posix":
        assert legacy_link.is_symlink()
        assert legacy_link.readlink() == legacy_target


@pytest.mark.skipif(os.name != "posix", reason="symlink rejection requires POSIX")
def test_persistent_auth_file_must_not_be_symlink(monkeypatch, tmp_path):
    _, persistent_dir, auth_file = set_private_paths(monkeypatch, tmp_path)
    persistent_dir.mkdir(parents=True)
    outside = tmp_path / "outside-auth"
    outside.write_bytes(b"do not touch")
    auth_file.symlink_to(outside)

    with pytest.raises(RuntimeError, match="regular file"):
        app_module.prepare_private_directories()

    assert outside.read_bytes() == b"do not touch"


def test_startup_rejects_unexpected_regular_runtime_auth(monkeypatch, tmp_path):
    runtime_home, persistent_dir, auth_file = set_private_paths(monkeypatch, tmp_path)
    persistent_dir.mkdir(parents=True)
    auth_file.write_bytes(b"preserve-auth")
    runtime_home.mkdir(parents=True)
    runtime_auth = runtime_home / "auth.json"
    runtime_auth.write_bytes(b"untrusted leftover")

    with pytest.raises(RuntimeError, match="unexpected regular runtime auth file"):
        app_module.prepare_private_directories()

    assert auth_file.read_bytes() == b"preserve-auth"
    assert runtime_auth.read_bytes() == b"untrusted leftover"


@pytest.mark.skipif(os.name != "posix", reason="symlink rejection requires POSIX")
def test_runtime_auth_symlink_must_match_exact_persistent_target(monkeypatch, tmp_path):
    runtime_home, persistent_dir, auth_file = set_private_paths(monkeypatch, tmp_path)
    persistent_dir.mkdir(parents=True)
    runtime_home.mkdir(parents=True)
    outside = tmp_path / "outside-auth"
    outside.write_bytes(b"do not touch")
    (runtime_home / "auth.json").symlink_to(outside)

    with pytest.raises(RuntimeError, match="unexpected runtime auth symlink"):
        app_module.prepare_private_directories()

    assert not auth_file.exists()
    assert outside.read_bytes() == b"do not touch"


@pytest.mark.skipif(os.name != "posix", reason="runtime symlink behavior requires POSIX")
def test_cli_login_auth_write_is_promoted_and_runtime_link_restored(monkeypatch, tmp_path):
    runtime_home, persistent_dir, auth_file = set_private_paths(monkeypatch, tmp_path)
    persistent_dir.mkdir(parents=True)
    auth_file.write_bytes(b"old-auth")
    app_module.prepare_private_directories()
    runtime_auth = runtime_home / "auth.json"

    # `codex login` removes the auth.json symlink before starting device auth.
    runtime_auth.unlink()
    runtime_auth.write_bytes(b'{"tokens":{"access_token":"synthetic-test-token"}}')
    log_root = tmp_path / "runtime" / "logs"
    log_root.mkdir(parents=True, exist_ok=True)
    log_dir = log_root / "login"
    log_dir.mkdir()
    process = FakeProcess(b"")
    process.returncode = 0
    runtime = CodexRuntime()
    runtime._login_process = process
    runtime._login_log_dir = log_dir
    runtime._watch_login(process, log_dir)

    assert auth_file.read_bytes() == b'{"tokens":{"access_token":"synthetic-test-token"}}'
    assert stat.S_IMODE(os.lstat(auth_file).st_mode) == 0o600
    assert runtime_auth.is_symlink()
    assert runtime_auth.readlink() == auth_file

    # A new app process can safely repeat setup without changing the credentials.
    app_module.prepare_private_directories()
    assert auth_file.read_bytes() == b'{"tokens":{"access_token":"synthetic-test-token"}}'
    assert runtime_auth.is_symlink()


@pytest.mark.skipif(os.name != "posix", reason="runtime symlink behavior requires POSIX")
def test_first_login_without_existing_auth_promotes_and_survives_restart(
    monkeypatch, tmp_path
):
    runtime_home, persistent_dir, auth_file = set_private_paths(monkeypatch, tmp_path)
    app_module.prepare_private_directories()
    runtime_auth = runtime_home / "auth.json"
    assert not auth_file.exists()
    assert runtime_auth.is_symlink()

    runtime_auth.unlink()
    new_auth = b'{"tokens":{"access_token":"first-login-test-token"}}'
    runtime_auth.write_bytes(new_auth)
    log_dir = app_module.LOG_DIR / "login"
    log_dir.mkdir()
    process = FakeProcess(b"")
    process.returncode = 0
    runtime = CodexRuntime()
    runtime._login_process = process
    runtime._login_log_dir = log_dir

    runtime._watch_login(process, log_dir)

    assert auth_file.read_bytes() == new_auth
    assert runtime_auth.is_symlink()
    app_module.prepare_private_directories()
    assert auth_file.read_bytes() == new_auth


@pytest.mark.skipif(os.name != "posix", reason="runtime symlink write-through requires POSIX")
def test_auth_refresh_write_through_runtime_link_updates_persistent_bytes(
    monkeypatch, tmp_path
):
    runtime_home, persistent_dir, auth_file = set_private_paths(monkeypatch, tmp_path)
    persistent_dir.mkdir(parents=True)
    original_auth = b'{"tokens":{"access_token":"before-refresh"}}'
    refreshed_auth = b'{"tokens":{"access_token":"after-refresh"}}'
    auth_file.write_bytes(original_auth)
    app_module.prepare_private_directories()
    runtime_auth = runtime_home / "auth.json"
    assert runtime_auth.is_symlink()

    # Codex 0.160 file storage opens the configured auth path in-place.
    runtime_auth.write_bytes(refreshed_auth)

    assert runtime_auth.is_symlink()
    assert auth_file.read_bytes() == refreshed_auth


@pytest.mark.skipif(os.name != "posix", reason="runtime symlink behavior requires POSIX")
def test_cancel_after_cli_unlinks_runtime_auth_restores_existing_persistent_auth(
    monkeypatch, tmp_path
):
    runtime_home, persistent_dir, auth_file = set_private_paths(monkeypatch, tmp_path)
    persistent_dir.mkdir(parents=True)
    auth_file.write_bytes(b"preserve-old-auth")
    app_module.prepare_private_directories()
    runtime_auth = runtime_home / "auth.json"
    runtime_auth.unlink()
    runtime_auth.write_bytes(b"partial-login-auth")
    runtime = CodexRuntime()
    process = FakeProcess(b"")
    runtime._login_process = process
    runtime._login_state = "waiting"
    runtime.authenticated = lambda force=False: False

    runtime.cancel_login()

    assert auth_file.read_bytes() == b"preserve-old-auth"
    assert runtime_auth.is_symlink()
    assert runtime_auth.readlink() == auth_file
    assert runtime.login_status()["login_state"] == "idle"


@pytest.mark.skipif(os.name != "posix", reason="runtime symlink behavior requires POSIX")
def test_cancel_auth_promotion_error_returns_503_and_removes_login_logs(
    monkeypatch, tmp_path
):
    runtime_home, persistent_dir, auth_file = set_private_paths(monkeypatch, tmp_path)
    persistent_dir.mkdir(parents=True)
    old_auth = b"preserve-existing-auth"
    auth_file.write_bytes(old_auth)
    app_module.prepare_private_directories()
    runtime_auth = runtime_home / "auth.json"
    runtime_auth.unlink()
    runtime_auth.write_bytes(b"x" * (1024 * 1024 + 1))
    log_dir = app_module.LOG_DIR / "login"
    log_dir.mkdir()
    (log_dir / "diagnostic.log").write_text("private diagnostic")
    process = FakeProcess(b"")
    process.returncode = 0
    runtime = CodexRuntime()
    runtime._login_process = process
    runtime._login_log_dir = log_dir
    runtime._login_state = "waiting"
    allowed_user = "a" * 32
    handler = object.__new__(IngressHandler)
    handler.client_address = (INGRESS_PEER, 1234)
    handler.headers = {"X-Remote-User-Id": allowed_user, "Content-Length": "2"}
    handler.path = "/api/login/cancel"
    handler.rfile = io.BytesIO(b"{}")
    handler.server = SimpleNamespace(
        runtime=runtime, authorized_user_ids=frozenset({allowed_user})
    )
    responses = []
    handler._json = lambda status, data: responses.append((status, data))

    handler.do_POST()

    assert responses == [(503, {"error": "sign-in unavailable"})]
    assert not log_dir.exists()
    assert auth_file.read_bytes() == old_auth
    assert runtime_auth.is_symlink()
    assert runtime._login_state == "failed"


def test_login_log_directory_creation_failure_leaves_safe_failed_state(monkeypatch):
    runtime = CodexRuntime()
    runtime.authenticated = lambda force=False: False
    monkeypatch.setattr(
        runtime,
        "_new_log_dir",
        lambda: (_ for _ in ()).throw(OSError("synthetic filesystem error")),
    )

    with pytest.raises(RuntimeErrorSafe, match="login could not start"):
        runtime.start_login()

    assert runtime._login_state == "failed"
    assert runtime._login_process is None
    assert runtime._login_log_dir is None


@pytest.mark.skipif(os.name != "posix", reason="runtime symlink behavior requires POSIX")
def test_late_cancel_preserves_completed_login_auth(monkeypatch, tmp_path):
    runtime_home, persistent_dir, auth_file = set_private_paths(monkeypatch, tmp_path)
    persistent_dir.mkdir(parents=True)
    auth_file.write_bytes(b"old-auth")
    app_module.prepare_private_directories()
    runtime_auth = runtime_home / "auth.json"
    runtime_auth.unlink()
    new_auth = b'{"tokens":{"access_token":"late-cancel-test-token"}}'
    runtime_auth.write_bytes(new_auth)
    process = FakeProcess(b"")
    process.returncode = 0
    runtime = CodexRuntime()
    runtime._login_process = process
    runtime._login_state = "waiting"

    runtime.cancel_login()

    assert auth_file.read_bytes() == new_auth
    assert runtime_auth.is_symlink()
    assert runtime_auth.readlink() == auth_file


@pytest.mark.skipif(os.name != "posix", reason="runtime symlink behavior requires POSIX")
def test_shutdown_preserves_login_that_exited_successfully_before_watcher(
    monkeypatch, tmp_path
):
    runtime_home, persistent_dir, auth_file = set_private_paths(monkeypatch, tmp_path)
    persistent_dir.mkdir(parents=True)
    auth_file.write_bytes(b"old-auth")
    app_module.prepare_private_directories()
    runtime_auth = runtime_home / "auth.json"
    runtime_auth.unlink()
    completed_auth = b'{"tokens":{"access_token":"shutdown-test-token"}}'
    runtime_auth.write_bytes(completed_auth)
    process = FakeProcess(b"")
    process.returncode = 0
    runtime = CodexRuntime()
    runtime._login_process = process
    runtime._login_state = "waiting"

    runtime.shutdown()

    assert auth_file.read_bytes() == completed_auth
    assert runtime_auth.is_symlink()
    assert runtime_auth.readlink() == auth_file


@pytest.mark.skipif(os.name != "posix", reason="runtime symlink behavior requires POSIX")
def test_failed_login_discards_partial_auth_without_replacing_persistent_file(
    monkeypatch, tmp_path
):
    runtime_home, persistent_dir, auth_file = set_private_paths(monkeypatch, tmp_path)
    persistent_dir.mkdir(parents=True)
    auth_file.write_bytes(b"preserve-existing-auth")
    app_module.prepare_private_directories()
    runtime_auth = runtime_home / "auth.json"
    runtime_auth.unlink()
    runtime_auth.write_bytes(b'{"tokens":')
    log_dir = app_module.LOG_DIR / "login"
    log_dir.mkdir()
    process = FakeProcess(b"")
    process.returncode = 1
    runtime = CodexRuntime()
    runtime._login_process = process
    runtime._login_log_dir = log_dir

    runtime._watch_login(process, log_dir)

    assert auth_file.read_bytes() == b"preserve-existing-auth"
    assert runtime_auth.is_symlink()
    assert runtime_auth.readlink() == auth_file
    assert runtime._login_state == "failed"


@pytest.mark.skipif(os.name != "posix", reason="runtime symlink behavior requires POSIX")
def test_success_exit_rejects_non_json_auth_without_replacing_persistent_file(
    monkeypatch, tmp_path
):
    runtime_home, persistent_dir, auth_file = set_private_paths(monkeypatch, tmp_path)
    persistent_dir.mkdir(parents=True)
    auth_file.write_bytes(b"preserve-existing-auth")
    app_module.prepare_private_directories()
    runtime_auth = runtime_home / "auth.json"
    runtime_auth.unlink()
    runtime_auth.write_bytes(b"not-json")
    log_dir = app_module.LOG_DIR / "login"
    log_dir.mkdir()
    process = FakeProcess(b"")
    process.returncode = 0
    runtime = CodexRuntime()
    runtime._login_process = process
    runtime._login_log_dir = log_dir

    runtime._watch_login(process, log_dir)

    assert auth_file.read_bytes() == b"preserve-existing-auth"
    assert runtime_auth.is_symlink()
    assert runtime_auth.readlink() == auth_file
    assert runtime._login_state == "failed"


def test_query_removes_temporary_cli_log_directory(monkeypatch, tmp_path):
    log_root = tmp_path / "logs"
    monkeypatch.setattr(app_module, "LOG_DIR", log_root)
    runtime, _ = make_runtime(message_stream("Answer."))

    assert runtime.query("Question") == "Answer."
    assert list(log_root.iterdir()) == []


@pytest.mark.parametrize(
    "source,expected",
    [
        (b"unknown config field", "config-invalid"),
        (b"usage limit exceeded", "quota-limit"),
        (b"HTTP 403 unauthorized", "access-denied"),
        (b"model not found", "model-unavailable"),
        (b"bwrap: sandbox permission denied", "sandbox-unavailable"),
        (b"permission denied writing cache file", "runtime-storage"),
        (b"TLS connection timed out", "network-unavailable"),
        (b"HTTP 400 bad request", "request-rejected"),
    ],
)
def test_query_failure_classifier_returns_only_fixed_categories(source, expected):
    secret = b"synthetic-secret-4b6e"
    prompt = b"synthetic-prompt-0ad9"
    principal = b"synthetic-principal-82fa"
    diagnosis = app_module.classify_query_failure(
        source + b" " + secret + b" " + prompt + b" " + principal, b""
    )
    assert diagnosis == expected
    assert secret.decode() not in diagnosis
    assert prompt.decode() not in diagnosis
    assert principal.decode() not in diagnosis


def test_extract_answer_assigns_fixed_event_pipeline_categories():
    with pytest.raises(RuntimeErrorSafe) as error:
        CodexRuntime._extract_answer(b"not-json\n")
    assert error.value.diagnosis == "event-format"

    with pytest.raises(RuntimeErrorSafe) as error:
        CodexRuntime._extract_answer(
            (json.dumps({"type": "item.completed", "item": {"type": "command_execution"}}) + "\n").encode()
        )
    assert error.value.diagnosis == "tool-event-rejected"


def test_extract_answer_ignores_plan_metadata_and_returns_agent_message():
    events = [
        {
            "type": "item.started",
            "item": {"id": "plan_1", "type": "todo_list", "items": []},
        },
        {
            "type": "item.updated",
            "item": {
                "id": "plan_1",
                "type": "todo_list",
                "items": [{"text": "Think through the answer", "completed": True}],
            },
        },
        {
            "type": "item.completed",
            "item": {"id": "answer_1", "type": "agent_message", "text": "Scattering makes the sky blue."},
        },
        {"type": "turn.completed"},
    ]
    runtime, _ = make_runtime(("\n".join(map(json.dumps, events)) + "\n").encode())
    runtime.authenticated = lambda: True

    assert runtime.query("Why is the sky blue?") == "Scattering makes the sky blue."
    status = runtime.login_status()
    assert status["query_state"] == "completed"
    assert status["query_event_type"] == "todo_list"
    assert "Think through the answer" not in json.dumps(status)


def test_error_item_classifies_quota_without_exposing_payload_or_type_details():
    secret = "synthetic-secret-error-item-19d3"
    prompt = "synthetic-prompt-error-item-b748"
    events = [
        {
            "type": "item.completed",
            "item": {
                "id": "error_1",
                "type": "error",
                "message": f"usage limit exceeded {secret} {prompt}",
            },
        },
        {"type": "turn.completed"},
    ]
    runtime, _ = make_runtime(("\n".join(map(json.dumps, events)) + "\n").encode())
    runtime.authenticated = lambda: True

    with pytest.raises(RuntimeErrorSafe) as error:
        runtime.query(prompt)

    assert error.value.diagnosis == "quota-limit"
    assert error.value.event_type == "error"
    assert str(error.value) == "query failed"
    status = runtime.login_status()
    assert status["query_state"] == "failed"
    assert status["query_diagnosis"] == "quota-limit"
    assert status["query_event_type"] == "error"
    assert secret not in str(error.value)
    assert prompt not in str(error.value)
    assert secret not in json.dumps(status)
    assert prompt not in json.dumps(status)


def test_nonfatal_error_item_then_completed_answer_succeeds_with_fixed_hint():
    warning = "invalid global instructions synthetic-private-warning-2e81"
    events = [
        {
            "type": "item.completed",
            "item": {"id": "warning_1", "type": "error", "message": warning},
        },
        {
            "type": "item.completed",
            "item": {"id": "answer_1", "type": "agent_message", "text": "The sky appears blue."},
        },
        {"type": "turn.completed"},
    ]
    runtime, _ = make_runtime(("\n".join(map(json.dumps, events)) + "\n").encode())
    runtime.authenticated = lambda: True

    assert runtime.query("Why is the sky blue?") == "The sky appears blue."
    status = runtime.login_status()
    assert status["query_state"] == "completed"
    assert status["query_diagnosis"] == ""
    assert status["query_event_type"] == "error"
    assert warning not in json.dumps(status)


def test_nonfatal_error_item_without_answer_still_fails_with_safe_diagnosis():
    private = "synthetic-private-quota-detail-a77f"
    events = [
        {
            "type": "item.completed",
            "item": {
                "id": "warning_2",
                "type": "error",
                "message": f"usage limit exceeded {private}",
            },
        },
        {"type": "turn.completed"},
    ]
    runtime, _ = make_runtime(("\n".join(map(json.dumps, events)) + "\n").encode())
    runtime.authenticated = lambda: True

    with pytest.raises(RuntimeErrorSafe) as error:
        runtime.query("Question")

    assert error.value.diagnosis == "quota-limit"
    assert error.value.event_type == "error"
    assert str(error.value) == "query failed"
    status = runtime.login_status()
    assert status["query_state"] == "failed"
    assert status["query_diagnosis"] == "quota-limit"
    assert status["query_event_type"] == "error"
    assert private not in str(error.value)
    assert private not in json.dumps(status)


def test_warning_item_does_not_override_turn_failed_even_after_answer():
    events = [
        {
            "type": "item.completed",
            "item": {"id": "warning_3", "type": "error", "message": "nonfatal warning"},
        },
        {
            "type": "item.completed",
            "item": {"id": "answer_2", "type": "agent_message", "text": "Partial answer."},
        },
        {"type": "turn.failed", "error": {"message": "turn failed"}},
    ]
    runtime, _ = make_runtime(("\n".join(map(json.dumps, events)) + "\n").encode())
    runtime.authenticated = lambda: True

    with pytest.raises(RuntimeErrorSafe) as error:
        runtime.query("Question")

    assert error.value.diagnosis == "query-failed"
    assert str(error.value) == "query failed"
    status = runtime.login_status()
    assert status["query_state"] == "failed"
    assert status["query_diagnosis"] == "query-failed"


def test_warning_item_does_not_make_action_item_safe_to_ignore():
    events = [
        {
            "type": "item.completed",
            "item": {"id": "warning_4", "type": "error", "message": "nonfatal warning"},
        },
        {
            "type": "item.completed",
            "item": {"id": "command_1", "type": "command_execution", "command": "unsafe"},
        },
        {
            "type": "item.completed",
            "item": {"id": "answer_3", "type": "agent_message", "text": "Answer."},
        },
        {"type": "turn.completed"},
    ]
    runtime, _ = make_runtime(("\n".join(map(json.dumps, events)) + "\n").encode())
    runtime.authenticated = lambda: True

    with pytest.raises(RuntimeErrorSafe) as error:
        runtime.query("Question")

    assert error.value.diagnosis == "tool-event-rejected"
    assert error.value.event_type == "command_execution"
    status = runtime.login_status()
    assert status["query_state"] == "failed"
    assert status["query_diagnosis"] == "tool-event-rejected"
    assert status["query_event_type"] == "command_execution"


@pytest.mark.parametrize(
    "event_type",
    [
        "command_execution",
        "file_change",
        "mcp_tool_call",
        "collab_tool_call",
        "web_search",
    ],
)
def test_extract_answer_rejects_all_action_items(event_type):
    event = {
        "type": "item.completed",
        "item": {"id": "action_1", "type": event_type},
    }

    with pytest.raises(RuntimeErrorSafe) as error:
        CodexRuntime._extract_answer((json.dumps(event) + "\n").encode())

    assert error.value.diagnosis == "tool-event-rejected"
    assert error.value.event_type == event_type
    assert event_type not in {"agent_message", "reasoning"}


def test_unknown_item_type_is_rejected_and_reported_only_as_unknown():
    untrusted_type = "synthetic-secret-type-9b61"
    event = {
        "type": "item.completed",
        "item": {"id": "unknown_1", "type": untrusted_type, "payload": untrusted_type},
    }

    with pytest.raises(RuntimeErrorSafe) as error:
        CodexRuntime._extract_answer((json.dumps(event) + "\n").encode())

    assert error.value.diagnosis == "tool-event-rejected"
    assert error.value.event_type == "unknown"
    assert untrusted_type not in str(error.value)


def test_event_type_hint_skips_malformed_lines_without_exposing_them():
    secret = "synthetic-private-malformed-hint-7f31"
    output = (
        b"not-json " + secret.encode() + b"\n"
        + (json.dumps({"type": "item.updated", "item": {"type": "todo_list"}}) + "\n").encode()
    )

    assert CodexRuntime._event_type_hint(output) == "todo_list"


@pytest.mark.parametrize("untrusted_type", [{"private": "value"}, ["private", "value"]])
def test_nonstring_event_type_hint_is_sanitized_to_unknown(untrusted_type):
    event = {"type": "item.completed", "item": {"type": untrusted_type}}

    assert CodexRuntime._event_type_hint((json.dumps(event) + "\n").encode()) == "unknown"


def test_arbitrary_private_event_type_updates_status_only_as_unknown():
    private_type = "private-internal-marker-267b"
    event = {
        "type": "item.completed",
        "item": {"id": "unknown_2", "type": private_type, "payload": private_type},
    }
    runtime, _ = make_runtime((json.dumps(event) + "\n").encode())
    runtime.authenticated = lambda: True

    with pytest.raises(RuntimeErrorSafe) as error:
        runtime.query("A harmless question")

    assert error.value.event_type == "unknown"
    status = runtime.login_status()
    assert status["query_state"] == "failed"
    assert status["query_event_type"] == "unknown"
    assert private_type not in json.dumps(status)
    assert private_type not in str(error.value)


def test_query_drains_stderr_pipe_and_classifies_without_exposing_raw_error():
    secret = b"synthetic-secret-7ae3"
    prompt = "synthetic-prompt-1f09"
    principal = "synthetic-principal-4a21"
    output = json.dumps({"type": "error", "message": "usage limit " + secret.decode()}).encode() + b"\n"
    runtime, calls = make_runtime(output, stderr=b"prompt=" + prompt.encode() + b" id=" + principal.encode())

    with pytest.raises(RuntimeErrorSafe) as error:
        runtime.query(prompt)

    assert error.value.diagnosis == "quota-limit"
    assert str(error.value) == "query failed"
    assert secret.decode() not in str(error.value)
    assert calls[0][2].stderr is not None
    assert calls[0][2].stderr.closed
    assert calls[0][1]["stderr"] == app_module.subprocess.PIPE


def test_fake_process_with_no_stderr_remains_supported():
    runtime, calls = make_runtime(message_stream("Answer."))
    assert runtime.query("Question") == "Answer."
    assert calls[0][2].stderr is None


def test_nonzero_cli_exit_uses_allowlisted_diagnosis_not_raw_streams():
    secret = b"synthetic-secret-a493"
    principal = b"synthetic-user-id-1c20"
    FakeProcess.next_process = b"error id=" + principal
    FakeProcess.next_stderr = b"model not found " + secret

    class FailedProcess(FakeProcess):
        def wait(self, timeout=None):
            self.returncode = 1
            return self.returncode

    created = []

    def factory(command, **kwargs):
        process = FailedProcess(command, **kwargs)
        created.append(process)
        return process

    runtime = CodexRuntime(process_factory=factory)
    with pytest.raises(RuntimeErrorSafe) as error:
        runtime.query("synthetic-question-72b1")

    assert error.value.diagnosis == "model-unavailable"
    assert str(error.value) == "query failed"
    assert secret.decode() not in str(error.value)
    assert principal.decode() not in str(error.value)
    assert created[0].stderr.closed


@pytest.mark.parametrize("answer", [
    "This text discusses a rate limit and HTTP 403 as examples.",
    "The phrase invalid argument appears here as ordinary answer text.",
])
def test_normal_answer_text_never_drives_failure_classification(answer):
    class FailedProcess(FakeProcess):
        def wait(self, timeout=None):
            self.returncode = 1
            return self.returncode

    def factory(command, **kwargs):
        return FailedProcess(command, **kwargs)

    runtime, _ = make_runtime(message_stream(answer), stderr=b"generic process failure")
    runtime._popen = factory
    with pytest.raises(RuntimeErrorSafe) as error:
        runtime.query("Question")
    assert error.value.diagnosis == "query-failed"


def test_failure_event_fields_drive_category_but_not_other_event_text():
    output = b"\n".join([
        json.dumps({"type": "item.completed", "item": {
            "type": "agent_message", "text": "rate limit 403 invalid argument"
        }}).encode(),
        json.dumps({"type": "turn.failed", "error": {
            "message": "generic failure", "code": "model_not_found"
        }}).encode(),
    ])
    fields = app_module._failure_event_fields(output)
    assert b"rate limit" not in fields
    assert app_module.classify_query_failure(fields, b"") == "model-unavailable"


def test_stderr_overflow_terminates_process_and_is_bounded(monkeypatch):
    class WaitingProcess(FakeProcess):
        def __init__(self, command, **kwargs):
            super().__init__(command, **kwargs)
            self.finished = threading.Event()

        def wait(self, timeout=None):
            if not self.finished.wait(timeout):
                raise app_module.subprocess.TimeoutExpired(self.command, timeout)
            return self.returncode

        def terminate(self):
            self.returncode = -15
            self.finished.set()

    created = []

    def factory(command, **kwargs):
        FakeProcess.next_stderr = b"x" * 4096
        process = WaitingProcess(command, **kwargs)
        created.append(process)
        return process

    def terminate(process):
        process.terminate()

    monkeypatch.setattr(app_module, "MAX_QUERY_STDERR_BYTES", 1024)
    monkeypatch.setattr(app_module, "_terminate_process", terminate)
    FakeProcess.next_process = message_stream("unused")
    runtime = CodexRuntime(process_factory=factory)

    with pytest.raises(RuntimeErrorSafe, match="output limit") as error:
        runtime.query("Question")

    assert created[0].returncode == -15
    assert created[0].stderr.closed
    assert error.value.diagnosis == "event-format"


def test_ingress_returns_only_safe_diagnosis_category(monkeypatch):
    secret = "synthetic-secret-e6fa"
    prompt = "synthetic-prompt-808b"

    class Runtime:
        def authenticated(self):
            return True

        def query(self, _question):
            raise RuntimeErrorSafe(secret + prompt, diagnosis="quota-limit")

    allowed_user = "a" * 32
    handler = object.__new__(IngressHandler)
    handler.client_address = (INGRESS_PEER, 1234)
    body = json.dumps({"question": "What is 2 + 2?"}).encode()
    handler.headers = {"X-Remote-User-Id": allowed_user, "Content-Length": str(len(body))}
    handler.path = "/api/ask"
    handler.rfile = io.BytesIO(body)
    handler.server = SimpleNamespace(
        runtime=Runtime(), authorized_user_ids=frozenset({allowed_user})
    )
    responses = []
    handler._json = lambda status, data: responses.append((status, data))

    handler.do_POST()

    assert responses == [(502, {"error": "answer unavailable", "diagnosis": "quota-limit"})]
    serialized = json.dumps(responses)
    assert secret not in serialized and prompt not in serialized
    for diagnosis in app_module.QUERY_DIAGNOSES:
        assert f"'{diagnosis}':'" in app_module.APP_JS


def test_ask_auth_failure_has_fixed_diagnosis_and_http_status():
    class Runtime:
        def authenticated(self):
            return False

    allowed_user = "b" * 32
    body = json.dumps({"question": "Question"}).encode()
    handler = object.__new__(IngressHandler)
    handler.client_address = (INGRESS_PEER, 1234)
    handler.headers = {"X-Remote-User-Id": allowed_user, "Content-Length": str(len(body))}
    handler.path = "/api/ask"
    handler.rfile = io.BytesIO(body)
    handler.server = SimpleNamespace(
        runtime=Runtime(), authorized_user_ids=frozenset({allowed_user})
    )
    responses = []
    handler._json = lambda status, data: responses.append((status, data))
    handler.do_POST()
    assert responses == [(401, {"error": "sign-in required", "diagnosis": "access-denied"})]
    assert "error.status=Number.isInteger(response.status)" in app_module.APP_JS
    assert "if(!diagnosis && (!error.status || error.status>=500))" in app_module.APP_JS
    assert "status.query_state==='failed'" in app_module.APP_JS
    assert "HTTP ${error.status}" in app_module.APP_JS


def test_unexpected_query_exception_is_contained_without_raw_details():
    secret = "synthetic-secret-exception-92da"

    class Runtime:
        def authenticated(self):
            return True

        def query(self, _question):
            raise RuntimeError(secret)

    allowed_user = "c" * 32
    body = json.dumps({"question": "Question"}).encode()
    handler = object.__new__(IngressHandler)
    handler.client_address = (INGRESS_PEER, 1234)
    handler.headers = {"X-Remote-User-Id": allowed_user, "Content-Length": str(len(body))}
    handler.path = "/api/ask"
    handler.rfile = io.BytesIO(body)
    handler.server = SimpleNamespace(
        runtime=Runtime(), authorized_user_ids=frozenset({allowed_user})
    )
    responses = []
    handler._json = lambda status, data: responses.append((status, data))

    handler.do_POST()

    assert responses == [(502, {"error": "answer unavailable", "diagnosis": "query-failed"})]
    assert secret not in json.dumps(responses)


def test_query_log_creation_failure_releases_single_question_slot(monkeypatch):
    runtime, calls = make_runtime(message_stream("unused"))
    monkeypatch.setattr(
        runtime,
        "_new_log_dir",
        lambda: (_ for _ in ()).throw(OSError("synthetic filesystem error")),
    )

    with pytest.raises(RuntimeErrorSafe, match="query failed"):
        runtime.query("Question")

    assert calls == []
    assert runtime._query_slot.acquire(blocking=False)
    runtime._query_slot.release()


def test_login_start_returns_503_when_status_or_login_log_creation_fails(monkeypatch):
    runtime = CodexRuntime()
    monkeypatch.setattr(
        runtime,
        "_new_log_dir",
        lambda: (_ for _ in ()).throw(OSError("synthetic filesystem error")),
    )
    allowed_user = "a" * 32
    handler = object.__new__(IngressHandler)
    handler.client_address = (INGRESS_PEER, 1234)
    handler.headers = {"X-Remote-User-Id": allowed_user, "Content-Length": "2"}
    handler.path = "/api/login/start"
    handler.rfile = io.BytesIO(b"{}")
    handler.server = SimpleNamespace(
        runtime=runtime, authorized_user_ids=frozenset({allowed_user})
    )
    responses = []
    handler._json = lambda status, data: responses.append((status, data))

    handler.do_POST()

    assert responses == [(503, {"error": "sign-in unavailable"})]
    assert runtime._login_state == "failed"
    assert runtime._login_process is None
    assert runtime._login_log_dir is None


def test_query_rejects_any_tool_event():
    events = [
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Thinking."}},
        {
            "type": "item.started",
            "item": {"type": "command_execution", "command": "cat /etc/passwd"},
        },
        {"type": "turn.completed"},
    ]
    runtime, _ = make_runtime(("\n".join(map(json.dumps, events)) + "\n").encode())
    with pytest.raises(RuntimeErrorSafe, match="unexpected tool event"):
        runtime.query("Answer a general question")


def test_query_rejects_incomplete_jsonl_turn():
    runtime, _ = make_runtime(
        json.dumps(
            {"type": "item.completed", "item": {"type": "agent_message", "text": "No final"}}
        ).encode()
    )
    with pytest.raises(RuntimeErrorSafe, match="answer unavailable"):
        runtime.query("Question")


def test_query_discards_normal_reasoning_items():
    events = [
        {"type": "item.completed", "item": {"type": "reasoning", "text": "Internal thought."}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Answer."}},
        {"type": "turn.completed"},
    ]
    runtime, _ = make_runtime(("\n".join(map(json.dumps, events)) + "\n").encode())
    assert runtime.query("Question") == "Answer."


def test_query_status_tracks_success_and_failure_without_sensitive_data():
    secret = "synthetic-secret-query-status"
    runtime, _ = make_runtime(message_stream("Answer."))
    runtime.authenticated = lambda: True
    assert runtime.login_status()["query_state"] == "idle"
    assert runtime.query("synthetic prompt") == "Answer."
    status = runtime.login_status()
    assert status["query_state"] == "completed"
    assert status["query_diagnosis"] == ""
    assert secret not in json.dumps(status)

    runtime2, _ = make_runtime(b"not-json\n")
    runtime2.authenticated = lambda: True
    with pytest.raises(RuntimeErrorSafe):
        runtime2.query(secret)
    status = runtime2.login_status()
    assert status["query_state"] == "failed"
    assert status["query_diagnosis"] == "event-format"
    assert secret not in json.dumps(status)


def test_busy_query_does_not_overwrite_active_query_diagnostic():
    runtime, _ = make_runtime(message_stream("unused"))
    runtime._query_slot.acquire()
    try:
        with runtime._query_state_lock:
            runtime._query_state = "running"
            runtime._query_diagnosis = ""
        with pytest.raises(RuntimeErrorSafe, match="busy"):
            runtime.query("second question")
        assert runtime.login_status()["query_state"] == "running"
        assert runtime.login_status()["query_diagnosis"] == ""
    finally:
        runtime._query_slot.release()


def test_bounded_reader_terminates_once_on_first_overflow():
    terminated = []
    output, too_large = _read_bounded(
        io.BytesIO(b"0123456789"), 5, on_overflow=lambda: terminated.append(True)
    )
    assert output == b""
    assert too_large
    assert terminated == [True]


@pytest.mark.skipif(os.name != "posix", reason="POSIX pipe readiness behavior")
def test_query_pipe_reader_stops_with_inherited_writer_still_open():
    read_fd, write_fd = os.pipe()
    stream = os.fdopen(read_fd, "rb", buffering=0)
    stop = threading.Event()
    overflow = threading.Event()
    result = []
    reader = threading.Thread(
        target=lambda: result.append(_read_query_pipe(stream, 1024, overflow, None, stop)),
        daemon=True,
    )
    reader.start()
    try:
        time.sleep(0.15)
        started = time.monotonic()
        stop.set()
        reader.join(timeout=0.5)
        assert not reader.is_alive()
        assert time.monotonic() - started < 0.5
        assert result == [(b"", False)]
        assert stream.closed
    finally:
        os.close(write_fd)


def test_query_kills_process_promptly_when_stdout_exceeds_limit(monkeypatch):
    class WaitingProcess(FakeProcess):
        def __init__(self, command, **kwargs):
            super().__init__(command, **kwargs)
            self.finished = threading.Event()

        def wait(self, timeout=None):
            if not self.finished.wait(timeout):
                raise app_module.subprocess.TimeoutExpired(self.command, timeout)
            return self.returncode

    created = []

    def factory(command, **kwargs):
        process = WaitingProcess(command, **kwargs)
        created.append(process)
        return process

    def terminate(process):
        process.returncode = -15
        process.finished.set()

    monkeypatch.setattr(app_module, "MAX_QUERY_STDOUT_BYTES", 1024)
    monkeypatch.setattr(app_module, "QUERY_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(app_module, "_terminate_process", terminate)
    FakeProcess.next_process = b"x" * 2048
    runtime = CodexRuntime(process_factory=factory)
    started = time.monotonic()

    with pytest.raises(RuntimeErrorSafe, match="output limit"):
        runtime.query("Question")

    assert created[0].returncode == -15
    assert time.monotonic() - started < 0.8


def test_login_challenge_parser_returns_only_code_and_official_url():
    code, url = parse_login_challenge(
        b"Welcome to Codex [v0.160.0]\n"
        b"OpenAI's command-line coding agent\n"
        b"Follow these steps to sign in with ChatGPT using device code authorization:\n"
        b"1. Open this link in your browser and sign in to your account\n"
        b"   https://auth.openai.com/codex/device\x1b[0m\n\n"
        b"2. Enter this one-time code (expires in 15 minutes)\x1b[0m\n"
        b"   ABCD-EF12\x1b[0m\n"
        b"Continue only if you started this login in Codex. If a website or another person gave you this code, cancel."
    )
    assert code == "ABCD-EF12"
    assert url == "https://auth.openai.com/codex/device"

    code, url = parse_login_challenge(
        "https://evil.example/codex/device\n"
        "2. Enter this one-time code (expires in 15 minutes)\n"
        "   ABCD-EF12"
    )
    assert code == "ABCD-EF12"
    assert url is None


def test_login_challenge_does_not_accept_non_https_or_non_openai_url():
    prompt = "2. Enter this one-time code (expires in 15 minutes)\n   123456"
    assert parse_login_challenge("http://auth.openai.com/codex/device\n" + prompt) == (
        "123456",
        None,
    )
    assert parse_login_challenge("https://auth.openai.com.attacker.example/path\n" + prompt) == (
        "123456",
        None,
    )


def test_login_challenge_accepts_opaque_code_shapes_after_exact_label():
    label = "2. Enter this one-time code (expires in 15 minutes)\n   "
    assert parse_login_challenge(label + "123456")[0] == "123456"
    assert parse_login_challenge(label + "ABCDE-FGHIJ")[0] == "ABCDE-FGHIJ"


@pytest.mark.parametrize(
    "line",
    [
        "   Error: expired",
        "   ABCD EF12",
        "   abcdef",
        "   " + "A" * 33,
        "   ---",
    ],
)
def test_login_challenge_rejects_invalid_code_line_after_label(line):
    text = "2. Enter this one-time code (expires in 15 minutes)\n" + line
    assert parse_login_challenge(text)[0] is None


def test_login_challenge_requires_exact_label_and_immediately_next_line():
    assert parse_login_challenge(
        "2. Enter this one-time code (expires in 15 minutes)\n"
        "   Error: retry later\n   ABCD-EF12"
    )[0] is None
    assert parse_login_challenge(
        "2. Enter this one-time code (expires in 30 minutes)\n   ABCD-EF12"
    )[0] is None
    assert parse_login_challenge("ABCD-EF12\n   unrelated message")[0] is None


@pytest.mark.parametrize(
    "url",
    [
        "https://auth.openai.com/codex/device?next=evil",
        "https://auth.openai.com/codex/device#fragment",
        "https://user@auth.openai.com/codex/device",
        "https://auth.openai.com.evil.example/codex/device",
        "https://auth.openai.com/codex/device/extra",
    ],
)
def test_login_challenge_requires_exact_verification_url(url):
    text = url + "\n2. Enter this one-time code (expires in 15 minutes)\n   123456"
    assert parse_login_challenge(text) == ("123456", None)


def test_ingress_requires_supervisor_peer_and_authenticated_user_header():
    handler = object.__new__(IngressHandler)
    handler.client_address = (INGRESS_PEER, 1234)
    allowed_user = "a" * 32
    handler.server = SimpleNamespace(authorized_user_ids=frozenset({allowed_user}))
    handler.headers = {"X-Remote-User-Id": allowed_user}
    assert handler._authorized()

    handler.client_address = ("172.30.32.3", 1234)
    assert not handler._authorized()

    handler.client_address = (INGRESS_PEER, 1234)
    handler.headers = {"X-Remote-User-Id": "b" * 32}
    assert not handler._authorized()
    handler.server = SimpleNamespace(authorized_user_ids=frozenset())
    handler.headers = {"X-Remote-User-Id": allowed_user}
    assert not handler._authorized()


def test_authorized_user_allowlist_validation_fails_closed():
    assert app_module.parse_authorized_user_ids(
        json.dumps({"authorized_user_ids": ["A" * 32]}).encode()
    ) == frozenset({"a" * 32})
    assert app_module.parse_authorized_user_ids(b'{"authorized_user_ids":[]}') == frozenset()
    assert app_module.parse_authorized_user_ids(
        b'{"authorized_user_ids":["valid-but-not-32"]}'
    ) == frozenset()
    assert app_module.parse_authorized_user_ids(b"not-json") == frozenset()


def test_authorized_user_allowlist_loader_and_empty_default(tmp_path):
    options_file = tmp_path / "options.json"
    options_file.write_text(json.dumps({"authorized_user_ids": ["c" * 32]}))
    assert app_module.load_authorized_user_ids(options_file) == frozenset({"c" * 32})

    options_file.write_text(json.dumps({"authorized_user_ids": []}))
    assert app_module.load_authorized_user_ids(options_file) == frozenset()


@pytest.mark.skipif(os.name != "posix", reason="killpg is POSIX-only")
def test_terminate_skips_already_reaped_process_group(monkeypatch):
    signals = []

    class ReapedProcess:
        pid = 12345
        returncode = 0
        _waitpid_lock = threading.Lock()

        def poll(self):
            return self.returncode

    process = ReapedProcess()
    monkeypatch.setattr(app_module, "_terminate_process", _REAL_TERMINATE_PROCESS)
    monkeypatch.setattr(app_module.os, "killpg", lambda *args: signals.append(args))

    app_module._terminate_process(process)

    assert signals == []


@pytest.mark.skipif(os.name != "posix", reason="killpg is POSIX-only")
def test_terminate_signals_active_process_group(monkeypatch):
    signals = []

    class ActiveProcess:
        pid = 12345
        returncode = None
        _waitpid_lock = threading.Lock()

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.returncode = -15
            return self.returncode

    process = ActiveProcess()
    monkeypatch.setattr(app_module, "_terminate_process", _REAL_TERMINATE_PROCESS)
    monkeypatch.setattr(app_module.os, "killpg", lambda *args: signals.append(args))

    app_module._terminate_process(process)

    assert signals == [(12345, app_module.signal.SIGTERM)]
    assert process.returncode == -15


def test_old_login_watcher_only_removes_its_own_log_directory(monkeypatch, tmp_path):
    log_root = tmp_path / "logs"
    log_root.mkdir()
    monkeypatch.setattr(app_module, "LOG_DIR", log_root)
    old_log_dir = log_root / "old"
    new_log_dir = log_root / "new"
    old_log_dir.mkdir()
    new_log_dir.mkdir()
    (old_log_dir / "diagnostic.log").write_text("old")
    (new_log_dir / "diagnostic.log").write_text("new")

    old_process = FakeProcess(b"", stdout=io.BytesIO(b""))
    old_process.returncode = 0
    new_process = FakeProcess(b"", stdout=io.BytesIO(b""))
    runtime = CodexRuntime()
    runtime._login_process = new_process
    runtime._login_log_dir = new_log_dir
    runtime._login_state = "waiting"
    secured = []
    monkeypatch.setattr(runtime, "_secure_auth_file", lambda: secured.append(True))

    runtime._watch_login(old_process, old_log_dir)

    assert not old_log_dir.exists()
    assert (new_log_dir / "diagnostic.log").read_text() == "new"
    assert runtime._login_process is new_process
    assert runtime._login_log_dir == new_log_dir
    assert runtime._login_state == "waiting"
    assert secured == []


@pytest.mark.skipif(os.name != "posix", reason="runtime symlink behavior requires POSIX")
def test_stale_login_watcher_cannot_persist_after_next_login_starts(monkeypatch, tmp_path):
    runtime_home, persistent_dir, auth_file = set_private_paths(monkeypatch, tmp_path)
    persistent_dir.mkdir(parents=True)
    auth_file.write_bytes(b"existing-auth")
    app_module.prepare_private_directories()
    runtime_auth = runtime_home / "auth.json"
    runtime_auth.unlink()
    runtime_auth.write_bytes(b"stale-partial-auth")

    log_root = app_module.LOG_DIR
    old_log_dir = log_root / "old"
    new_log_dir = log_root / "new"
    old_log_dir.mkdir()
    new_log_dir.mkdir()
    old_process = FakeProcess(b"")
    old_process.returncode = 0
    new_process = FakeProcess(b"")
    runtime = CodexRuntime()
    runtime._login_process = new_process
    runtime._login_log_dir = new_log_dir
    runtime._login_state = "waiting"
    secured = []
    monkeypatch.setattr(runtime, "_secure_auth_file", lambda: secured.append(True))

    runtime._watch_login(old_process, old_log_dir)

    assert secured == []
    assert auth_file.read_bytes() == b"existing-auth"
    assert runtime_auth.read_bytes() == b"stale-partial-auth"
    assert runtime._login_process is new_process
    assert runtime._login_log_dir == new_log_dir
    assert runtime._login_state == "waiting"


@pytest.mark.skipif(os.name != "posix", reason="runtime symlink behavior requires POSIX")
def test_cancel_restores_link_before_a_new_login_starts(monkeypatch, tmp_path):
    runtime_home, persistent_dir, auth_file = set_private_paths(monkeypatch, tmp_path)
    persistent_dir.mkdir(parents=True)
    auth_file.write_bytes(b"keep-auth")
    app_module.prepare_private_directories()
    runtime_auth = runtime_home / "auth.json"
    runtime_auth.unlink()
    runtime = CodexRuntime()
    old_process = FakeProcess(b"")
    runtime._login_process = old_process
    runtime._login_state = "waiting"
    runtime.cancel_login()

    new_process = FakeProcess(b"")
    runtime.authenticated = lambda force=False: False
    monkeypatch.setattr(runtime, "_spawn", lambda *args, **kwargs: new_process)

    class UnstartedThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(app_module.threading, "Thread", UnstartedThread)
    runtime.start_login()

    assert auth_file.read_bytes() == b"keep-auth"
    assert runtime_auth.is_symlink()
    assert runtime_auth.readlink() == auth_file
    assert runtime._login_process is new_process
    assert runtime._login_state == "waiting"


@pytest.mark.skipif(os.name != "posix", reason="privilege dropping is POSIX-only")
def test_drop_privileges_is_ordered_and_never_walks_persistent_cache(monkeypatch, tmp_path):
    from types import SimpleNamespace
    import pwd

    runtime_home, persistent_dir, _auth_file = set_private_paths(monkeypatch, tmp_path)
    app_module.prepare_private_directories()
    legacy_dir = persistent_dir / "tmp" / "arg0" / "codex-arg0legacy"
    legacy_dir.mkdir(parents=True)
    legacy_link = legacy_dir / "apply_patch"
    legacy_target = tmp_path / "codex"
    legacy_target.write_text("test executable")
    legacy_link.symlink_to(legacy_target)

    account = SimpleNamespace(pw_uid=10001, pw_gid=10001)
    events = []
    changed_paths = []
    monkeypatch.setattr(app_module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(pwd, "getpwnam", lambda _name: account)

    def record_chown(path, uid, gid):
        changed_paths.append((Path(path), uid, gid))

    def forbid_walk(*_args, **_kwargs):
        raise AssertionError("drop_privileges must not traverse persistent cache")

    monkeypatch.setattr(app_module.os, "chown", record_chown)
    monkeypatch.setattr(app_module.os, "walk", forbid_walk)
    monkeypatch.setattr(app_module.os, "setgroups", lambda groups: events.append(("groups", groups)))
    monkeypatch.setattr(app_module.os, "setgid", lambda gid: events.append(("gid", gid)))
    monkeypatch.setattr(app_module.os, "setuid", lambda uid: events.append(("uid", uid)))

    app_module.drop_privileges()

    assert events == [("groups", []), ("gid", 10001), ("uid", 10001)]
    assert all(uid == 10001 and gid == 10001 for _, uid, gid in changed_paths)
    assert not any(path == legacy_link or path.is_relative_to(legacy_dir) for path, _, _ in changed_paths)
    assert legacy_link.is_symlink()


def test_login_status_clears_stale_authenticated_state():
    runtime = CodexRuntime()
    runtime._auth_cache = (float("inf"), False)
    runtime._login_state = "authenticated"
    status = runtime.login_status()
    assert status["authenticated"] is False
    assert status["login_state"] == "idle"


def test_oversized_request_body_forces_connection_close():
    handler = object.__new__(IngressHandler)
    handler.headers = {"Content-Length": "4097"}
    handler.close_connection = False
    with pytest.raises(ValueError, match="body size"):
        handler._read_json()
    assert handler.close_connection is True


def test_query_command_contains_no_user_supplied_paths_or_prompt():
    command = make_query_command()
    assert command[:4] == [CODEX_BIN, "--ask-for-approval", "never", "exec"]
    assert "--ask-for-approval" not in command[4:]
    assert command[-1] == "-"
    assert str(WORK_DIR) in command
    assert all("SUPERVISOR" not in arg and "OPENAI_API_KEY" not in arg for arg in command)
