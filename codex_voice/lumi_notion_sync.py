"""Read-only, allowlist-bound Notion sync for the local Lumi knowledge cache."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Callable
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID

try:
    from .lumi_knowledge import KnowledgeStore
except ImportError:  # direct script invocation from codex_voice/
    from lumi_knowledge import KnowledgeStore


API_ROOT = "https://api.notion.com/v1"
NOTION_VERSION = "2025-09-03"
MAX_PAGES = 100
MAX_RESPONSE_BYTES = 1_048_576
MAX_TOTAL_SECONDS = 60.0
REQUEST_TIMEOUT_SECONDS = 5.0
MAX_BLOCKS = 500
MAX_DEPTH = 5
MAX_PAGE_TEXT = 30_000
MAX_CONFIG_BYTES = 32_768
_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\Z")
_PAGE_PATH_RE = re.compile(r"/v1/pages/([0-9a-fA-F-]{36})\Z")
_BLOCK_PATH_RE = re.compile(r"/v1/blocks/([0-9a-fA-F-]{36})/children\Z")

_TEXT_BLOCKS = {
    "paragraph", "heading_1", "heading_2", "heading_3", "bulleted_list_item",
    "numbered_list_item", "to_do", "toggle", "quote", "callout", "code",
}
_CONTAINER_BLOCKS = {"table", "column_list", "column"}
_EMPTY_BLOCKS = {"divider", "breadcrumb", "table_of_contents"}
_UNFOLLOW_BLOCKS = {
    "child_page", "child_database", "image", "video", "file", "pdf", "audio",
}


class NotionSyncError(Exception):
    """A safe, content-free synchronization failure."""


class _PageRevoked(NotionSyncError):
    """The explicitly requested page is no longer accessible."""


class _TransientFailure(NotionSyncError):
    """A temporary API or transport failure; retain cached pages as stale."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _urllib_transport(url: str, headers: dict[str, str], timeout: float):
    request = Request(url, headers=headers, method="GET")
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    try:
        return opener.open(request, timeout=timeout)
    except HTTPError as response:
        return response


def _page_uuid(value: object) -> str:
    if not isinstance(value, str) or not _UUID_RE.fullmatch(value):
        raise ValueError("page_ids must contain UUID strings")
    try:
        return str(UUID(value))
    except ValueError as exc:
        raise ValueError("page_ids must contain UUID strings") from exc


def load_page_ids(path: str | os.PathLike[str] | None) -> list[str]:
    """Read the only accepted configuration shape; no path means no pages."""
    if path is None:
        return []
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_CONFIG_BYTES + 1)
    if len(raw) > MAX_CONFIG_BYTES:
        raise ValueError("pages configuration is too large")
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("pages configuration is invalid") from exc
    if not isinstance(document, dict) or set(document) != {"page_ids"}:
        raise ValueError("pages configuration must contain only page_ids")
    page_ids = document["page_ids"]
    if not isinstance(page_ids, list) or len(page_ids) > MAX_PAGES:
        raise ValueError("page_ids must be a list of at most 100 UUIDs")
    normalized = [_page_uuid(item) for item in page_ids]
    if len(set(normalized)) != len(normalized):
        raise ValueError("page_ids must not contain duplicates")
    return normalized


def _pinned_url(url: str) -> tuple[str, dict[str, str]]:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise NotionSyncError("Notion URL rejected") from exc
    if (parsed.scheme != "https" or parsed.hostname != "api.notion.com"
            or parsed.username is not None or parsed.password is not None
            or port is not None or parsed.fragment):
        raise NotionSyncError("Notion URL rejected")
    match = _PAGE_PATH_RE.fullmatch(parsed.path) or _BLOCK_PATH_RE.fullmatch(parsed.path)
    if match is None:
        raise NotionSyncError("Notion URL rejected")
    try:
        page_id = str(UUID(match.group(1)))
    except ValueError as exc:
        raise NotionSyncError("Notion URL rejected") from exc
    is_block_endpoint = parsed.path.startswith("/v1/blocks/")
    if parsed.query:
        # Pagination is the only permitted query string, assembled internally.
        pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
        query = dict(pairs)
        if (not is_block_endpoint or len(query) != len(pairs)
                or set(query) - {"page_size", "start_cursor"}
                or query.get("page_size") != "100"):
            raise NotionSyncError("Notion URL rejected")
        if "start_cursor" in query and not query["start_cursor"]:
            raise NotionSyncError("Notion URL rejected")
    elif is_block_endpoint:
        raise NotionSyncError("Notion URL rejected")
    return page_id, parsed.path


class NotionSync:
    """Fetch only explicitly listed pages, then atomically replace the cache."""

    def __init__(
        self,
        store: KnowledgeStore,
        token: str,
        *,
        get_transport: Callable[[str, dict[str, str], float], object] = _urllib_transport,
        monotonic: Callable[[], float] = time.monotonic,
        max_pages: int = MAX_PAGES,
        max_total_seconds: float = MAX_TOTAL_SECONDS,
        max_blocks: int = MAX_BLOCKS,
    ):
        if (type(max_pages) is not int or not 1 <= max_pages <= 10000
                or type(max_blocks) is not int or not 1 <= max_blocks <= 100000
                or type(max_total_seconds) not in (float, int)
                or not 0 < max_total_seconds <= 900):
            raise ValueError("invalid Notion sync limits")
        self._max_pages = max_pages
        self._max_seconds = max_total_seconds
        self._max_blocks = max_blocks
        if not isinstance(token, str) or "\r" in token or "\n" in token:
            raise ValueError("Notion token is invalid")
        self._store = store
        self._token = token
        self._transport = get_transport
        self._monotonic = monotonic
        self._deadline = 0.0
        self._block_count = 0

    def sync(self, page_ids: list[str] | tuple[str, ...]) -> dict[str, int]:
        ids = [_page_uuid(item) for item in page_ids]
        if len(ids) > self._max_pages or len(set(ids)) != len(ids):
            raise ValueError("page_ids must be unique and within the configured limit")
        # Revoke removed IDs before any network operation, even on later failure.
        live_ids = set(ids)
        self._store.revoke_except(live_ids)
        if ids and not self._token:
            raise NotionSyncError("Notion token is required")
        self._deadline = self._monotonic() + self._max_seconds
        self._block_count = 0
        pages: list[dict[str, str]] = []
        revoked = 0
        for page_id in ids:
            try:
                page = self._fetch_page(page_id)
            except _PageRevoked:
                live_ids.discard(page_id)
                self._store.revoke_except(live_ids)
                revoked += 1
                continue
            pages.append(page)
        self._store.replace_pages(pages, allowed_ids=ids)
        return {"configured": len(ids), "synced": len(pages), "revoked": revoked}

    def _check_deadline(self) -> float:
        remaining = self._deadline - self._monotonic()
        if remaining <= 0:
            raise _TransientFailure("Notion sync deadline exceeded")
        return remaining

    def _request_json(self, url: str) -> dict[str, object]:
        expected_id, _ = _pinned_url(url)
        remaining = self._check_deadline()
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Notion-Version": NOTION_VERSION,
            "Accept": "application/json",
        }
        response = None
        request_failed = False
        try:
            try:
                response = self._transport(url, headers, min(REQUEST_TIMEOUT_SECONDS, remaining))
            except Exception:
                request_failed = True
            if not request_failed:
                try:
                    status = int(getattr(response, "status", getattr(response, "code", 0)))
                    final_url = (response.geturl() if callable(getattr(response, "geturl", None))
                                 else getattr(response, "url", None))
                    if final_url is not None and final_url != url:
                        raise NotionSyncError("Notion redirect rejected")
                    if status in {403, 404}:
                        raise _PageRevoked("Notion page is inaccessible")
                    if status == 429 or 500 <= status <= 599:
                        raise _TransientFailure("Notion service is temporarily unavailable")
                    if status < 200 or status >= 300:
                        raise NotionSyncError("Notion request was rejected")
                    body = bytearray()
                    while len(body) <= MAX_RESPONSE_BYTES:
                        self._check_deadline()
                        chunk = response.read(min(16_384, MAX_RESPONSE_BYTES + 1 - len(body)))
                        if not chunk:
                            break
                        if not isinstance(chunk, bytes):
                            raise NotionSyncError("Notion response rejected")
                        body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise NotionSyncError("Notion response exceeds the size limit")
                except NotionSyncError:
                    raise
                except Exception:
                    request_failed = True
        finally:
            if response is not None and callable(getattr(response, "close", None)):
                try:
                    response.close()
                except Exception:
                    request_failed = True
        if request_failed:
            raise _TransientFailure("Notion request failed")
        invalid_json = False
        try:
            document = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            invalid_json = True
            document = None
        if invalid_json:
            raise NotionSyncError("Notion response is invalid")
        if not isinstance(document, dict):
            raise NotionSyncError("Notion response is invalid")
        # The URL's UUID is checked against the returned page/block ID by callers.
        document["_requested_id"] = expected_id
        return document

    def _fetch_page(self, page_id: str) -> dict[str, str] | None:
        page = self._request_json(f"{API_ROOT}/pages/{page_id}")
        if page.get("object") != "page" or self._normalize_response_id(page) != page_id:
            raise NotionSyncError("Notion page identity mismatch")
        archived = page.get("archived", False)
        in_trash = page.get("in_trash", False)
        if not isinstance(archived, bool) or not isinstance(in_trash, bool):
            raise NotionSyncError("Notion page metadata is invalid")
        if archived or in_trash:
            raise _PageRevoked("Notion page is no longer active")
        title = self._page_title(page)
        edited_at = page.get("last_edited_time")
        if not isinstance(edited_at, str) or not edited_at:
            raise NotionSyncError("Notion page metadata is invalid")
        text_parts: list[str] = []
        self._page_text_size = 0
        self._walk_children(page_id, depth=1, page_id=page_id, text_parts=text_parts)
        return {
            "page_id": page_id,
            "title": title,
            "url": f"https://www.notion.so/{page_id.replace('-', '')}",
            "text": "\n".join(text_parts),
            "edited_at": edited_at,
        }

    @staticmethod
    def _normalize_response_id(document: dict[str, object]) -> str | None:
        value = document.get("id")
        if not isinstance(value, str):
            return None
        try:
            return str(UUID(value))
        except ValueError:
            return None

    @staticmethod
    def _page_title(page: dict[str, object]) -> str:
        properties = page.get("properties")
        if not isinstance(properties, dict):
            raise NotionSyncError("Notion page title is unavailable")
        for value in properties.values():
            if isinstance(value, dict) and value.get("type") == "title":
                title = value.get("title")
                return NotionSync._rich_text(title)
        raise NotionSyncError("Notion page title is unavailable")

    @staticmethod
    def _rich_text(value: object) -> str:
        if not isinstance(value, list):
            raise NotionSyncError("Notion rich text is invalid")
        parts: list[str] = []
        for item in value:
            if not isinstance(item, dict) or not isinstance(item.get("plain_text"), str):
                raise NotionSyncError("Notion rich text is invalid")
            parts.append(item["plain_text"])
        return "".join(parts)

    def _append_text(self, value: str, output: list[str]) -> None:
        addition = len(value) + (1 if value and output else 0)
        if self._page_text_size + addition > MAX_PAGE_TEXT:
            raise NotionSyncError("Notion page text exceeds the size limit")
        if value:
            output.append(value)
            self._page_text_size += addition

    def _walk_children(
        self, block_id: str, *, depth: int, page_id: str, text_parts: list[str]
    ) -> None:
        if depth > MAX_DEPTH:
            raise NotionSyncError("Notion block depth exceeds the limit")
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            query = {"page_size": "100"}
            if cursor is not None:
                query["start_cursor"] = cursor
            url = f"{API_ROOT}/blocks/{block_id}/children?{urlencode(query)}"
            response = self._request_json(url)
            if response.get("object") != "list" or response.get("_requested_id") != block_id:
                raise NotionSyncError("Notion block response identity mismatch")
            results = response.get("results")
            has_more = response.get("has_more")
            if not isinstance(results, list) or not isinstance(has_more, bool):
                raise NotionSyncError("Notion block response is invalid")
            for block in results:
                self._block_count += 1
                if self._block_count > self._max_blocks:
                    raise NotionSyncError("Notion block count exceeds the limit")
                expected_parent_type = "page_id" if depth == 1 else "block_id"
                self._validate_parent(block, expected_parent_id=block_id,
                                      expected_parent_type=expected_parent_type)
                self._consume_block(block, depth=depth, page_id=page_id, text_parts=text_parts)
            if not has_more:
                return
            next_cursor = response.get("next_cursor")
            if (not isinstance(next_cursor, str) or not next_cursor
                    or len(next_cursor) > 2_048
                    or next_cursor in seen_cursors or next_cursor == cursor):
                raise NotionSyncError("Notion pagination is invalid")
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    @staticmethod
    def _validate_parent(
        block: object, *, expected_parent_id: str, expected_parent_type: str
    ) -> None:
        if not isinstance(block, dict):
            raise NotionSyncError("Notion block is invalid")
        parent = block.get("parent")
        if not isinstance(parent, dict):
            raise NotionSyncError("Notion block parent is invalid")
        parent_type = parent.get("type")
        parent_key = "page_id" if expected_parent_type == "page_id" else "block_id"
        if parent_type != expected_parent_type:
            raise NotionSyncError("Notion block parent is invalid")
        if parent.get(parent_key) != expected_parent_id:
            raise NotionSyncError("Notion block parent mismatch")

    def _consume_block(
        self, block: object, *, depth: int, page_id: str, text_parts: list[str]
    ) -> None:
        if not isinstance(block, dict):
            raise NotionSyncError("Notion block is invalid")
        block_id = self._normalize_response_id(block)
        block_type = block.get("type")
        has_children = block.get("has_children")
        if block_id is None or not isinstance(block_type, str) or not isinstance(has_children, bool):
            raise NotionSyncError("Notion block metadata is invalid")
        content = block.get(block_type)
        if not isinstance(content, dict):
            raise NotionSyncError("Notion block content is invalid")

        if block_type in _UNFOLLOW_BLOCKS or block_type in {"child_page", "child_database"}:
            return
        if block_type == "synced_block":
            if "synced_from" not in content:
                raise NotionSyncError("Notion synced block metadata is invalid")
            synced_from = content.get("synced_from")
            if synced_from is not None:
                return
        elif block_type in _TEXT_BLOCKS:
            self._append_text(self._rich_text(content.get("rich_text")), text_parts)
        elif block_type == "table_row":
            cells = content.get("cells")
            if not isinstance(cells, list):
                raise NotionSyncError("Notion table row is invalid")
            cell_texts = [self._rich_text(cell) for cell in cells]
            self._append_text(" | ".join(cell_texts), text_parts)
        elif block_type in _EMPTY_BLOCKS or block_type in _CONTAINER_BLOCKS or block_type == "synced_block":
            pass
        else:
            raise NotionSyncError("Notion block type is unsupported")

        if has_children:
            if block_type in _UNFOLLOW_BLOCKS or block_type in {"child_page", "child_database"}:
                return
            if block_type == "synced_block" and content.get("synced_from") is not None:
                return
            if block_type not in _TEXT_BLOCKS | _CONTAINER_BLOCKS | {"synced_block"}:
                raise NotionSyncError("Notion nested block type is unsupported")
            if depth >= MAX_DEPTH:
                raise NotionSyncError("Notion block depth exceeds the limit")
            self._walk_children(block_id, depth=depth + 1, page_id=page_id, text_parts=text_parts)


def sync_once(
    database: str | os.PathLike[str],
    pages_config: str | os.PathLike[str] | None,
    *,
    token: str | None = None,
    store_factory: Callable[[str | os.PathLike[str]], KnowledgeStore] = KnowledgeStore,
    get_transport: Callable[[str, dict[str, str], float], object] = _urllib_transport,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, int]:
    page_ids = load_page_ids(pages_config)
    if token is None:
        token = os.environ.get("LUMI_NOTION_READ_TOKEN", "")
    store = store_factory(database)
    return NotionSync(store, token, get_transport=get_transport, monotonic=monotonic).sync(page_ids)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pages-config", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--once", action="store_true", required=True)
    args = parser.parse_args(argv)
    try:
        counts = sync_once(args.database, args.pages_config)
    except Exception:
        print("Notion sync failed; cached pages were retained where still authorized.", file=sys.stderr)
        return 1
    print(json.dumps({"status": "ok", **counts}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
