"""Private, offline cache for explicitly approved shared Notion pages."""

from __future__ import annotations

import os
import re
import sqlite3
import stat
import time
import math
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

MAX_PAGES = 100
MAX_TOTAL_BYTES = 2 * 1024 * 1024
MAX_PAGE_TEXT = 30_000
MAX_TITLE = 200
MAX_EXCERPT = 1_200
STALE_AFTER_SECONDS = 24 * 60 * 60
_ALLOWED_HOSTS = {"notion.so", "www.notion.so", "app.notion.com"}
_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)


def _uuid(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a UUID string")
    try:
        return str(UUID(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"{field} must be a UUID string") from exc


def _validate_url(value: object, page_id: str) -> str:
    if not isinstance(value, str) or len(value) > 2_048:
        raise ValueError("url must be a short HTTPS Notion URL")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        # Accessing .port also rejects malformed ports.
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid Notion URL") from exc
    if (parsed.scheme != "https" or host not in _ALLOWED_HOSTS or
            parsed.username is not None or parsed.password is not None or port is not None):
        raise ValueError("url must use an approved HTTPS Notion host")
    dashed = page_id.casefold()
    compact = page_id.replace("-", "").casefold()
    if not any(
        segment.casefold() in {dashed, compact}
        or segment.casefold().endswith("-" + dashed)
        or segment.casefold().endswith("-" + compact)
        for segment in parsed.path.split("/") if segment
    ):
        raise ValueError("url path must contain the matching page UUID")
    return value


def _iso_datetime(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ValueError("edited_at must be an ISO datetime string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("edited_at must be an ISO datetime string") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("edited_at must include a timezone")
    return value


def _tokens(value: str) -> list[str]:
    # Regex keeps Unicode letters and digits while excluding underscore.
    return [match.casefold() for match in _TOKEN_RE.findall(value)]


def _excerpt(text: str, query_tokens: list[str]) -> str:
    if len(text) <= MAX_EXCERPT:
        return text
    folded = text.casefold()
    positions = [folded.find(token) for token in query_tokens]
    positions = [position for position in positions if position >= 0]
    start = max(0, (min(positions) if positions else 0) - MAX_EXCERPT // 4)
    end = min(len(text), start + MAX_EXCERPT)
    start = max(0, end - MAX_EXCERPT)
    excerpt = text[start:end]
    if start:
        excerpt = "…" + excerpt[1:]
    if end < len(text):
        excerpt = excerpt[:-1] + "…"
    return excerpt[:MAX_EXCERPT]


class KnowledgeStore:
    """SQLite cache whose contents are restricted to explicitly allowed pages.

    ``path`` is the database filename. Its parent directory is created with
    private permissions when necessary.
    """

    def __init__(self, path: os.PathLike[str] | str, clock=time.time, *,
                 max_pages: int = MAX_PAGES, max_total_bytes: int = MAX_TOTAL_BYTES):
        if (type(max_pages) is not int or not 1 <= max_pages <= 10000
                or type(max_total_bytes) is not int or not 1 <= max_total_bytes <= 32 * 1024 * 1024):
            raise ValueError("invalid knowledge limits")
        self.max_pages = max_pages
        self.max_total_bytes = max_total_bytes
        self.path = Path(path).absolute()
        self.clock = clock
        self._secure_filesystem()
        self._initialize()

    def _secure_filesystem(self) -> None:
        parent = self.path.parent
        # Refuse symlinks in the path before creating or opening anything.
        current = Path(self.path.anchor)
        for part in self.path.parts[1:-1]:
            current = current / part
            if current.is_symlink():
                raise OSError("symlink in knowledge store path")
        if parent.is_symlink():
            raise OSError("knowledge store directory must not be a symlink")
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if parent.is_symlink() or not parent.is_dir():
            raise OSError("knowledge store directory must be a real directory")
        os.chmod(parent, 0o700)

        if self.path.is_symlink():
            raise OSError("knowledge database must not be a symlink")
        if self.path.exists():
            if not stat.S_ISREG(self.path.stat().st_mode):
                raise OSError("knowledge database must be a regular file")
            os.chmod(self.path, 0o600)
        else:
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(self.path, flags, 0o600)
            os.close(fd)

    def _connect(self) -> sqlite3.Connection:
        if self.path.is_symlink():
            raise OSError("knowledge database must not be a symlink")
        connection = sqlite3.connect(str(self.path), timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS pages ("
                "page_id TEXT PRIMARY KEY, title TEXT NOT NULL, url TEXT NOT NULL, "
                "text TEXT NOT NULL, synced_at REAL NOT NULL, edited_at TEXT NOT NULL)"
            )
            connection.commit()
        finally:
            connection.close()
        os.chmod(self.path, 0o600)

    @staticmethod
    def _validate_pages(pages, allowed_ids, *, max_pages=MAX_PAGES,
                        max_total_bytes=MAX_TOTAL_BYTES) -> list[tuple[str, str, str, str, str]]:
        if isinstance(allowed_ids, (str, bytes)):
            raise ValueError("allowed_ids must be an explicit collection of UUIDs")
        try:
            allowed = {_uuid(item, "allowed_id") for item in allowed_ids}
        except TypeError as exc:
            raise ValueError("allowed_ids must be an explicit collection of UUIDs") from exc
        if isinstance(pages, (str, bytes)):
            raise ValueError("pages must be a collection of page records")
        try:
            records = list(pages)
        except TypeError as exc:
            raise ValueError("pages must be a collection of page records") from exc
        if len(records) > max_pages:
            raise ValueError("too many pages")
        validated = []
        seen = set()
        total_bytes = 0
        required = {"page_id", "title", "url", "text", "edited_at"}
        for record in records:
            if not isinstance(record, dict) or set(record) != required:
                raise ValueError("each page must contain exactly page_id, title, url, text, edited_at")
            page_id = _uuid(record["page_id"], "page_id")
            if page_id not in allowed:
                raise ValueError("page is outside the explicitly allowed set")
            if page_id in seen:
                raise ValueError("duplicate page_id")
            seen.add(page_id)
            title, text = record["title"], record["text"]
            if not isinstance(title, str) or len(title) > MAX_TITLE:
                raise ValueError("title must be at most 200 characters")
            if not isinstance(text, str) or len(text) > MAX_PAGE_TEXT:
                raise ValueError("text must be at most 30000 characters")
            url = _validate_url(record["url"], page_id)
            edited_at = _iso_datetime(record["edited_at"])
            total_bytes += sum(len(value.encode("utf-8")) for value in (title, text, url, edited_at))
            if total_bytes > max_total_bytes:
                raise ValueError("page set exceeds byte limit")
            validated.append((page_id, title, url, text, edited_at))
        return validated

    def replace_pages(self, pages, allowed_ids) -> None:
        """Atomically replace the whole cache with validated allowed pages."""
        validated = self._validate_pages(pages, allowed_ids, max_pages=self.max_pages,
                                         max_total_bytes=self.max_total_bytes)
        try:
            synced_at = float(self.clock())
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("clock must return a finite timestamp") from exc
        if not math.isfinite(synced_at):
            raise ValueError("clock must return a finite timestamp")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM pages")
            connection.executemany(
                "INSERT INTO pages(page_id,title,url,text,synced_at,edited_at) VALUES(?,?,?,?,?,?)",
                [(page_id, title, url, text, synced_at, edited_at)
                 for page_id, title, url, text, edited_at in validated],
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def revoke_except(self, allowed_ids) -> None:
        """Delete every cached page not present in the explicit allowlist."""
        if isinstance(allowed_ids, (str, bytes)):
            raise ValueError("allowed_ids must be an explicit collection of UUIDs")
        try:
            allowed = {_uuid(item, "allowed_id") for item in allowed_ids}
        except TypeError as exc:
            raise ValueError("allowed_ids must be an explicit collection of UUIDs") from exc
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cached_ids = connection.execute("SELECT page_id FROM pages").fetchall()
            revoked = [(row["page_id"],) for row in cached_ids if row["page_id"] not in allowed]
            connection.executemany("DELETE FROM pages WHERE page_id=?", revoked)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def search(self, question: str, limit: int = 3) -> list[dict]:
        """Search the local cache only; no connector or network is used."""
        if not isinstance(question, str):
            return []
        tokens = list(dict.fromkeys(_tokens(question)))
        if not tokens:
            return []
        try:
            limit = int(limit)
        except (TypeError, ValueError, OverflowError):
            return []
        if limit <= 0:
            return []
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT page_id,title,url,text,synced_at,edited_at FROM pages"
            ).fetchall()
        finally:
            connection.close()
        ranked = []
        try:
            now = float(self.clock())
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("clock must return a finite timestamp") from exc
        if not math.isfinite(now):
            raise ValueError("clock must return a finite timestamp")
        for row in rows:
            title_tokens = _tokens(row["title"])
            body_tokens = _tokens(row["text"])
            title_counts = {token: title_tokens.count(token) for token in set(tokens)}
            body_counts = {token: body_tokens.count(token) for token in set(tokens)}
            score = sum(title_counts.values()) * 4 + sum(body_counts.values())
            if score == 0:
                continue
            ranked.append((score, row, {
                "page_id": row["page_id"], "title": row["title"], "url": row["url"],
                "text": _excerpt(row["text"], tokens), "synced_at": row["synced_at"],
                "edited_at": row["edited_at"],
                "stale": now - row["synced_at"] > STALE_AFTER_SECONDS,
            }))
        ranked.sort(key=lambda item: (-item[0], item[1]["title"].casefold(), item[1]["page_id"]))
        return [item[2] for item in ranked[:limit]]
