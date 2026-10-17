import json

import pytest

from codex_voice import app


def test_missing_and_existing_empty_settings_keep_cli_default(tmp_path):
    path = tmp_path / "options.json"
    assert app.load_voice_model_settings(path) == ("", "")
    path.write_text('{}', encoding="utf-8")
    assert app.load_voice_model_settings(path) == ("", "")


@pytest.mark.parametrize("settings", [
    {"voice_model": "--help"}, {"voice_model": "name\n--config"},
    {"voice_model": None}, {"voice_reasoning_effort": "unlimited"},
    {"voice_reasoning_effort": []},
])
def test_invalid_settings_fail_before_cli_launch(tmp_path, settings):
    path = tmp_path / "options.json"
    path.write_text(json.dumps(settings), encoding="utf-8")
    with pytest.raises(app.RuntimeErrorSafe) as error:
        app.load_voice_model_settings(path)
    assert error.value.diagnosis == "config-invalid"


def test_voice_command_selects_explicit_model_and_effort_only(monkeypatch):
    monkeypatch.setattr(app, "load_voice_model_settings", lambda: ("test-model", "low"))
    voice = app.make_query_command(voice=True, structured=True)
    assert voice[voice.index("--model") + 1] == "test-model"
    assert 'model_reasoning_effort="low"' in voice
    assert "--output-schema" in voice
    assert "read-only" in voice
    assert voice[-1] == "-"
    normal = app.make_query_command()
    assert "--model" not in normal
    assert not any("model_reasoning_effort" in value for value in normal)


def test_oversized_options_are_rejected(tmp_path):
    path = tmp_path / "options.json"
    path.write_bytes(b" " * (app.MAX_OPTIONS_BYTES + 1))
    with pytest.raises(app.RuntimeErrorSafe):
        app.load_voice_model_settings(path)


def test_explicit_valid_settings_are_loaded(tmp_path):
    path = tmp_path / "options.json"
    path.write_text(json.dumps({"voice_model": "test-model.v1", "voice_reasoning_effort": "low",
                                "authorized_user_ids": []}), encoding="utf-8")
    assert app.load_voice_model_settings(path) == ("test-model.v1", "low")


def test_regular_text_command_does_not_load_voice_settings(monkeypatch):
    def forbidden():
        raise AssertionError("Text requests must retain their existing model selection")
    monkeypatch.setattr(app, "load_voice_model_settings", forbidden)
    assert "--model" not in app.make_query_command()


def test_voice_command_uses_startup_settings_without_reopening_private_options(monkeypatch):
    monkeypatch.setattr(app, "VOICE_MODEL_SETTINGS", ("test-model", "low"))
    def forbidden():
        raise PermissionError("Runtime user cannot reopen root-owned options")
    monkeypatch.setattr(app, "load_voice_model_settings", forbidden)
    command = app.make_query_command(voice=True)
    assert command[command.index("--model") + 1] == "test-model"
    assert 'model_reasoning_effort="low"' in command
