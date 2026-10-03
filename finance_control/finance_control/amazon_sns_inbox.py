"""Private, bounded SQLite inbox for authenticated Amazon SNS messages."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from contextlib import closing

from .amazon_sns import SNS_TOPIC_ARN_PATTERN as _ARN, VerifiedSnsMessage
from .storage_paths import outside_repository


class InboxError(RuntimeError):
    """Stable, redacted inbox error code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


_MESSAGE_ID = re.compile(r"^\S{1,128}$")
_FIELDS = ("message_type", "message_id", "topic_arn", "timestamp", "message", "subject", "subscribe_url", "token")


class AmazonSnsInbox:
    """Store verified SNS messages outside Git, using a connection per operation."""

    def __init__(self, database: Path, max_messages: int = 10_000) -> None:
        if type(max_messages) is not int or max_messages <= 0:
            raise InboxError("invalid_configuration")
        try:
            path = outside_repository(Path(database))
            if path.suffix.lower() != ".sqlite" or path.name.lower() == "finance.sqlite":
                raise ValueError
            path.parent.mkdir(parents=True, exist_ok=True)
            path = outside_repository(path)
            if path.exists() and not path.is_file():
                raise ValueError
            self._database = path
            self._max_messages = max_messages
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                objects = connection.execute(
                    "SELECT type,name,tbl_name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
                ).fetchall()
                allowed = {
                    ("table", "amazon_sns_inbox", "amazon_sns_inbox"),
                    ("index", "amazon_sns_inbox_pending", "amazon_sns_inbox"),
                }
                if any(tuple(row) not in allowed for row in objects):
                    raise ValueError
                connection.execute("""CREATE TABLE IF NOT EXISTS amazon_sns_inbox (
                    topic_arn TEXT NOT NULL, message_id TEXT NOT NULL, message_type TEXT NOT NULL,
                    timestamp TEXT NOT NULL, message TEXT NOT NULL, subject TEXT,
                    subscribe_url TEXT, token TEXT, digest TEXT NOT NULL, received_at TEXT NOT NULL,
                    processed INTEGER NOT NULL DEFAULT 0 CHECK(processed IN (0,1)),
                    PRIMARY KEY(topic_arn, message_id)
                )""")
                columns = {row[1] for row in connection.execute("PRAGMA table_info(amazon_sns_inbox)")}
                if columns != set(_FIELDS) | {"digest", "received_at", "processed"}:
                    raise ValueError
                connection.execute("CREATE INDEX IF NOT EXISTS amazon_sns_inbox_pending ON amazon_sns_inbox(message_type, processed, received_at)")
                connection.commit()
        except Exception:
            raise InboxError("invalid_database") from None

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _values(message: VerifiedSnsMessage) -> tuple[tuple, str]:
        try:
            if not isinstance(message, VerifiedSnsMessage):
                raise ValueError
            kind = message.message_type
            mid = message.message_id
            topic = message.topic_arn
            stamp = message.timestamp
            body = message.message
            if kind not in {"Notification", "SubscriptionConfirmation"}:
                raise ValueError
            if not isinstance(mid, str) or not _MESSAGE_ID.fullmatch(mid):
                raise ValueError
            if not isinstance(topic, str) or not _ARN.fullmatch(topic):
                raise ValueError
            if not isinstance(stamp, datetime) or stamp.tzinfo is None:
                raise ValueError
            if not isinstance(body, str) or len(body.encode("utf-8")) > 16_384:
                raise ValueError
            for optional in (message.subject, message.subscribe_url, message.token):
                if optional is not None and not isinstance(optional, str):
                    raise ValueError
            if kind == "Notification" and (message.subscribe_url is not None or message.token is not None):
                raise ValueError
            if kind == "SubscriptionConfirmation" and (not message.subscribe_url or not message.token):
                raise ValueError
            timestamp = stamp.astimezone(timezone.utc).isoformat()
            value = (kind, mid, topic, timestamp, body, message.subject, message.subscribe_url, message.token)
            encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            if len(encoded.encode("utf-8")) > 32_768:
                raise ValueError
            digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
            return value, digest
        except Exception:
            raise InboxError("invalid_message") from None

    def record(self, message: VerifiedSnsMessage) -> bool:
        value, digest = self._values(message)
        kind, mid, topic, timestamp, body, subject, subscribe_url, token = value
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT digest FROM amazon_sns_inbox WHERE topic_arn=? AND message_id=?", (topic, mid)
                ).fetchone()
                if row:
                    if row["digest"] != digest:
                        raise InboxError("message_id_conflict")
                    connection.commit()
                    return False
                count = connection.execute("SELECT COUNT(*) FROM amazon_sns_inbox").fetchone()[0]
                if count >= self._max_messages:
                    raise InboxError("inbox_full")
                connection.execute("""INSERT INTO amazon_sns_inbox
                    (message_type,message_id,topic_arn,timestamp,message,subject,subscribe_url,token,digest,received_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (kind, mid, topic, timestamp, body, subject, subscribe_url, token, digest,
                     datetime.now(timezone.utc).isoformat()))
                connection.commit()
                return True
        except InboxError:
            raise
        except Exception:
            raise InboxError("storage_error") from None

    def _pending(self, kind: str, limit: int | None) -> list[VerifiedSnsMessage]:
        if limit is not None and (type(limit) is not int or limit < 0):
            raise InboxError("invalid_limit")
        try:
            with closing(self._connect()) as connection:
                query = "SELECT * FROM amazon_sns_inbox WHERE message_type=? AND processed=0 ORDER BY received_at,topic_arn,message_id"
                if limit is not None:
                    query += " LIMIT ?"
                    rows = connection.execute(query, (kind, limit)).fetchall()
                else:
                    rows = connection.execute(query, (kind,)).fetchall()
            messages = []
            for row in rows:
                message = VerifiedSnsMessage(
                    message_type=row["message_type"], message_id=row["message_id"], topic_arn=row["topic_arn"],
                    timestamp=datetime.fromisoformat(row["timestamp"]), message=row["message"],
                    subject=row["subject"], subscribe_url=row["subscribe_url"], token=row["token"],
                )
                _, digest = self._values(message)
                if digest != row["digest"]:
                    raise InboxError("storage_error")
                messages.append(message)
            return messages
        except InboxError:
            raise
        except Exception:
            raise InboxError("storage_error") from None

    def pending_notifications(self, limit: int = 100) -> list[VerifiedSnsMessage]:
        return self._pending("Notification", limit)

    def pending_subscriptions(self) -> list[VerifiedSnsMessage]:
        return self._pending("SubscriptionConfirmation", None)

    def mark_processed(self, message_id: str, topic_arn: str) -> None:
        if not isinstance(message_id, str) or not _MESSAGE_ID.fullmatch(message_id) or not isinstance(topic_arn, str) or not _ARN.fullmatch(topic_arn):
            raise InboxError("invalid_message_key")
        try:
            with closing(self._connect()) as connection:
                cursor = connection.execute("UPDATE amazon_sns_inbox SET processed=1 WHERE message_type='Notification' AND message_id=? AND topic_arn=?", (message_id, topic_arn))
                if cursor.rowcount != 1:
                    raise InboxError("message_not_found")
        except InboxError:
            raise
        except Exception:
            raise InboxError("storage_error") from None

    def counts(self) -> dict[str, int]:
        try:
            with closing(self._connect()) as connection:
                values = dict(connection.execute("SELECT message_type || ':' || processed, COUNT(*) FROM amazon_sns_inbox GROUP BY message_type, processed").fetchall())
            pending_notifications = values.get("Notification:0", 0)
            pending_subscriptions = values.get("SubscriptionConfirmation:0", 0)
            processed = values.get("Notification:1", 0)
            return {"pending_notifications": pending_notifications, "pending_subscriptions": pending_subscriptions,
                    "processed": processed, "total": sum(values.values())}
        except Exception:
            raise InboxError("storage_error") from None

    def merge_pending_from(self, snapshot: Path, topic_arn: str) -> int:
        """Atomically merge validated pending notifications from a private snapshot."""
        try:
            if not isinstance(topic_arn, str) or not _ARN.fullmatch(topic_arn):
                raise ValueError
            source_path = outside_repository(Path(snapshot))
            if (source_path.suffix.lower() != ".sqlite" or source_path.name.lower() == "finance.sqlite"
                    or not source_path.is_file() or source_path == self._database):
                raise ValueError
            source_uri = source_path.as_uri() + "?mode=ro"
            with closing(sqlite3.connect(source_uri, uri=True, timeout=10)) as connection:
                connection.row_factory = sqlite3.Row
                objects = connection.execute(
                    "SELECT type,name,tbl_name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
                ).fetchall()
                allowed = {
                    ("table", "amazon_sns_inbox", "amazon_sns_inbox"),
                    ("index", "amazon_sns_inbox_pending", "amazon_sns_inbox"),
                }
                if any(tuple(row) not in allowed for row in objects) or len(objects) != len(allowed):
                    raise ValueError
                columns = {row[1] for row in connection.execute("PRAGMA table_info(amazon_sns_inbox)")}
                if columns != set(_FIELDS) | {"digest", "received_at", "processed"}:
                    raise ValueError
                connection.execute("BEGIN")
                source_count = connection.execute("SELECT COUNT(*) FROM amazon_sns_inbox").fetchone()[0]
                if type(source_count) is not int or source_count > 10_000:
                    raise ValueError
                source_rows = connection.execute(
                    "SELECT * FROM amazon_sns_inbox ORDER BY topic_arn,message_id"
                ).fetchall()
            messages = []
            for row in source_rows:
                message = VerifiedSnsMessage(
                    message_type=row["message_type"], message_id=row["message_id"],
                    topic_arn=row["topic_arn"], timestamp=datetime.fromisoformat(row["timestamp"]),
                    message=row["message"], subject=row["subject"],
                    subscribe_url=row["subscribe_url"], token=row["token"],
                )
                _, expected_digest = self._values(message)
                if (expected_digest != row["digest"] or type(row["processed"]) is not int
                        or row["processed"] not in (0, 1) or message.topic_arn != topic_arn):
                    raise ValueError
                if message.message_type == "Notification" and row["processed"] == 0:
                    messages.append(message)
            rows = []
            for message in messages:
                values, digest = self._values(message)
                kind, message_id, topic, timestamp, body, subject, subscribe_url, token = values
                rows.append((topic, message_id, kind, timestamp, body, subject, subscribe_url, token,
                             digest, datetime.now(timezone.utc).isoformat()))
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                new_rows = []
                for row in rows:
                    existing = connection.execute(
                        "SELECT digest FROM amazon_sns_inbox WHERE topic_arn=? AND message_id=?",
                        (row[0], row[1]),
                    ).fetchone()
                    if existing is not None and existing["digest"] != row[8]:
                        raise ValueError
                    if existing is None:
                        new_rows.append(row)
                count = connection.execute("SELECT COUNT(*) FROM amazon_sns_inbox").fetchone()[0]
                if count + len(new_rows) > self._max_messages:
                    raise ValueError
                connection.executemany("""INSERT INTO amazon_sns_inbox
                    (topic_arn,message_id,message_type,timestamp,message,subject,subscribe_url,token,digest,received_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""", new_rows)
                connection.commit()
            return len(new_rows)
        except Exception:
            raise InboxError("merge_error") from None

    def snapshot(self, destination: Path) -> None:
        """Create a private SQLite backup for a trusted local transport only.

        The snapshot includes subscription tokens and URLs. Never send it through
        Cloudflare, Drive, or another cloud service.
        """
        temporary: Path | None = None
        try:
            target = outside_repository(Path(destination))
            if target.suffix.lower() != ".sqlite" or target.name.lower() == "finance.sqlite":
                raise ValueError
            if target.exists():
                raise FileExistsError
            target.parent.mkdir(parents=True, exist_ok=True)
            # Resolve again after creating the parent so a symlinked destination
            # cannot move the write back inside a repository.
            target = outside_repository(target)
            if target.exists() or not self._database.is_file():
                raise ValueError
            fd, raw_temporary = tempfile.mkstemp(prefix=".amazon-sns-snapshot-", suffix=".sqlite", dir=target.parent)
            os.close(fd)
            temporary = Path(raw_temporary)
            with closing(self._connect()) as source, closing(sqlite3.connect(temporary)) as backup:
                source.backup(backup)
            if os.name == "posix":
                os.chmod(temporary, 0o600)
            # Hard-link creation is atomic and fails if another writer created
            # the target in the meantime; unlike replace(), it never clobbers.
            os.link(temporary, target)
            temporary.unlink()
            temporary = None
        except FileExistsError:
            raise InboxError("snapshot_exists") from None
        except Exception:
            raise InboxError("snapshot_error") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def close(self) -> None:
        """Connections are operation-scoped; there is no persistent handle to close."""
        return None

    def __enter__(self) -> AmazonSnsInbox:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
