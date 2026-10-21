from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sqlite3
import stat

import pytest

from codex_voice.lumi_context import ContextStore


PERSON_A = "a" * 64
PERSON_B = "b" * 64


class FakeClock:
    def __init__(self, value: float = 1_800_000_000.0):
        self.value = value

    def __call__(self) -> float:
        return self.value


def test_person_context_survives_restart_and_is_isolated(tmp_path: Path) -> None:
    clock = FakeClock()
    database = tmp_path / "private" / "context.sqlite3"
    first = ContextStore(database, clock=clock)
    first.remember("person", PERSON_A, "Frage A", "Antwort A")
    first.remember("person", PERSON_B, "Frage B", "Antwort B")

    restarted = ContextStore(database, clock=clock)

    assert restarted.recent("person", PERSON_A) == [
        {"question": "Frage A", "answer": "Antwort A", "created_at": clock.value}
    ]
    assert restarted.recent("person", PERSON_B)[0]["answer"] == "Antwort B"


def test_session_is_memory_only_expires_and_forget_is_scope_specific(tmp_path: Path) -> None:
    clock = FakeClock()
    database = tmp_path / "context.sqlite3"
    store = ContextStore(database, clock=clock)
    assert not database.exists()
    store.remember("person", PERSON_A, "persistent", "keep")
    assert database.exists()
    store.remember("session", PERSON_A, "temporary", "expire")

    other_instance = ContextStore(database, clock=clock)
    assert other_instance.recent("session", PERSON_A) == []
    store.forget("session", PERSON_A)
    assert store.recent("session", PERSON_A) == []
    assert store.recent("person", PERSON_A)[0]["answer"] == "keep"

    store.remember("session", PERSON_A, "temporary", "expire")
    clock.value += 600
    assert store.recent("session", PERSON_A)[0]["answer"] == "expire"
    clock.value += 0.01
    assert store.recent("session", PERSON_A) == []


def test_none_scope_never_stores_and_forget_only_removes_matching_scope(tmp_path: Path) -> None:
    clock = FakeClock()
    database = tmp_path / "context.sqlite3"
    store = ContextStore(database, clock=clock)
    store.remember("none", PERSON_A, "discard", "discard")
    assert not database.exists()
    store.remember("person", PERSON_A, "persistent", "saved")
    store.remember("session", PERSON_A, "session", "saved in RAM")

    assert store.recent("none", PERSON_A) == []
    store.forget("person", PERSON_A)
    assert store.recent("person", PERSON_A) == []
    assert store.recent("session", PERSON_A)[0]["answer"] == "saved in RAM"


def test_relevant_older_turn_is_added_to_latest_turns_and_limit_is_capped(tmp_path: Path) -> None:
    clock = FakeClock()
    store = ContextStore(tmp_path / "context.sqlite3", clock=clock)
    store.remember("person", PERSON_A, "Wie funktioniert die Heizung?", "Heizung Antwort")
    for index in range(14):
        clock.value += 1
        store.remember("person", PERSON_A, f"Unverbundene Frage {index}", f"Antwort {index}")

    recent = store.recent("person", PERSON_A, question="Heizung einstellen", limit=8)
    assert len(recent) == 9
    assert recent[0]["question"] == "Wie funktioniert die Heizung?"
    assert [row["question"] for row in recent[-8:]] == [
        f"Unverbundene Frage {index}" for index in range(6, 14)
    ]
    assert len(store.recent("person", PERSON_A, question="Heizung", limit=99)) == 12


def test_person_retention_is_90_days_and_turn_cap_is_500(tmp_path: Path) -> None:
    clock = FakeClock()
    store = ContextStore(tmp_path / "context.sqlite3", clock=clock)
    store.remember("person", PERSON_A, "at boundary", "retained")
    clock.value += 90 * 24 * 60 * 60
    assert store.recent("person", PERSON_A, limit=12)[0]["answer"] == "retained"
    clock.value += 0.01
    assert store.recent("person", PERSON_A) == []

    for index in range(501):
        clock.value += 1
        store.remember("person", PERSON_A, f"Question {index}", f"Answer {index}")
    latest = store.recent("person", PERSON_A, limit=12)
    assert latest[-1]["question"] == "Question 500"
    assert all(row["question"] != "Question 0" for row in store.recent("person", PERSON_A, limit=12))


def test_person_cap_evicts_least_recently_used_key(tmp_path: Path) -> None:
    clock = FakeClock()
    store = ContextStore(tmp_path / "context.sqlite3", clock=clock)
    keys = [f"{index:064x}" for index in range(101)]
    for index, key in enumerate(keys[:100]):
        clock.value += 1
        store.remember("person", key, f"Question {index}", f"Answer {index}")
    clock.value += 1
    store.remember("person", keys[100], "new person", "new answer")

    assert store.recent("person", keys[0]) == []
    assert store.recent("person", keys[1])
    assert store.recent("person", keys[100])


def test_forget_removes_all_turns_for_one_person_only(tmp_path: Path) -> None:
    clock = FakeClock()
    store = ContextStore(tmp_path / "context.sqlite3", clock=clock)
    for index in range(3):
        store.remember("person", PERSON_A, f"Question {index}", f"Answer {index}")
    store.remember("person", PERSON_B, "other", "preserved")

    store.forget("person", PERSON_A)

    assert store.recent("person", PERSON_A) == []
    assert store.recent("person", PERSON_B)[0]["answer"] == "preserved"


@pytest.mark.parametrize("key", ["", "a" * 63, "a" * 65, "g" * 64, 7, None])
@pytest.mark.parametrize("scope", ["person", "session", "none"])
def test_scope_keys_are_strictly_validated(tmp_path: Path, scope: str, key: object) -> None:
    store = ContextStore(tmp_path / "context.sqlite3")
    with pytest.raises(ValueError, match="64 hexadecimal"):
        store.recent(scope, key)  # type: ignore[arg-type]


def test_questions_and_answers_obey_storage_limits(tmp_path: Path) -> None:
    store = ContextStore(tmp_path / "context.sqlite3")
    with pytest.raises(ValueError, match="question"):
        store.remember("person", PERSON_A, "q" * 601, "answer")
    with pytest.raises(ValueError, match="answer"):
        store.remember("person", PERSON_A, "question", "a" * 6001)


def test_database_refuses_symlink_paths(tmp_path: Path) -> None:
    target = tmp_path / "target.sqlite3"
    target.write_bytes(b"do not touch")
    link = tmp_path / "linked.sqlite3"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(ValueError, match="symbolic link|link"):
        ContextStore(link)
    assert target.read_bytes() == b"do not touch"


def test_private_database_permissions_and_parallel_writes(tmp_path: Path) -> None:
    clock = FakeClock()
    database = tmp_path / "private" / "context.sqlite3"
    first = ContextStore(database, clock=clock)
    second = ContextStore(database, clock=clock)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(
            lambda index: (first if index % 2 else second).remember(
                "person", PERSON_A, f"Question {index}", f"Answer {index}"
            ),
            range(30),
        ))

    assert len(first.recent("person", PERSON_A, limit=12)) == 12
    assert stat.S_ISREG(database.stat().st_mode)
    if os.name == "posix":
        assert stat.S_IMODE(database.stat().st_mode) == 0o600
        assert stat.S_IMODE(database.parent.stat().st_mode) == 0o700


def test_session_turn_limit_and_oldest_session_eviction(tmp_path: Path) -> None:
    clock = FakeClock()
    store = ContextStore(tmp_path / "context.sqlite3", clock=clock)
    keys = [f"{index:064x}" for index in range(33)]
    for index in range(13):
        clock.value += 1
        store.remember("session", keys[0], f"Question {index}", f"Answer {index}")
    assert len(store.recent("session", keys[0], limit=99)) == 12
    assert store.recent("session", keys[0], limit=99)[0]["question"] == "Question 1"

    for index, key in enumerate(keys[:32]):
        clock.value += 1
        store.remember("session", key, f"session {index}", "present")
    assert store.recent("session", keys[0])
    clock.value += 1
    store.remember("session", keys[32], "newest session", "present")

    assert store.recent("session", keys[0]) == []
    assert store.recent("session", keys[1])
    assert store.recent("session", keys[32])
    assert len(store._sessions) == 32


def test_revoke_except_deletes_unapproved_and_expired_records_atomically(tmp_path: Path) -> None:
    clock = FakeClock()
    database = tmp_path / "context.sqlite3"
    store = ContextStore(database, clock=clock)
    store.remember("person", PERSON_A, "allowed", "keep")
    store.remember("person", PERSON_B, "revoked", "delete")
    stale_key = "c" * 64
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO turns(person_key, question, answer, created_at) VALUES (?, ?, ?, ?)",
            (stale_key, "expired", "delete", clock.value - 90 * 24 * 60 * 60 - 1),
        )
    store.remember("session", PERSON_A, "allowed session", "keep")
    store.remember("session", PERSON_B, "revoked session", "delete")

    store.revoke_except([PERSON_A, stale_key])

    with sqlite3.connect(database) as connection:
        remaining = connection.execute(
            "SELECT DISTINCT person_key FROM turns ORDER BY person_key"
        ).fetchall()
    assert remaining == [(PERSON_A,)]
    assert store.recent("session", PERSON_A)
    assert store.recent("session", PERSON_B) == []


def test_revoke_except_does_not_create_missing_database(tmp_path: Path) -> None:
    database = tmp_path / "not-created" / "context.sqlite3"
    ContextStore(database).revoke_except([PERSON_A])
    assert not database.exists()
    assert not database.parent.exists()


@pytest.mark.parametrize(
    "allowed_keys",
    ["a" * 64, ["g" * 64], ["a" * 64] * 101, ["a" * 64, "A" * 64]],
)
def test_revoke_except_validates_allowlist(tmp_path: Path, allowed_keys: object) -> None:
    store = ContextStore(tmp_path / "context.sqlite3")
    with pytest.raises(ValueError, match="allowed_keys|hexadecimal"):
        store.revoke_except(allowed_keys)
