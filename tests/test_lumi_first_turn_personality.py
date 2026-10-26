"""First-turn Lumi personality and planner fallback regression checks."""
from __future__ import annotations

import json

import pytest

from codex_voice.app import CodexRuntime
from codex_voice.lumi_assistant import LumiSettings, PERSONA
from codex_voice.lumi_context import ContextStore


OWNER = "a" * 64
UNKNOWN = "b" * 64
SESSION = "c" * 64


class FakeControlClient:
    def __init__(self, *, action=None):
        self.action = action or {"operation": "none"}
        self.executed = []

    def context(self):
        return {
            "operations": ["none", "set_light"],
            "devices": {"desk_lamp": {"domain": "light", "writable": True}},
            "spotify": {"enabled": False, "profiles": [], "targets": []},
        }

    def execute(self, action):
        self.executed.append(action)
        return "Die synthetische Aktion wurde bestätigt."


def make_runtime(tmp_path, *, control=None):
    runtime = CodexRuntime()
    runtime._control_client = control
    runtime._lumi_settings = LumiSettings(True, frozenset({OWNER}))
    runtime._lumi_context = ContextStore(tmp_path / "history.sqlite3")
    return runtime


def memory(person=OWNER, session=SESSION):
    return {"person_key": person, "session_key": session}


def plan(action=None):
    return json.dumps({"answer": "Planner-Kurzantwort.", "action": action or {"operation": "none"}})


def test_first_question_without_history_falls_through_to_persona_and_keeps_long_answer(tmp_path):
    runtime = make_runtime(tmp_path, control=FakeControlClient())
    calls = []
    long_answer = "L" * 2501

    def fake_query(question, **kwargs):
        calls.append((question, kwargs))
        if "_control_prompt" in kwargs:
            return plan()
        return long_answer

    runtime.query = fake_query
    answer = runtime.query_lumi("Erklär mir den synthetischen Test.", memory=memory(), request_id="first")

    assert answer == long_answer
    assert len(calls) == 2
    assert "_control_prompt" in calls[0][1]
    conversation_prompt = calls[1][1]["_conversation_prompt"]
    assert conversation_prompt.startswith(PERSONA)
    assert "memory_scope" in conversation_prompt
    assert "Aktuelle Frage:\nErklär mir den synthetischen Test." in conversation_prompt
    assert len(answer) > 2000 and len(answer) <= 6000


def test_control_action_still_executes_once_without_a_persona_query(tmp_path):
    action = {"operation": "set_light", "alias": "desk_lamp", "is_on": True}
    control = FakeControlClient(action=action)
    runtime = make_runtime(tmp_path, control=control)
    calls = []

    def fake_query(question, **kwargs):
        calls.append((question, kwargs))
        return plan(action)

    runtime.query = fake_query
    answer = runtime.query_lumi("Schalte die Schreibtischlampe ein.", memory=memory(), request_id="action")

    assert answer == "Die synthetische Aktion wurde bestätigt."
    assert len(calls) == 1
    assert "_control_prompt" in calls[0][1]
    assert "_conversation_prompt" not in calls[0][1]
    assert control.executed == [action]


@pytest.mark.parametrize(
    ("person", "session", "expected_scope"),
    [(UNKNOWN, "d" * 64, '"memory_scope": "session"'), ("", "", '"memory_scope": "none"')],
)
def test_unknown_or_missing_memory_never_includes_owner_history(
    tmp_path, person, session, expected_scope
):
    runtime = make_runtime(tmp_path, control=FakeControlClient())
    runtime._lumi_context.remember("person", OWNER, "OWNER-PRIVATE-QUESTION", "OWNER-PRIVATE-ANSWER")
    if session:
        runtime._lumi_context.remember("session", session, "LOCAL-SESSION-QUESTION", "LOCAL-SESSION-ANSWER")
    prompts = []

    def fake_query(question, **kwargs):
        if "_control_prompt" in kwargs:
            return plan()
        prompts.append(kwargs["_conversation_prompt"])
        return "Lokale Antwort."

    runtime.query = fake_query
    runtime.query_lumi("Was war besprochen?", memory=memory(person, session), request_id="isolated")

    assert len(prompts) == 1
    assert expected_scope in prompts[0]
    assert "OWNER-PRIVATE" not in prompts[0]
    if session:
        assert "LOCAL-SESSION-ANSWER" in prompts[0]
    else:
        assert "LOCAL-SESSION-ANSWER" not in prompts[0]
