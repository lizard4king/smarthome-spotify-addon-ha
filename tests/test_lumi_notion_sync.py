from __future__ import annotations

import json
from pathlib import Path
import time
from urllib.parse import urlencode

import pytest

from codex_voice.lumi_knowledge import KnowledgeStore
from codex_voice import lumi_notion_sync as notion


PAGE_A = "11111111-1111-4111-8111-111111111111"
PAGE_B = "22222222-2222-4222-8222-222222222222"
BLOCK_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
BLOCK_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
TOKEN = "synthetic-notion-token-must-never-leak"


class FakeResponse:
    def __init__(self, body: object, *, status: int = 200, url: str | None = None):
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.status = status
        self.url = url
        self.offset = 0

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self.body) - self.offset
        chunk = self.body[self.offset:self.offset + size]
        self.offset += len(chunk)
        return chunk

    def close(self) -> None:
        pass


class FakeTransport:
    def __init__(self, routes: dict[str, FakeResponse | BaseException] | None = None):
        self.routes = routes or {}
        self.calls: list[tuple[str, dict[str, str], float]] = []

    def __call__(self, url: str, headers: dict[str, str], timeout: float) -> FakeResponse:
        self.calls.append((url, headers, timeout))
        if url not in self.routes:
            raise AssertionError(f"unexpected HTTP request: {url}")
        result = self.routes[url]
        if isinstance(result, BaseException):
            raise result
        return result


class RecordingStore:
    def __init__(self):
        self.revoke_calls: list[set[str]] = []
        self.replace_calls: list[tuple[list[dict[str, str]], list[str]]] = []

    def revoke_except(self, allowed_ids):
        self.revoke_calls.append(set(allowed_ids))

    def replace_pages(self, pages, allowed_ids):
        self.replace_calls.append((list(pages), list(allowed_ids)))


def page_document(page_id: str = PAGE_A, *, archived: bool = False, in_trash: bool = False):
    return {
        "object": "page", "id": page_id, "archived": archived, "in_trash": in_trash,
        "last_edited_time": "2026-10-11T10:00:00Z",
        "properties": {"title": {"type": "title", "title": [{"plain_text": "Licht"}]}},
    }


def block(block_id: str, block_type: str, parent_id: str, *, parent_type: str = "page_id",
          rich_text: str | None = None, has_children: bool = False, extra: dict | None = None):
    payload = {"object": "block", "id": block_id, "type": block_type,
               "has_children": has_children, "parent": {"type": parent_type,
               f"{parent_type}": parent_id}}
    content = dict(extra or {})
    if rich_text is not None:
        content["rich_text"] = [{"plain_text": rich_text}]
    payload[block_type] = content
    return payload


def children_doc(parent_id: str, blocks: list[dict], *, has_more: bool = False,
                 next_cursor: str | None = None):
    return {"object": "list", "results": blocks, "has_more": has_more,
            "next_cursor": next_cursor}


def page_url(page_id: str) -> str:
    return f"{notion.API_ROOT}/pages/{page_id}"


def children_url(block_id: str, cursor: str | None = None) -> str:
    query = {"page_size": "100"}
    if cursor:
        query["start_cursor"] = cursor
    return f"{notion.API_ROOT}/blocks/{block_id}/children?{urlencode(query)}"


def test_allowlisted_page_fetches_paginated_nested_text_and_commits_snapshot():
    root_blocks = [block(BLOCK_A, "paragraph", PAGE_A, rich_text="Ebene eins", has_children=True)]
    second_blocks = [block(BLOCK_B, "heading_2", PAGE_A, rich_text="Zweiter Teil")]
    nested_blocks = [block(PAGE_B, "to_do", BLOCK_A, parent_type="block_id", rich_text="Unterpunkt")]
    transport = FakeTransport({
        page_url(PAGE_A): FakeResponse(page_document()),
        children_url(PAGE_A): FakeResponse(children_doc(PAGE_A, root_blocks, has_more=True,
                                                        next_cursor="cursor-2")),
        children_url(PAGE_A, "cursor-2"): FakeResponse(children_doc(PAGE_A, second_blocks)),
        children_url(BLOCK_A): FakeResponse(children_doc(BLOCK_A, nested_blocks)),
    })
    store = RecordingStore()

    result = notion.NotionSync(store, TOKEN, get_transport=transport).sync([PAGE_A])

    assert result == {"configured": 1, "synced": 1, "revoked": 0}
    assert store.replace_calls[0][0] == [{
        "page_id": PAGE_A, "title": "Licht", "url": f"https://www.notion.so/{PAGE_A.replace('-', '')}",
        "text": "Ebene eins\nUnterpunkt\nZweiter Teil", "edited_at": "2026-10-11T10:00:00Z",
    }]
    assert len(transport.calls) == 4
    for url, headers, timeout in transport.calls:
        assert url.startswith("https://api.notion.com/v1/")
        assert headers["Authorization"] == f"Bearer {TOKEN}"
        assert headers["Notion-Version"] == "2025-09-03"
        assert timeout <= 5.0


def test_child_pages_databases_foreign_synced_blocks_and_attachments_are_not_followed():
    blocks = [
        block(BLOCK_A, "child_page", PAGE_A, has_children=True, extra={"title": "ignored"}),
        block(BLOCK_B, "child_database", PAGE_A, has_children=True, extra={"title": "ignored"}),
        block("cccccccc-cccc-4ccc-8ccc-cccccccccccc", "synced_block", PAGE_A,
              has_children=True, extra={"synced_from": {"type": "block_id", "block_id": PAGE_B}}),
        block("dddddddd-dddd-4ddd-8ddd-dddddddddddd", "file", PAGE_A,
              has_children=True, extra={"file": {"url": "https://example.invalid/private"}}),
    ]
    transport = FakeTransport({
        page_url(PAGE_A): FakeResponse(page_document()),
        children_url(PAGE_A): FakeResponse(children_doc(PAGE_A, blocks)),
    })
    store = RecordingStore()

    notion.NotionSync(store, TOKEN, get_transport=transport).sync([PAGE_A])

    assert len(transport.calls) == 2
    assert all(BLOCK_A not in url and BLOCK_B not in url for url, _, _ in transport.calls)
    assert store.replace_calls[0][0][0]["text"] == ""


def test_original_synced_block_and_nested_columns_are_read_only_and_supported():
    synced_id = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
    transport = FakeTransport({
        page_url(PAGE_A): FakeResponse(page_document()),
        children_url(PAGE_A): FakeResponse(children_doc(PAGE_A, [
            block(synced_id, "synced_block", PAGE_A, has_children=True,
                  extra={"synced_from": None}),
        ])),
        children_url(synced_id): FakeResponse(children_doc(synced_id, [
            block(BLOCK_A, "paragraph", synced_id, parent_type="block_id", rich_text="Local source"),
        ])),
    })
    store = RecordingStore()

    notion.NotionSync(store, TOKEN, get_transport=transport).sync([PAGE_A])

    assert store.replace_calls[0][0][0]["text"] == "Local source"
    assert len(transport.calls) == 3


def test_synced_block_without_origin_metadata_fails_closed():
    transport = FakeTransport({
        page_url(PAGE_A): FakeResponse(page_document()),
        children_url(PAGE_A): FakeResponse(children_doc(PAGE_A, [
            block(BLOCK_A, "synced_block", PAGE_A, has_children=True),
        ])),
    })
    store = RecordingStore()

    with pytest.raises(notion.NotionSyncError, match="synced block metadata"):
        notion.NotionSync(store, TOKEN, get_transport=transport).sync([PAGE_A])

    assert store.replace_calls == []


def test_unsupported_block_and_parent_mismatch_abort_without_replacing():
    unsupported = block(BLOCK_A, "equation", PAGE_A, extra={"expression": "x"})
    transport = FakeTransport({
        page_url(PAGE_A): FakeResponse(page_document()),
        children_url(PAGE_A): FakeResponse(children_doc(PAGE_A, [unsupported])),
    })
    store = RecordingStore()
    with pytest.raises(notion.NotionSyncError):
        notion.NotionSync(store, TOKEN, get_transport=transport).sync([PAGE_A])
    assert store.replace_calls == []

    wrong_parent = block(BLOCK_A, "paragraph", PAGE_B, rich_text="cross-page")
    transport = FakeTransport({
        page_url(PAGE_A): FakeResponse(page_document()),
        children_url(PAGE_A): FakeResponse(children_doc(PAGE_A, [wrong_parent])),
    })
    store = RecordingStore()
    with pytest.raises(notion.NotionSyncError, match="parent mismatch"):
        notion.NotionSync(store, TOKEN, get_transport=transport).sync([PAGE_A])
    assert store.replace_calls == []


def test_archived_and_revoked_pages_are_removed_immediately():
    transport = FakeTransport({
        page_url(PAGE_A): FakeResponse(page_document(archived=True)),
        page_url(PAGE_B): FakeResponse({}, status=404),
    })
    store = RecordingStore()

    result = notion.NotionSync(store, TOKEN, get_transport=transport).sync([PAGE_A, PAGE_B])

    assert result == {"configured": 2, "synced": 0, "revoked": 2}
    assert store.revoke_calls == [{PAGE_A, PAGE_B}, {PAGE_B}, set()]
    assert store.replace_calls == [([], [PAGE_A, PAGE_B])]


def test_transient_failure_keeps_previous_snapshot_and_it_becomes_stale(tmp_path: Path):
    now = [1_800_000_000.0]
    store = KnowledgeStore(tmp_path / "knowledge.sqlite3", clock=lambda: now[0])
    store.replace_pages([{
        "page_id": PAGE_A, "title": "Licht", "url": f"https://www.notion.so/{PAGE_A}",
        "text": "cached room light", "edited_at": "2026-10-11T10:00:00Z",
    }], [PAGE_A])
    transport = FakeTransport({page_url(PAGE_A): FakeResponse({}, status=503)})

    with pytest.raises(notion._TransientFailure):
        notion.NotionSync(store, TOKEN, get_transport=transport).sync([PAGE_A])
    now[0] += 24 * 60 * 60 + 1
    results = store.search("room")
    assert len(results) == 1
    assert results[0]["stale"] is True


def test_error_after_revocation_keeps_other_cached_pages(caplog):
    transport = FakeTransport({
        page_url(PAGE_A): FakeResponse({}, status=403),
        page_url(PAGE_B): OSError(f"transport text contains {TOKEN}"),
    })
    store = RecordingStore()

    with pytest.raises(notion._TransientFailure) as caught:
        notion.NotionSync(store, TOKEN, get_transport=transport).sync([PAGE_A, PAGE_B])

    assert str(caught.value) == "Notion request failed"
    assert caught.value.__context__ is None
    assert TOKEN not in caplog.text
    assert store.revoke_calls == [{PAGE_A, PAGE_B}, {PAGE_B}]
    assert store.replace_calls == []


def test_returned_page_id_mismatch_aborts_without_replacement():
    transport = FakeTransport({page_url(PAGE_A): FakeResponse(page_document(PAGE_B))})
    store = RecordingStore()

    with pytest.raises(notion.NotionSyncError, match="identity mismatch"):
        notion.NotionSync(store, TOKEN, get_transport=transport).sync([PAGE_A])

    assert store.replace_calls == []


@pytest.mark.parametrize("failure", ["oversize", "depth", "blocks", "text", "pagination"])
def test_oversized_or_truncated_snapshot_fails_closed(failure: str):
    transport = FakeTransport({page_url(PAGE_A): FakeResponse(page_document())})
    if failure == "oversize":
        transport.routes[page_url(PAGE_A)] = FakeResponse(b" " * (notion.MAX_RESPONSE_BYTES + 1))
    elif failure == "depth":
        parent_id = PAGE_A
        for depth in range(1, notion.MAX_DEPTH + 1):
            block_id = f"{depth:08x}-0000-4000-8000-000000000000"
            route = children_url(parent_id)
            transport.routes[route] = FakeResponse(children_doc(parent_id, [
                block(block_id, "column", parent_id,
                      parent_type="page_id" if depth == 1 else "block_id",
                      has_children=True),
            ]))
            parent_id = block_id
    elif failure == "blocks":
        blocks = [block(f"{index:08x}-0000-4000-8000-000000000000", "divider", PAGE_A)
                  for index in range(notion.MAX_BLOCKS + 1)]
        transport.routes[children_url(PAGE_A)] = FakeResponse(children_doc(PAGE_A, blocks))
    elif failure == "pagination":
        transport.routes[children_url(PAGE_A)] = FakeResponse(children_doc(
            PAGE_A, [], has_more=True, next_cursor="repeated-cursor"))
        transport.routes[children_url(PAGE_A, "repeated-cursor")] = FakeResponse(children_doc(
            PAGE_A, [], has_more=True, next_cursor="repeated-cursor"))
    else:
        too_much = "x" * (notion.MAX_PAGE_TEXT + 1)
        transport.routes[children_url(PAGE_A)] = FakeResponse(children_doc(PAGE_A, [
            block(BLOCK_A, "paragraph", PAGE_A, rich_text=too_much),
        ]))
    if failure in {"depth", "blocks", "text"}:
        transport.routes.setdefault(children_url(PAGE_A), FakeResponse(children_doc(PAGE_A, [])))

    store = RecordingStore()
    with pytest.raises(notion.NotionSyncError):
        notion.NotionSync(store, TOKEN, get_transport=transport).sync([PAGE_A])
    assert store.replace_calls == []


def test_host_pinning_redirects_and_token_redaction():
    transport = FakeTransport()
    client = notion.NotionSync(RecordingStore(), TOKEN, get_transport=transport)
    client._deadline = time.monotonic() + 60
    with pytest.raises(notion.NotionSyncError, match="URL rejected"):
        client._request_json("https://evil.example/v1/pages/" + PAGE_A)
    assert transport.calls == []

    url = page_url(PAGE_A)
    transport.routes[url] = FakeResponse({}, status=302, url="https://evil.example/redirect")
    with pytest.raises(notion.NotionSyncError, match="redirect rejected") as caught:
        client._request_json(url)
    assert TOKEN not in str(caught.value)
    assert len(transport.calls) == 1
    assert TOKEN not in repr(transport.calls[0][0])


def test_headers_are_pinned_and_configuration_defaults_to_no_pages(tmp_path: Path):
    config = tmp_path / "pages.json"
    config.write_text(json.dumps({"page_ids": [PAGE_A]}), encoding="utf-8")
    assert notion.load_page_ids(None) == []
    assert notion.load_page_ids(config) == [PAGE_A]
    config.write_text(json.dumps({"page_ids": [PAGE_A, PAGE_A]}), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicates"):
        notion.load_page_ids(config)

    transport = FakeTransport({page_url(PAGE_A): FakeResponse(page_document()),
                               children_url(PAGE_A): FakeResponse(children_doc(PAGE_A, []))})
    store = RecordingStore()
    result = notion.NotionSync(store, TOKEN, get_transport=transport).sync([PAGE_A])
    assert result["synced"] == 1
    assert all(call[1]["Notion-Version"] == notion.NOTION_VERSION for call in transport.calls)
    assert all(url.startswith("https://api.notion.com/v1/") for url, _, _ in transport.calls)


def test_empty_allowlist_clears_cache_without_a_token_or_request():
    store = RecordingStore()
    transport = FakeTransport()
    result = notion.NotionSync(store, "", get_transport=transport).sync([])

    assert result == {"configured": 0, "synced": 0, "revoked": 0}
    assert store.revoke_calls == [set()]
    assert store.replace_calls == [([], [])]
    assert transport.calls == []


def test_cli_outputs_only_counts_or_generic_errors(monkeypatch, capsys):
    monkeypatch.setenv("LUMI_NOTION_READ_TOKEN", TOKEN)
    monkeypatch.setattr(notion, "sync_once", lambda *_args: {
        "configured": 1, "synced": 1, "revoked": 0,
    })
    args = ["--database", "unused.db", "--pages-config", "selected-pages.json", "--once"]
    assert notion.main(args) == 0
    output = capsys.readouterr()
    assert output.out == '{"configured": 1, "revoked": 0, "status": "ok", "synced": 1}\n'
    assert TOKEN not in output.out + output.err
    assert PAGE_A not in output.out + output.err

    monkeypatch.setattr(notion, "sync_once", lambda *_args: (_ for _ in ()).throw(
        RuntimeError(f"private body and {TOKEN}")))
    assert notion.main(args) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "Notion sync failed; cached pages were retained where still authorized.\n"
    assert TOKEN not in output.err


def test_cli_missing_page_selection_does_not_touch_existing_cache(monkeypatch, capsys):
    def forbidden_sync(*_args, **_kwargs):
        pytest.fail("missing page selection must not open or revoke the cache")

    monkeypatch.setattr(notion, "sync_once", forbidden_sync)
    with pytest.raises(SystemExit) as caught:
        notion.main(["--database", "existing-cache.db", "--once"])
    assert caught.value.code == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "--pages-config" in output.err
