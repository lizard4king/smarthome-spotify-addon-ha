"""Anonymous browser runtime, without devices or external services."""
import json
from unittest.mock import Mock

import pytest

from codex_voice.app import CodexRuntime, load_browser_control_enabled
from codex_voice.lumi_assistant import LumiSettings


SESSION_A = "a" * 64
SESSION_B = "b" * 64


def browser_memory(session=SESSION_A, person=""):
    return {"person_key": person, "session_key": session}


def test_browser_ram_context_with_global_memory_disabled(monkeypatch, tmp_path):
    from codex_voice import app
    monkeypatch.setattr(app, "LUMI_DATA_DIR", tmp_path / "absent")
    runtime = CodexRuntime()
    runtime._lumi_settings = LumiSettings(memory_enabled=False)
    runtime._lumi_knowledge = Mock()
    runtime._lumi_notion_commands = Mock()
    prompts = []

    def query(_question, **kwargs):
        prompts.append(kwargs["_conversation_prompt"])
        return "Antwort eins" if len(prompts) == 1 else "Antwort zwei"

    runtime.query = query
    assert runtime.query_browser("Erste Frage", memory=browser_memory()) == "Antwort eins"
    assert runtime.query_browser("Zweite Frage", memory=browser_memory()) == "Antwort zwei"
    assert "Erste Frage" in prompts[1] and "Antwort eins" in prompts[1]
    assert "notion_sources\": []" in prompts[1]
    assert "Keine Geräteaktion ausführen" in prompts[1]
    assert not (tmp_path / "absent").exists()
    runtime._lumi_knowledge.search.assert_not_called()
    runtime._lumi_notion_commands.handle.assert_not_called()


def test_person_rejected_and_sessions_isolated_and_forget_only_own():
    runtime = CodexRuntime()
    runtime.query = Mock(side_effect=["A", "B", "A2", "B2"])
    with pytest.raises(ValueError):
        runtime.query_browser("Frage", memory=browser_memory(person="c" * 64))
    runtime.query_browser("Nur A", memory=browser_memory(SESSION_A))
    runtime.query_browser("Nur B", memory=browser_memory(SESSION_B))
    assert "Nur A" not in runtime.query.call_args.kwargs["_conversation_prompt"]
    assert runtime.query_browser("Vergiss unsere Gespräche", memory=browser_memory(SESSION_A)) == "Der Kontext dieser Sitzung wurde gelöscht."
    runtime.query_browser("A danach", memory=browser_memory(SESSION_A))
    assert "Nur A" not in runtime.query.call_args.kwargs["_conversation_prompt"]
    runtime.query_browser("B danach", memory=browser_memory(SESSION_B))
    assert "Nur B" in runtime.query.call_args.kwargs["_conversation_prompt"]


def test_control_disabled_never_touches_client_and_enabled_uses_current_planner():
    runtime = CodexRuntime()
    client = Mock()
    runtime._control_client = client
    runtime.query = Mock(return_value="Keine Aktion")
    assert runtime.query_browser("Alte Frage", memory=browser_memory()) == "Keine Aktion"
    client.context.assert_not_called()
    client.execute.assert_not_called()
    runtime._browser_control_enabled = True
    client.context.return_value = {"operations": ["none", "status"]}
    runtime.query = Mock(return_value=json.dumps({"answer": "Klärung", "action": {"operation": "clarify"}}))
    assert runtime.query_browser("Status vom Licht?", memory=browser_memory()) == "Klärung"
    client.context.assert_called_once()
    client.execute.assert_not_called()
    assert "Status vom Licht?" in runtime.query.call_args.kwargs["_control_prompt"]
    assert "Alte Frage" not in runtime.query.call_args.kwargs["_control_prompt"]
    client.context.return_value["devices"] = {"lampe": {"domain": "light", "writable": True}}
    client.context.return_value["operations"] = ["none", "status", "set_light"]
    client.execute.return_value = "Status bestätigt"
    runtime.query = Mock(return_value=json.dumps({"answer": "", "action": {
        "operation": "status", "alias": "lampe"}}))
    assert runtime.query_browser("Status der Lampe", memory=browser_memory()) == "Status bestätigt"
    client.execute.assert_called_once_with({"operation": "status", "alias": "lampe"})
    # The unchanged Alexa default still consults the same control client.
    client.reset_mock()
    runtime.query_voice("Status vom Licht?")
    client.context.assert_called_once()


def test_browser_forget_is_locally_classified_but_notion_is_not():
    runtime = CodexRuntime()
    assert runtime.handles_browser_locally("Vergiss unsere Gespräche")
    assert not runtime.handles_browser_locally("Notiz in Haus speichern mit Inhalt Test")


def test_option_reader_fails_closed(tmp_path):
    path = tmp_path / "options.json"
    assert load_browser_control_enabled(path) is False
    path.write_text('{"lumi_browser_control_enabled":true}', encoding="utf-8")
    assert load_browser_control_enabled(path) is True
    path.write_text('{"lumi_browser_control_enabled":"true"}', encoding="utf-8")
    assert load_browser_control_enabled(path) is False
    path.write_text('{"lumi_browser_control_enabled":true,"lumi_browser_control_enabled":false}', encoding="utf-8")
    assert load_browser_control_enabled(path) is False
