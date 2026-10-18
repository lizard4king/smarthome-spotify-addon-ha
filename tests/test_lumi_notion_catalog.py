import json
from uuid import UUID

import pytest

from codex_voice import lumi_notion_catalog as catalog


def page(number=1, title="Example", **extra):
    page_id = str(UUID(int=number))
    return {"object": "page", "id": page_id, "archived": False, "in_trash": False,
            "url": "https://www.notion.so/" + page_id.replace("-", ""),
            "properties": {"Name": {"type": "title", "title": [{"plain_text": title}]}}, **extra}


def batch(pages, cursor=None):
    return {"object": "list", "results": pages, "has_more": cursor is not None, "next_cursor": cursor}


class Response:
    status = 200
    url = catalog.SEARCH_URL

    def __init__(self, document):
        self.raw = json.dumps(document).encode()

    def read(self, size):
        result, self.raw = self.raw[:size], self.raw[size:]
        return result

    def close(self):
        pass


def client(documents):
    calls = []
    def transport(url, headers, body, timeout):
        calls.append((url, headers, json.loads(body), timeout))
        return Response(documents.pop(0))
    return catalog.NotionCatalog("synthetic-token", post_transport=transport), calls


def test_paginated_atomic_catalog_and_unique_aliases(tmp_path):
    obj, calls = client([batch([page(1), page(2, " Other ")], "next"), batch([page(3)])])
    target = tmp_path / "catalog.json"
    assert obj.sync(target) == {"complete": True, "pages": 3}
    result = json.loads(target.read_text(encoding="utf-8"))
    assert result["aliases"] == {"other": str(UUID(int=2))}
    assert "synthetic-token" not in target.read_text()
    assert calls[0][2] == {"filter": {"value": "page", "property": "object"}, "page_size": 100}
    assert calls[1][2]["start_cursor"] == "next"
    assert all(0 < call[3] <= 5 for call in calls)


def test_complete_replacement_removes_revoked_and_archived(tmp_path):
    target = tmp_path / "catalog.json"
    obj, _ = client([batch([page(1), page(2)])])
    obj.sync(target)
    obj, _ = client([batch([page(2, archived=True), page(3, in_trash=True)])])
    obj.sync(target)
    assert json.loads(target.read_text())["pages"] == []


def test_over_limit_preserves_previous_catalog(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog, "MAX_PAGES", 100)
    target = tmp_path / "catalog.json"
    target.write_text("old complete catalog")
    obj, _ = client([batch([page(i) for i in range(1, 101)], "next"), batch([page(101)])])
    with pytest.raises(catalog.CatalogIncomplete):
        obj.sync(target)
    assert target.read_text() == "old complete catalog"


@pytest.mark.parametrize("document", [batch([page(), page()]), batch([page(url="https://example.com")]),
    {"object": "list", "results": [], "has_more": "yes", "next_cursor": None},
    batch([page(properties={})]), batch([page(archived="false")])])
def test_invalid_responses_do_not_replace(tmp_path, document):
    target = tmp_path / "catalog.json"
    target.write_text("previous")
    obj, _ = client([document])
    with pytest.raises(catalog.CatalogError):
        obj.sync(target)
    assert target.read_text() == "previous"


def test_repeated_cursor_rejected():
    obj, _ = client([batch([], "cursor"), batch([], "cursor")])
    with pytest.raises(catalog.CatalogError):
        obj.discover()


@pytest.mark.parametrize("status,url", [(403, catalog.SEARCH_URL), (429, catalog.SEARCH_URL),
                                        (200, "https://example.com")])
def test_rejected_transport(status, url):
    def transport(*args):
        response = Response(batch([]))
        response.status, response.url = status, url
        return response
    with pytest.raises(catalog.CatalogError):
        catalog.NotionCatalog("synthetic", post_transport=transport).discover()


def test_deadline_and_size_limits(monkeypatch):
    ticks = iter([0, 121])
    with pytest.raises(catalog.CatalogError):
        catalog.NotionCatalog("synthetic", monotonic=lambda: next(ticks)).discover()
    monkeypatch.setattr(catalog, "MAX_RESPONSE_BYTES", 10)
    obj, _ = client([batch([])])
    with pytest.raises(catalog.CatalogError):
        obj.discover()


def test_cli_requires_token(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("LUMI_NOTION_READ_TOKEN", raising=False)
    assert catalog.main(["--output", str(tmp_path / "catalog"), "--once"]) == 1
    assert "preserved" in capsys.readouterr().out


def test_load_catalog_returns_ids_and_targets(tmp_path):
    target = tmp_path / "catalog.json"
    obj, _ = client([batch([page()])])
    obj.sync(target)
    assert catalog.load_catalog(target) == {"page_ids": [str(UUID(int=1))], "targets": {"example": str(UUID(int=1))}}
    data = json.loads(target.read_text())
    data["aliases"] = {"injected": str(UUID(int=1))}
    target.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        catalog.load_catalog(target)


def test_load_catalog_rejects_oversize(tmp_path, monkeypatch):
    target = tmp_path / "catalog.json"
    target.write_text(" " * 11)
    monkeypatch.setattr(catalog, "MAX_CATALOG_BYTES", 10)
    with pytest.raises(ValueError):
        catalog.load_catalog(target)


@pytest.mark.parametrize("request_status", [
    {"type": "incomplete", "incomplete_reason": "query_result_limit_reached"},
    None, [], {}, {"type": "unexpected"},
])
def test_explicit_partial_or_invalid_search_preserves_previous_catalog(tmp_path, request_status):
    target = tmp_path / "catalog.json"
    target.write_bytes(b"previous-complete-catalog")
    document = {**batch([page()]), "request_status": request_status}
    obj, calls = client([document])
    with pytest.raises(catalog.CatalogIncomplete):
        obj.sync(target)
    assert target.read_bytes() == b"previous-complete-catalog"
    assert len(calls) == 1


def test_incomplete_later_page_discards_collected_partial_results(tmp_path):
    target = tmp_path / "catalog.json"
    target.write_bytes(b"previous-complete-catalog")
    partial = {**batch([page(2)]), "request_status": {"type": "incomplete"}}
    obj, calls = client([batch([page(1)], "next"), partial])
    with pytest.raises(catalog.CatalogIncomplete):
        obj.sync(target)
    assert target.read_bytes() == b"previous-complete-catalog"
    assert len(calls) == 2


def test_optional_complete_status_accepts_additive_metadata(tmp_path):
    document = {**batch([page()]), "request_status": {"type": "complete", "future_field": True}}
    obj, _ = client([document])
    assert obj.sync(tmp_path / "catalog.json") == {"complete": True, "pages": 1}
