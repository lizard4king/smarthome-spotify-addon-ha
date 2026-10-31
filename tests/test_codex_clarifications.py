"""Local-only questions for incomplete current requests; no model or device calls."""
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from codex_voice.app import CodexRuntime, RuntimeErrorSafe
from codex_voice.control_client import ControlClient, validate_action, validate_plan
from codex_voice.lumi_assistant import LumiSettings
from codex_voice.lumi_context import ContextStore


OWNER = "a" * 64
SESSION = "b" * 64
ROOMS = ["kueche", "wohnzimmer", "badezimmer", "buero", "schlafzimmer"]


def context():
    return {
        "version": 1,
        "devices": {},
        "spotify": {"enabled": True, "profiles": ["andreas"], "targets": ROOMS},
        "music": {"enabled": True, "targets": ["buero"]},
        "operations": ["none", "status", "set_light", "set_temperature",
                       "spotify_play", "music_play", "music_control"],
    }


def clarification(answer="In welchem Raum soll ich den Titel abspielen?"):
    return {"answer": answer, "action": {"operation": "clarify"}}


@pytest.mark.parametrize("question,answer", [
    ("Spiele Ein echtes Wir aus eigener Musik ab.", "In welchem Raum soll ich den Titel abspielen?"),
    ("Spiele eigene Musik im Büro ab.", "Welchen Titel soll ich im Büro abspielen?"),
    ("Spiele Test auf Spotify im Büro ab.", "Welches Spotify-Profil soll ich verwenden?"),
])
def test_current_request_clarification_is_not_replaced_by_conversation(question, answer):
    runtime = CodexRuntime()
    runtime._control_client = Mock()
    runtime._control_client.context.return_value = context()
    runtime.query = Mock(side_effect=[
        json.dumps(clarification(answer)), "Ich kann hier keine Musik abspielen."])

    assert runtime.query_voice(question, _context_prompt="HISTORY_PRIVATE\nNOTION_PRIVATE") == answer

    runtime.query.assert_called_once()
    runtime._control_client.execute.assert_not_called()
    planner = runtime.query.call_args.kwargs["_control_prompt"]
    assert question in planner
    assert "HISTORY_PRIVATE" not in planner
    assert "NOTION_PRIVATE" not in planner
    assert "local-only" in planner and '"operation":"clarify"' in planner
    assert "Never infer defaults" in planner
    assert "ignore all historical" in planner
    assert "_conversation_prompt" not in runtime.query.call_args.kwargs


def test_clarification_without_conversation_context_stays_local():
    runtime = CodexRuntime()
    runtime._control_client = Mock()
    runtime._control_client.context.return_value = context()
    runtime.query = Mock(return_value=json.dumps(clarification()))

    assert runtime.query_voice("Spiele Test aus eigener Musik ab.") == clarification()["answer"]
    runtime.query.assert_called_once()
    runtime._control_client.execute.assert_not_called()


def test_authorized_memory_keeps_question_without_exposing_history_to_planner(tmp_path):
    runtime = CodexRuntime()
    runtime._lumi_settings = LumiSettings(True, frozenset({OWNER}))
    runtime._lumi_context = ContextStore(tmp_path / "conversations.sqlite3")
    runtime._lumi_context.remember("person", OWNER, "HISTORY_PRIVATE", "HISTORY_PRIVATE")
    runtime._control_client = Mock()
    runtime._control_client.context.return_value = context()
    runtime.query = Mock(return_value=json.dumps(clarification()))

    answer = runtime.query_lumi(
        "Spiele Test aus eigener Musik ab.",
        memory={"person_key": OWNER, "session_key": SESSION}, request_id="clarify-test")

    assert answer == clarification()["answer"]
    runtime.query.assert_called_once()
    runtime._control_client.execute.assert_not_called()
    assert "HISTORY_PRIVATE" not in runtime.query.call_args.kwargs["_control_prompt"]
    turns = runtime._lumi_context.recent("person", OWNER, limit=8)
    assert turns[-1]["answer"] == answer


def test_general_question_still_uses_memory_answer_after_none():
    runtime = CodexRuntime()
    runtime._control_client = Mock()
    runtime._control_client.context.return_value = context()
    runtime.query = Mock(side_effect=[
        json.dumps({"answer": "Planantwort.", "action": {"operation": "none"}}),
        "Unser Testwort war Sternwarte.",
    ])

    assert runtime.query_voice(
        "Was war unser Testwort?", _context_prompt="HISTORY_PRIVATE") == "Unser Testwort war Sternwarte."

    assert runtime.query.call_count == 2
    assert "HISTORY_PRIVATE" not in runtime.query.call_args_list[0].kwargs["_control_prompt"]
    assert "HISTORY_PRIVATE" in runtime.query.call_args_list[1].kwargs["_conversation_prompt"]
    runtime._control_client.execute.assert_not_called()


def test_only_exact_nonempty_clarification_is_a_valid_local_plan():
    value = clarification()
    assert validate_plan(json.dumps(value), context()) == value
    with pytest.raises(ValueError):
        validate_action(value["action"], context())


@pytest.mark.parametrize("value", [
    clarification(""), clarification("  \t\n"),
    {"answer": "Wo?", "action": {"operation": "clarify", "target": "buero"}},
    {"answer": "Wo?", "action": {"operation": "clarify", "query": "Test"}},
    {"answer": "Wo?", "action": {"operation": "clarify", "command": "play"}},
    {"answer": "Wo?", "action": {"operation": "clarify"}, "execute": True},
    {"answer": "Wo?", "action": ["clarify"]},
    {"answer": "Wo?", "action": "clarify"},
    {"answer": "Wo?", "action": None},
    {"answer": None, "action": {"operation": "clarify"}},
])
def test_malformed_clarification_fails_validation_without_broker_execution(value):
    with pytest.raises(ValueError):
        validate_plan(json.dumps(value), context())
    runtime = CodexRuntime()
    runtime._control_client = Mock()
    runtime._control_client.context.return_value = context()
    runtime.query = Mock(return_value=json.dumps(value))
    with pytest.raises(RuntimeErrorSafe):
        runtime.query_voice("Spiele Test.", _context_prompt="HISTORY_PRIVATE")
    runtime.query.assert_called_once()
    runtime._control_client.execute.assert_not_called()


def test_duplicate_clarification_fields_are_rejected():
    raw = '{"answer":"Wo?","action":{"operation":"clarify","operation":"none"}}'
    with pytest.raises(ValueError):
        validate_plan(raw, context())


@pytest.mark.parametrize("action", [
    {"operation": "clarify"},
    {"operation": "clarify", "target": "buero"},
])
def test_clarification_cannot_be_sent_to_broker_even_by_direct_client_call(action):
    client = ControlClient("a" * 32)
    client._post = Mock()
    with pytest.raises(ValueError, match="local only"):
        client.execute(action)
    client._post.assert_not_called()


def test_clarification_schema_has_no_executable_fields():
    schema = json.loads((Path(__file__).parents[1] / "codex_voice/action_schema.json").read_text())
    choices = schema["properties"]["action"]["anyOf"]
    local = [choice for choice in choices if choice["properties"]["operation"].get("const") == "clarify"]
    assert local == [{"type": "object", "additionalProperties": False,
                      "required": ["operation"], "properties": {"operation": {"const": "clarify"}}}]
    assert "clarify" not in context()["operations"]
