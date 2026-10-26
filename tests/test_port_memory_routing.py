"""Keep the newer Lumi contracts while adding a private identity window.

The original PR80 tests described the superseded 0.1.11 port. These checks keep
accepted personality, pause/erase, long-answer and isolation behaviour. Stores
and control calls below are synthetic; no real device or model is contacted.
"""
from __future__ import annotations

import importlib.util
import json

import pytest

from codex_voice.app import CodexRuntime
from codex_voice.lumi_assistant import LumiSettings, PERSONA
from codex_voice.lumi_context import ContextStore

OWNER = "a" * 64
OTHER = "b" * 64
SESSION = "c" * 64
MEMORY = {"person_key": OWNER, "session_key": SESSION}


class RecordingControl:
    def __init__(self):
        self.executed = []

    def context(self):
        return {
            "version": 1,
            "devices": {"testlampe": {"domain": "light", "writable": True}},
            "spotify": {"enabled": False, "profiles": [], "targets": []},
            "operations": ["none", "set_light"],
        }

    def execute(self, action):
        self.executed.append(action)
        return "Die Testlampe ist eingeschaltet."


def runtime_with_store(tmp_path, *, enabled=True, control=None):
    runtime = CodexRuntime()
    runtime._lumi_settings = LumiSettings(enabled, frozenset({OWNER}))
    runtime._lumi_context = ContextStore(tmp_path / "history.sqlite3")
    runtime._control_client = control
    return runtime


def test_notion_and_answer_modules_remain_available():
    for name in (
        "codex_voice.lumi_knowledge", "codex_voice.lumi_notion_catalog",
        "codex_voice.lumi_notion_commands", "codex_voice.lumi_notion_credentials",
        "codex_voice.lumi_notion_runtime", "codex_voice.lumi_notion_sync",
        "codex_voice.lumi_notion_write", "music_alexa.answer_pages",
        "music_alexa.lumi_greeting", "music_alexa.lumi_identity",
    ):
        assert importlib.util.find_spec(name) is not None


def test_paused_memory_retains_history_but_does_not_use_or_extend_it(tmp_path):
    runtime = runtime_with_store(tmp_path, enabled=False)
    runtime._lumi_context.remember("person", OWNER, "OWNER-OLD-QUESTION", "OWNER-PRIVATE-ANSWER")
    before = runtime._lumi_context.recent("person", OWNER)
    prompts = []
    runtime.query = lambda question, **kw: prompts.append(kw) or "Eine aktuelle Antwort."

    assert runtime.query_lumi("Wie spät ist es?", memory=MEMORY) == "Eine aktuelle Antwort."
    assert len(prompts) == 1
    assert prompts[0]["_conversation_prompt"].startswith(PERSONA)
    assert "OWNER-PRIVATE" not in prompts[0]["_conversation_prompt"]
    assert runtime._lumi_context.recent("person", OWNER) == before


def test_explicit_own_erase_still_works_while_memory_is_paused(tmp_path):
    runtime = runtime_with_store(tmp_path, enabled=False)
    runtime._lumi_context.remember("person", OWNER, "own", "erase")
    runtime._lumi_context.remember("session", SESSION, "session", "erase")
    runtime._lumi_context.remember("person", OTHER, "other", "preserve")
    runtime.query = lambda *a, **kw: pytest.fail("Explicit erase does not call a model")

    assert runtime.query_lumi("Lösche meinen Gesprächsverlauf", memory=MEMORY) == (
        "Dein gespeicherter Gesprächsverlauf wurde gelöscht.")
    assert runtime._lumi_context.recent("person", OWNER) == []
    assert runtime._lumi_context.recent("session", SESSION) == []
    assert runtime._lumi_context.recent("person", OTHER)[0]["answer"] == "preserve"


def test_current_control_planning_never_receives_old_action_text(tmp_path):
    control = RecordingControl()
    runtime = runtime_with_store(tmp_path, control=control)
    runtime._lumi_context.remember("person", OWNER, "OLD-ACTION", "OLD-PRIVATE-CONTENT")
    calls = []

    def query(question, **kwargs):
        calls.append((question, kwargs))
        return json.dumps({"answer": "Keine Ausführung behaupten.",
                           "action": {"operation": "set_light", "alias": "testlampe", "is_on": True}})

    runtime.query = query
    assert runtime.query_lumi("Schalte die Testlampe ein.", memory=MEMORY) == (
        "Die Testlampe ist eingeschaltet.")
    assert len(calls) == 1
    assert "OLD-" not in calls[0][1]["_control_prompt"]
    assert "_conversation_prompt" not in calls[0][1]
    assert control.executed == [{"operation": "set_light", "alias": "testlampe", "is_on": True}]


def test_first_knowledge_answer_retains_personality_and_full_length(tmp_path):
    control = RecordingControl()
    runtime = runtime_with_store(tmp_path, control=control)
    calls = []
    answer = "Eine ausführliche Antwort. " * 110

    def query(question, **kwargs):
        calls.append(kwargs)
        if "_control_prompt" in kwargs:
            return json.dumps({"answer": "Kurzantwort", "action": {"operation": "none"}})
        return answer

    runtime.query = query
    assert runtime.query_lumi("Erkläre das ausführlich.", memory=MEMORY) == answer.strip()
    assert len(calls) == 2
    assert calls[1]["_conversation_prompt"].startswith(PERSONA)
    assert len(answer) > 2000
    assert control.executed == []


def test_unknown_speaker_keeps_local_session_without_owner_history(tmp_path):
    runtime = runtime_with_store(tmp_path)
    runtime._lumi_context.remember("person", OWNER, "OWNER-PRIVATE", "OWNER-PRIVATE")
    runtime._lumi_context.remember("session", SESSION, "LOCAL-QUESTION", "LOCAL-ANSWER")
    prompts = []
    runtime.query = lambda q, **kw: prompts.append(kw["_conversation_prompt"]) or "Antwort."
    assert runtime.query_lumi("Was war besprochen?", memory={
        "person_key": OTHER, "session_key": SESSION}) == "Antwort."
    assert "LOCAL-ANSWER" in prompts[0]
    assert "OWNER-PRIVATE" not in prompts[0]


def test_unreadable_history_keeps_current_personality_answer(tmp_path):
    import sqlite3

    runtime = runtime_with_store(tmp_path)
    runtime._lumi_context.recent = lambda *a, **kw: (_ for _ in ()).throw(
        sqlite3.OperationalError("synthetic locked database"))
    prompts = []
    runtime.query = lambda q, **kw: prompts.append(kw["_conversation_prompt"]) or "Aktuelle Antwort."
    assert runtime.query_lumi("Welcher Tag ist heute?", memory=MEMORY) == "Aktuelle Antwort."
    assert prompts[0].startswith(PERSONA)
