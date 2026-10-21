"""Runtime acceptance for isolated cached greetings, with no real model calls."""
from concurrent.futures import ThreadPoolExecutor
import threading

from codex_voice.app import CodexRuntime
from codex_voice.lumi_assistant import GREETING_FALLBACK, GreetingCache, LumiSettings
from codex_voice.lumi_context import ContextStore

OWNER = "a" * 64
SESSION = "b" * 64
MEMORY = {"person_key": OWNER, "session_key": SESSION}


def make_runtime(tmp_path):
    runtime = CodexRuntime()
    runtime._lumi_settings = LumiSettings(True, frozenset({OWNER}))
    runtime._lumi_context = ContextStore(tmp_path / "history.sqlite3")
    runtime._lumi_context.remember("person", OWNER, "Ein vorheriges Gespräch.", "Ein bekannter Gesprächsstand.")
    return runtime


def test_same_allowed_context_reuses_greeting_without_another_model_call(tmp_path):
    runtime = make_runtime(tmp_path)
    calls = []
    runtime.query = lambda *_args, **_kw: calls.append(1) or "Gut, dich wieder zu hören."
    assert runtime.greet_lumi(memory=MEMORY) == "Gut, dich wieder zu hören."
    assert runtime.greet_lumi(memory=MEMORY) == "Gut, dich wieder zu hören."
    assert calls == [1]
    assert len(runtime._lumi_context.recent("person", OWNER)) == 1


def test_unknown_people_cannot_reuse_owner_or_other_session_greeting(tmp_path):
    runtime = make_runtime(tmp_path)
    calls = []
    runtime.query = lambda *_args, **_kw: calls.append(1) or f"Ein kurzer Gruß Nummer {len(calls)}."
    owner = runtime.greet_lumi(memory=MEMORY)
    first = {"person_key": "c" * 64, "session_key": SESSION}
    second = {"person_key": "c" * 64, "session_key": "d" * 64}
    runtime._lumi_context.remember("session", SESSION, "Erste Sitzung.", "Erster Stand.")
    runtime._lumi_context.remember("session", "d" * 64, "Andere Sitzung.", "Anderer Stand.")
    assert runtime.greet_lumi(memory=first) != owner
    assert runtime.greet_lumi(memory=second) != owner
    assert calls == [1, 1, 1]


def test_changed_conversation_invalidates_the_previous_greeting(tmp_path):
    runtime = make_runtime(tmp_path)
    calls = []
    runtime.query = lambda *_args, **_kw: calls.append(1) or "Gut, dich wieder zu hören."
    runtime.greet_lumi(memory=MEMORY)
    runtime.query_voice = lambda *_args, **_kw: "Das ist nun der neue Gesprächsstand."
    runtime.query_lumi("Der Plan hat sich geändert.", memory=MEMORY)
    runtime.greet_lumi(memory=MEMORY)
    assert calls == [1, 1]
    assert len(runtime._lumi_context.recent("person", OWNER)) == 2


def test_context_changed_outside_runtime_cannot_match_the_old_cache(tmp_path):
    runtime = make_runtime(tmp_path)
    calls = []
    runtime.query = lambda *_args, **_kw: calls.append(1) or "Gut, dich wieder zu hören."
    runtime.greet_lumi(memory=MEMORY)
    runtime._lumi_context.remember("person", OWNER, "Ein neuer Stand", "Vermerkt")
    runtime.greet_lumi(memory=MEMORY)
    assert calls == [1, 1]


def test_forgetting_invalidates_cached_greeting_and_empty_history_stays_neutral(tmp_path):
    runtime = make_runtime(tmp_path)
    calls = []
    runtime.query = lambda *_args, **_kw: calls.append(1) or "Gut, dich wieder zu hören."
    runtime.greet_lumi(memory=MEMORY)
    runtime.query_lumi("Lösche meinen Gesprächsverlauf", memory=MEMORY)
    assert runtime.greet_lumi(memory=MEMORY) == GREETING_FALLBACK
    assert calls == [1]


def test_expired_cache_retries_model_and_control_does_not_disable_greeting(tmp_path):
    runtime = make_runtime(tmp_path)
    clock = [1000.0]
    runtime._lumi_greeting_cache = GreetingCache(clock=lambda: clock[0])
    calls = []
    runtime.query = lambda *_args, **_kw: calls.append(1) or "Gut, dich wieder zu hören."
    runtime.greet_lumi(memory=MEMORY)
    clock[0] += 301
    runtime.greet_lumi(memory=MEMORY)
    runtime._control_client = object()
    assert runtime.greet_lumi(memory=MEMORY) == "Gut, dich wieder zu hören."
    assert calls == [1, 1]


def test_late_greeting_after_forget_is_discarded_and_cannot_restore_cache(tmp_path):
    runtime = make_runtime(tmp_path)
    entered, release = threading.Event(), threading.Event()

    def slow_query(*_args, **_kw):
        entered.set()
        assert release.wait(3)
        return "Ein inzwischen veralteter persönlicher Gruß."

    runtime.query = slow_query
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(runtime.greet_lumi, memory=MEMORY)
        try:
            assert entered.wait(3)
            runtime.query_lumi("Lösche meinen Gesprächsverlauf", memory=MEMORY)
        finally:
            release.set()
        assert future.result(timeout=3) == GREETING_FALLBACK
    calls = []
    runtime.query = lambda *_args, **_kw: calls.append(1) or "Ein neuer neutraler Gruß."
    assert runtime.greet_lumi(memory=MEMORY) == GREETING_FALLBACK
    assert calls == []


def test_answer_finishing_after_forget_does_not_recreate_deleted_history(tmp_path):
    runtime = make_runtime(tmp_path)
    runtime._lumi_context.remember("person", OWNER, "Vorher", "Alter Kontext")
    entered, release = threading.Event(), threading.Event()

    def slow_answer(*_args, **_kw):
        entered.set()
        assert release.wait(3)
        return "Antwort auf die vorher begonnene Frage."

    runtime.query_voice = slow_answer
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(runtime.query_lumi, "Die alte Frage", memory=MEMORY)
        try:
            assert entered.wait(3)
            runtime.query_lumi("Lösche meinen Gesprächsverlauf", memory=MEMORY)
        finally:
            release.set()
        assert future.result(timeout=3) == "Antwort auf die vorher begonnene Frage."
    assert runtime._lumi_context.recent("person", OWNER) == []
