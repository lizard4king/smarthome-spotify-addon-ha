"""Local, resumable state for the two read-only Amazon portability queries.

The caller verifies SNS signatures and supplies a trusted record loader. Neither
credentials nor provider URLs, page tokens or source payloads are persisted.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Callable

from .amazon_portability_api import (
    AmazonPortabilityApi, QueryRecord, parse_response_notification,
)
from .amazon_portability_import import import_amazon_portability
from .amazon_sns import VerifiedSnsMessage, verify_sns_message
from .connectors.amazon_portability import parse_amazon_portability

ORDERS = "portability-physical-orders"
RETURNS = "portability-physical-order-returns"
SCOPES = (ORDERS, RETURNS)
MAX_PAGES = 20
MAX_RECORDS = 1000
MAX_PAYLOAD_BYTES = 16 * 1024 * 1024


class AmazonPortabilitySyncError(RuntimeError):
    """Stable error code; never includes source data or credentials."""


def _alias(value: str) -> str:
    if type(value) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value) is None:
        raise AmazonPortabilitySyncError("invalid_profile_alias")
    return value


def _json_array(raw: bytes) -> bytes:
    if type(raw) is not bytes or len(raw) > MAX_PAYLOAD_BYTES:
        raise AmazonPortabilitySyncError("invalid_record_payload")
    try:
        text = raw.decode("utf-8-sig")
        value = json.loads(text, parse_float=Decimal)
    except (UnicodeError, ValueError, TypeError):
        raise AmazonPortabilitySyncError("invalid_record_payload") from None
    if type(value) is not list:
        raise AmazonPortabilitySyncError("invalid_record_payload")
    # Reuse the source spelling of decimal numbers; decoding and re-encoding via
    # binary float would change financial amounts.
    inner = text.strip()[1:-1].strip()
    return inner.encode("utf-8")


class AmazonPortabilitySync:
    def __init__(self, store, api: AmazonPortabilityApi) -> None:
        self.store = store
        self.api = api

    def status(self, profile_alias: str) -> dict[str, tuple[str, str]]:
        alias = _alias(profile_alias)
        rows = self.store.db.execute(
            "SELECT scope_id,query_id,status FROM amazon_portability_queries WHERE profile_alias=?",
            (alias,),
        )
        return {row["scope_id"]: (row["query_id"], row["status"]) for row in rows}

    def start_sync(self, profile_alias: str, *, access_token: str) -> dict[str, tuple[str, str]]:
        alias = _alias(profile_alias)
        state = self.status(alias)
        if state and (
            all(status == "IMPORTED" for _, status in state.values())
            or any(status == "CANCELED" for _, status in state.values())
        ):
            # Query IDs are short-lived operational state. Imported source
            # documents keep their own immutable provenance and external IDs.
            with self.store.db:
                self.store.db.execute(
                    "DELETE FROM amazon_portability_queries WHERE profile_alias=?", (alias,)
                )
            state = {}
        elif any(status == "IMPORTED" for _, status in state.values()):
            raise AmazonPortabilitySyncError("inconsistent_query_state")
        for scope_id in SCOPES:
            if scope_id in state:
                continue
            try:
                query_id = self.api.create_query(scope_id, access_token=access_token)
            except Exception:
                failed = True
            else:
                failed = False
            if failed:
                raise AmazonPortabilitySyncError("query_create_failed")
            # Commit after each successful create: a later failure cannot erase it.
            with self.store.db:
                self.store.db.execute(
                    "INSERT INTO amazon_portability_queries VALUES (?,?,?,?)",
                    (alias, scope_id, query_id, "PENDING"),
                )
            state[scope_id] = (query_id, "PENDING")
        return state

    def record_notification(
        self, payload: bytes, *, expected_topic_arn: str,
        datetime_now: datetime | None = None,
    ) -> tuple[str, str]:
        try:
            envelope = verify_sns_message(
                payload, expected_topic_arn=expected_topic_arn,
                now=datetime_now,
            )
            if envelope.message_type != "Notification":
                raise AmazonPortabilitySyncError("notification_not_verified")
        except AmazonPortabilitySyncError:
            raise
        except Exception:
            failed = True
        else:
            failed = False
        if failed:
            raise AmazonPortabilitySyncError("invalid_notification")
        return self._record_verified_notification(envelope)

    def _record_verified_notification(self, envelope: VerifiedSnsMessage) -> tuple[str, str]:
        """Apply already authenticated local inbox evidence, without a second age check.

        This private entry point is for the separate receiver inbox only. Public
        request bodies must always enter through SNS verification first.
        """
        if not isinstance(envelope, VerifiedSnsMessage) or envelope.message_type != "Notification":
            raise AmazonPortabilitySyncError("notification_not_verified")
        try:
            inner = json.dumps({
                "Type": envelope.message_type,
                "Subject": envelope.subject,
                "Message": envelope.message,
            }).encode()
            notification = parse_response_notification(inner)
        except (TypeError, ValueError, RuntimeError):
            raise AmazonPortabilitySyncError("invalid_notification") from None
        row = self.store.db.execute(
            "SELECT status FROM amazon_portability_queries WHERE query_id=?",
            (notification.query_id,),
        ).fetchone()
        if row is None:
            raise AmazonPortabilitySyncError("unknown_query_id")
        old = row["status"]
        new = notification.status
        if old == new or (old == "IMPORTED" and new == "COMPLETED"):
            return notification.query_id, old
        if old != "PENDING":
            raise AmazonPortabilitySyncError("conflicting_notification")
        with self.store.db:
            changed = self.store.db.execute(
                "UPDATE amazon_portability_queries SET status=? WHERE query_id=? AND status='PENDING'",
                (new, notification.query_id),
            )
            if changed.rowcount != 1:
                current = self.store.db.execute(
                    "SELECT status FROM amazon_portability_queries WHERE query_id=?",
                    (notification.query_id,),
                ).fetchone()
                if current is None or current["status"] != new:
                    raise AmazonPortabilitySyncError("conflicting_notification")
        return notification.query_id, new

    def _scope_payload(
        self, scope_id: str, query_id: str, access_token: str,
        record_loader: Callable[[str, QueryRecord], bytes],
    ) -> bytes:
        chunks: list[bytes] = []
        next_token = None
        seen_tokens: set[str] = set()
        count = 0
        total_bytes = 0
        for _ in range(MAX_PAGES):
            remaining = MAX_RECORDS - count
            page = self.api.list_query_records(
                scope_id, query_id, access_token=access_token,
                max_results=max(1, min(250, remaining)), next_page_token=next_token,
            )
            if remaining == 0 and page.records:
                raise AmazonPortabilitySyncError("record_limit_exceeded")
            count += len(page.records)
            if count > MAX_RECORDS:
                raise AmazonPortabilitySyncError("record_limit_exceeded")
            for record in page.records:
                try:
                    raw = record_loader(scope_id, record)
                except Exception:
                    failed = True
                else:
                    failed = False
                if failed:
                    raise AmazonPortabilitySyncError("record_load_failed")
                if type(raw) is not bytes:
                    raise AmazonPortabilitySyncError("invalid_record_payload")
                total_bytes += len(raw)
                if total_bytes > MAX_PAYLOAD_BYTES:
                    raise AmazonPortabilitySyncError("payload_limit_exceeded")
                inner = _json_array(raw)
                if inner:
                    chunks.append(inner)
            next_token = page.next_page_token
            if next_token is None:
                return b"[" + b",".join(chunks) + b"]"
            if next_token in seen_tokens:
                raise AmazonPortabilitySyncError("repeated_page_token")
            seen_tokens.add(next_token)
        raise AmazonPortabilitySyncError("page_limit_exceeded")

    def complete_sync(
        self, profile_alias: str, *, access_token: str,
        record_loader: Callable[[str, QueryRecord], bytes],
        retrieved_at: datetime | None = None,
    ):
        alias = _alias(profile_alias)
        state = self.status(alias)
        if any(scope not in state for scope in SCOPES):
            raise AmazonPortabilitySyncError("queries_incomplete")
        if any(state[scope][1] == "CANCELED" for scope in SCOPES):
            raise AmazonPortabilitySyncError("query_canceled")
        if all(state[scope][1] == "IMPORTED" for scope in SCOPES):
            return None
        if any(state[scope][1] != "COMPLETED" for scope in SCOPES):
            raise AmazonPortabilitySyncError("queries_incomplete")
        try:
            orders = self._scope_payload(ORDERS, state[ORDERS][0], access_token, record_loader)
            returns = self._scope_payload(RETURNS, state[RETURNS][0], access_token, record_loader)
            batch = parse_amazon_portability(
                orders, returns, retrieved_at=retrieved_at or datetime.now(UTC),
            )
            result = import_amazon_portability(self.store, batch)
        except AmazonPortabilitySyncError:
            raise
        except Exception:
            failed = True
        else:
            failed = False
        if failed:
            raise AmazonPortabilitySyncError("sync_import_failed")
        with self.store.db:
            self.store.db.execute(
                "UPDATE amazon_portability_queries SET status='IMPORTED' "
                "WHERE profile_alias=? AND status='COMPLETED'", (alias,),
            )
        return result
