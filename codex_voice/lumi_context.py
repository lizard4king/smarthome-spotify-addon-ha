"""Small, bounded local conversation context for Lumi Voice jobs."""
from __future__ import annotations

from collections import OrderedDict
from contextlib import closing
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import time
from typing import Callable


PERSON_SCOPE = "person"
SESSION_SCOPE = "session"
NONE_SCOPE = "none"
_SCOPES = {PERSON_SCOPE, SESSION_SCOPE, NONE_SCOPE}
_KEY_RE = re.compile(r"[0-9a-fA-F]{64}\Z")
_WORD_RE = re.compile(r"[^\W_]{2,}", re.UNICODE)
_RETENTION_SECONDS = 90 * 24 * 60 * 60
_SESSION_TTL_SECONDS = 10 * 60
_MAX_TURNS_PER_PERSON = 500
_MAX_PERSONS = 100
_MAX_SESSIONS = 32
_MAX_TURNS_PER_SESSION = 12
_MAX_RECENT = 12
_MAX_QUESTION = 600
_MAX_ANSWER = 6000

_path_locks_guard = threading.Lock()
_path_locks: dict[str, threading.RLock] = {}


def _scope_key(scope: str, key: str) -> tuple[str, str]:
    if not isinstance(scope, str) or scope not in _SCOPES:
        raise ValueError("scope must be 'person', 'session', or 'none'")
    if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
        raise ValueError("key must be exactly 64 hexadecimal characters")
    return scope, key.lower()


def _text(value: str, name: str, maximum: int) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f"{name} must be text of at most {maximum} characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{name} must be valid UTF-8 text") from exc
    return value


def _time_value(clock: Callable[[], float]) -> float:
    value = float(clock())
    if not math.isfinite(value):
        raise ValueError("clock must return a finite timestamp")
    return value


def _resolve_database_path(path: str | os.PathLike[str]) -> Path:
    requested = Path(path).expanduser()
    if not requested.is_absolute():
        requested = Path.cwd() / requested
    lexical = Path(os.path.abspath(requested))

    cursor = Path(lexical.anchor)
    for component in lexical.parts[1:-1]:
        cursor = cursor / component
        if cursor.is_symlink():
            raise ValueError("database path must not traverse a symbolic link")
    if lexical.is_symlink():
        raise ValueError("database path must not be a symbolic link")
    resolved = lexical.resolve(strict=False)
    if resolved != lexical:
        raise ValueError("database path must resolve without symbolic links")

    if resolved.exists():
        if resolved.is_symlink() or not stat.S_ISREG(resolved.stat().st_mode):
            raise ValueError("database path must be a regular file, not a link")
    return resolved


def _lock_for(path: Path) -> threading.RLock:
    name = os.path.normcase(str(path))
    with _path_locks_guard:
        lock = _path_locks.get(name)
        if lock is None:
            lock = threading.RLock()
            _path_locks[name] = lock
        return lock


class ContextStore:
    """Read and write bounded context for already-authorized scope keys.

    Person records use a private SQLite database. Session records live only in
    this object's memory and expire ten minutes after each recorded turn. The
    parent runtime remains responsible for authorizing the supplied key.
    """

    def __init__(self, path: str | os.PathLike[str], clock: Callable[[], float] = time.time):
        self._clock = clock
        self._db_path = _resolve_database_path(path)
        self._lock = _lock_for(self._db_path)
        self._session_lock = threading.RLock()
        self._sessions: OrderedDict[str, list[dict[str, object]]] = OrderedDict()

    def _ensure_database(self) -> None:
        parent = self._db_path.parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if parent.is_symlink() or not parent.is_dir():
            raise ValueError("database parent must be a real directory")
        cursor = Path(self._db_path.anchor)
        for component in self._db_path.parts[1:-1]:
            cursor = cursor / component
            if cursor.is_symlink():
                raise ValueError("database path must not traverse a symbolic link")
        os.chmod(parent, 0o700)
        if self._db_path.is_symlink():
            raise ValueError("database path must not be a symbolic link")
        if self._db_path.exists():
            if not stat.S_ISREG(self._db_path.stat().st_mode):
                raise ValueError("database path must be a regular file, not a link")
            os.chmod(self._db_path, 0o600)
        else:
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                descriptor = os.open(self._db_path, flags, 0o600)
            except FileExistsError:
                if self._db_path.is_symlink() or not stat.S_ISREG(self._db_path.stat().st_mode):
                    raise ValueError("database path must be a regular file, not a link")
                os.chmod(self._db_path, 0o600)
            else:
                os.close(descriptor)
        with closing(self._connect()) as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS turns (
                    id INTEGER PRIMARY KEY,
                    person_key TEXT NOT NULL,
                    question TEXT NOT NULL,
                    answer TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
            """)
            connection.execute(
                "CREATE INDEX IF NOT EXISTS turns_person_time ON turns(person_key, created_at, id)"
            )
            connection.commit()
            os.chmod(self._db_path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path, timeout=10.0)
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _connect_existing(self) -> sqlite3.Connection:
        # mode=rw prevents SQLite from creating a database if it disappears
        # between the existence check and connect.
        connection = sqlite3.connect(
            self._db_path.as_uri() + "?mode=rw", uri=True, timeout=10.0
        )
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @staticmethod
    def _validate(scope: str, key: str) -> tuple[str, str]:
        return _scope_key(scope, key)

    def remember(self, scope: str, key: str, question: str, answer: str) -> None:
        scope, key = self._validate(scope, key)
        question = _text(question, "question", _MAX_QUESTION)
        answer = _text(answer, "answer", _MAX_ANSWER)
        if scope == NONE_SCOPE:
            return
        now = _time_value(self._clock)
        record = {"question": question, "answer": answer, "created_at": now}
        if scope == SESSION_SCOPE:
            with self._session_lock:
                self._expire_sessions(now)
                if key not in self._sessions and len(self._sessions) >= _MAX_SESSIONS:
                    self._sessions.popitem(last=False)
                turns = self._sessions.setdefault(key, [])
                turns.append(record)
                if len(turns) > _MAX_TURNS_PER_SESSION:
                    del turns[:-_MAX_TURNS_PER_SESSION]
                self._sessions.move_to_end(key)
            return

        cutoff = now - _RETENTION_SECONDS
        with self._lock:
            self._ensure_database()
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("DELETE FROM turns WHERE created_at < ?", (cutoff,))
                has_person = connection.execute(
                    "SELECT 1 FROM turns WHERE person_key = ? LIMIT 1", (key,)
                ).fetchone() is not None
                if not has_person:
                    persons = connection.execute(
                        "SELECT person_key FROM turns GROUP BY person_key "
                        "ORDER BY MAX(created_at), MIN(id) LIMIT ?",
                        (_MAX_PERSONS,),
                    ).fetchall()
                    if len(persons) >= _MAX_PERSONS:
                        oldest_key = persons[0][0]
                        connection.execute("DELETE FROM turns WHERE person_key = ?", (oldest_key,))
                connection.execute(
                    "INSERT INTO turns(person_key, question, answer, created_at) VALUES (?, ?, ?, ?)",
                    (key, question, answer, now),
                )
                connection.execute(
                    "DELETE FROM turns WHERE id IN ("
                    "SELECT id FROM turns WHERE person_key = ? "
                    "ORDER BY created_at DESC, id DESC LIMIT -1 OFFSET ?)",
                    (key, _MAX_TURNS_PER_PERSON),
                )
                connection.commit()
                os.chmod(self._db_path, 0o600)

    def recent(
        self, scope: str, key: str, question: str = "", limit: int = 8
    ) -> list[dict[str, object]]:
        scope, key = self._validate(scope, key)
        question = _text(question, "question", _MAX_QUESTION)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError("limit must be a non-negative integer")
        limit = min(limit, _MAX_RECENT)
        if scope == NONE_SCOPE or limit == 0:
            return []
        now = _time_value(self._clock)
        if scope == SESSION_SCOPE:
            with self._session_lock:
                self._expire_sessions(now)
                rows = list(self._sessions.get(key, ()))
            return self._select(rows, question, limit)

        cutoff = now - _RETENTION_SECONDS
        with self._lock:
            self._ensure_database()
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("DELETE FROM turns WHERE created_at < ?", (cutoff,))
                rows = connection.execute(
                    "SELECT question, answer, created_at FROM turns "
                    "WHERE person_key = ? ORDER BY created_at DESC, id DESC LIMIT ?",
                    (key, _MAX_TURNS_PER_PERSON),
                ).fetchall()
                connection.commit()
        records = [dict(question=row[0], answer=row[1], created_at=row[2]) for row in rows]
        records.reverse()
        return self._select(records, question, limit)

    def forget(self, scope: str, key: str) -> None:
        scope, key = self._validate(scope, key)
        if scope == NONE_SCOPE:
            return
        if scope == SESSION_SCOPE:
            with self._session_lock:
                self._sessions.pop(key, None)
            return
        with self._lock:
            self._ensure_database()
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("DELETE FROM turns WHERE person_key = ?", (key,))
                connection.commit()

    def revoke_except(self, allowed_keys: object) -> None:
        """Remove persistent context outside a validated owner-key allowlist."""
        if isinstance(allowed_keys, (str, bytes)):
            raise ValueError("allowed_keys must be a collection of keys")
        try:
            supplied = list(allowed_keys)  # type: ignore[arg-type]
        except TypeError as exc:
            raise ValueError("allowed_keys must be a collection of keys") from exc
        if len(supplied) > _MAX_PERSONS:
            raise ValueError("allowed_keys may contain at most 100 keys")
        normalized: list[str] = []
        for key in supplied:
            if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
                raise ValueError("each allowed key must be 64 hexadecimal characters")
            normalized_key = key.lower()
            if normalized_key in normalized:
                raise ValueError("allowed_keys must not contain duplicates")
            normalized.append(normalized_key)

        now = _time_value(self._clock)
        cutoff = now - _RETENTION_SECONDS
        with self._session_lock:
            permitted = set(normalized)
            for key in list(self._sessions):
                if key not in permitted:
                    del self._sessions[key]

        with self._lock:
            if not self._db_path.exists():
                return
            if self._db_path.is_symlink() or not stat.S_ISREG(self._db_path.stat().st_mode):
                raise ValueError("database path must be a regular file, not a link")
            with closing(self._connect_existing()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                if normalized:
                    placeholders = ", ".join("?" for _ in normalized)
                    connection.execute(
                        f"DELETE FROM turns WHERE created_at < ? "
                        f"OR person_key NOT IN ({placeholders})",
                        (cutoff, *normalized),
                    )
                else:
                    connection.execute(
                        "DELETE FROM turns WHERE created_at < ? OR person_key IS NOT NULL",
                        (cutoff,),
                    )
                connection.commit()

    def _expire_sessions(self, now: float) -> None:
        cutoff = now - _SESSION_TTL_SECONDS
        for key in list(self._sessions):
            retained = [record for record in self._sessions[key]
                        if float(record["created_at"]) >= cutoff]
            if retained:
                self._sessions[key] = retained
            else:
                del self._sessions[key]

    @staticmethod
    def _select(
        rows: list[dict[str, object]], question: str, limit: int
    ) -> list[dict[str, object]]:
        newest = rows[-limit:] if rows else []
        selected_ids = {id(row) for row in newest}
        remaining = _MAX_RECENT - len(newest)
        tokens = set(_WORD_RE.findall(question.casefold()))
        if remaining > 0 and tokens:
            relevant = []
            for row in rows:
                if id(row) in selected_ids:
                    continue
                overlap = tokens.intersection(_WORD_RE.findall(str(row["question"]).casefold()))
                if overlap:
                    relevant.append((len(overlap), float(row["created_at"]), row))
            relevant.sort(key=lambda item: (item[0], item[1]), reverse=True)
            newest.extend(item[2] for item in relevant[:remaining])
        newest.sort(key=lambda row: float(row["created_at"]))
        return [
            {
                "question": str(row["question"]),
                "answer": str(row["answer"]),
                "created_at": float(row["created_at"]),
            }
            for row in newest
        ]
