from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path

import pytest

from codex_voice.lumi_knowledge import KnowledgeStore, MAX_EXCERPT


PAGE_A = "11111111-1111-4111-8111-111111111111"
PAGE_B = "22222222-2222-4222-8222-222222222222"
PAGE_C = "33333333-3333-4333-8333-333333333333"


def page(page_id=PAGE_A, *, title="Licht im Wohnzimmer", text="Die Lampe ist eingeschaltet.", edited_at="2026-10-10T12:00:00Z", url=None):
    return {"page_id": page_id, "title": title,
            "url": url or f"https://www.notion.so/Haus/Licht-{page_id.replace('-', '')}",
            "text": text, "edited_at": edited_at}


def test_oversized_timestamp_rejected_before_datetime_parsing():
    record = page(edited_at="2026-10-10T12:00:00." + "0" * 100_000 + "Z")
    with pytest.raises(ValueError, match="edited_at"):
        KnowledgeStore._validate_pages([record], [PAGE_A])


def test_edited_timestamp_counts_towards_record_byte_limit():
    record = page()
    without_time = sum(len(record[key].encode("utf-8")) for key in ("title", "text", "url"))
    with pytest.raises(ValueError, match="byte limit"):
        KnowledgeStore._validate_pages([record], [PAGE_A], max_total_bytes=without_time)
    actual = without_time + len(record["edited_at"].encode("utf-8"))
    assert KnowledgeStore._validate_pages([record], [PAGE_A], max_total_bytes=actual)


def test_restart_query_ranking_and_excerpt(tmp_path):
    now = [1_800_000_000.0]
    db = tmp_path / "private" / "lumi.sqlite3"
    KnowledgeStore(db, clock=lambda: now[0]).replace_pages(
        [page(PAGE_A), page(PAGE_B, title="Lampe Küche", text="x" * 4_000 + " Küche Lampe " + "y" * 4_000)],
        allowed_ids=[PAGE_A, PAGE_B],
    )
    restarted = KnowledgeStore(db, clock=lambda: now[0])
    results = restarted.search("LAMPE küche", limit=2)
    assert [item["page_id"] for item in results] == [PAGE_B, PAGE_A]
    assert set(results[0]) == {"page_id", "title", "url", "text", "synced_at", "edited_at", "stale"}
    assert results[0]["synced_at"] == now[0]
    assert results[0]["stale"] is False
    assert len(results[0]["text"]) <= MAX_EXCERPT
    assert "Lampe" in results[0]["text"]
    assert "Küche" in results[0]["text"]


def test_explicit_scope_and_full_replacement(tmp_path):
    store = KnowledgeStore(tmp_path / "cache.db")
    with pytest.raises(ValueError):
        store.replace_pages([page(PAGE_A), page(PAGE_B)], allowed_ids=[PAGE_A])
    assert store.search("lampe") == []
    store.replace_pages([page(PAGE_A), page(PAGE_B)], allowed_ids=[PAGE_A, PAGE_B])
    store.replace_pages([page(PAGE_A, text="Nur die freigegebene Seite")], allowed_ids=[PAGE_A])
    assert [item["page_id"] for item in store.search("freigegebene")] == [PAGE_A]
    assert store.search("küche") == []


def test_revocation_deletes_rows_and_empty_allowlist(tmp_path):
    store = KnowledgeStore(tmp_path / "cache.db")
    store.replace_pages([page(PAGE_A), page(PAGE_B)], [PAGE_A, PAGE_B])
    store.revoke_except([PAGE_B])
    assert store.search("lampe", limit=10)[0]["page_id"] == PAGE_B
    store.revoke_except([])
    assert store.search("lampe") == []
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM pages").fetchone()[0] == 0


def test_validation_failure_is_atomic(tmp_path):
    store = KnowledgeStore(tmp_path / "cache.db", clock=lambda: 123.0)
    store.replace_pages([page(PAGE_A, title="Vorher")], [PAGE_A])
    with pytest.raises(ValueError):
        store.replace_pages([page(PAGE_B), page(PAGE_C, url="http://evil.test/" + PAGE_C)],
                            [PAGE_B, PAGE_C])
    found = store.search("vorher")
    assert [item["title"] for item in found] == ["Vorher"]
    assert store.search("küche") == []


@pytest.mark.parametrize("bad", [
    {"page_id": PAGE_A, "title": "x" * 201, "url": f"https://notion.so/{PAGE_A}", "text": "x", "edited_at": "2026-10-10T00:00:00Z"},
    {"page_id": PAGE_A, "title": "x", "url": f"https://evil.test/{PAGE_A}", "text": "x", "edited_at": "2026-10-10T00:00:00Z"},
    {"page_id": PAGE_A, "title": "x", "url": "https://notion.so/unpassend", "text": "x", "edited_at": "2026-10-10T00:00:00Z"},
    {"page_id": PAGE_A, "title": "x", "url": f"https://notion.so/{PAGE_A}", "text": "x", "edited_at": "2026-10-10T00:00:00"},
])
def test_record_bounds_and_shape(tmp_path, bad):
    store = KnowledgeStore(tmp_path / "cache.db")
    with pytest.raises(ValueError):
        store.replace_pages([bad], [PAGE_A])
    assert store.search("x") == []


def test_page_text_and_total_size_bounds(tmp_path):
    store = KnowledgeStore(tmp_path / "cache.db")
    with pytest.raises(ValueError):
        store.replace_pages([page(text="x" * 30_001)], [PAGE_A])
    pages = [page(f"{index:08x}-0000-4000-8000-000000000000", text="x" * 29_900)
             for index in range(1, 80)]
    allowed = [item["page_id"] for item in pages]
    with pytest.raises(ValueError):
        store.replace_pages(pages, allowed)
    assert store.search("lampe") == []


def test_stale_empty_text_and_empty_queries(tmp_path):
    now = [100_000.0]
    store = KnowledgeStore(tmp_path / "cache.db", clock=lambda: now[0])
    store.replace_pages([page(text="")], [PAGE_A])
    assert store.search("   ") == []
    assert store.search("") == []
    store.replace_pages([page(text="")], [PAGE_A])
    # Empty body is valid; the title remains searchable.
    assert store.search("wohnzimmer")[0]["text"] == ""
    now[0] += 24 * 60 * 60 + 1
    assert store.search("wohnzimmer")[0]["stale"] is True


def test_clock_must_be_finite_for_search_and_replace(tmp_path):
    store = KnowledgeStore(tmp_path / "cache.db", clock=lambda: float("nan"))
    with pytest.raises(ValueError):
        store.replace_pages([page()], [PAGE_A])
    # No record exists yet, but the clock contract is checked before returning.
    with pytest.raises(ValueError):
        store.search("wohnzimmer")


def test_file_permissions_and_symlink_fail_closed(tmp_path):
    db_dir = tmp_path / "private"
    store = KnowledgeStore(db_dir / "cache.db")
    if os.name != "nt":
        assert stat.S_IMODE(db_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    target = tmp_path / "target.db"
    target.write_text("do not open", encoding="utf-8")
    link = tmp_path / "linked.db"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    with pytest.raises(OSError):
        KnowledgeStore(link)
    assert target.read_text(encoding="utf-8") == "do not open"


def test_limits_page_count_and_search_limit(tmp_path):
    store = KnowledgeStore(tmp_path / "cache.db")
    pages = [page(f"{index:08x}-0000-4000-8000-000000000000") for index in range(1, 102)]
    with pytest.raises(ValueError):
        store.replace_pages(pages, [item["page_id"] for item in pages])
    assert store.search("lampe", limit=0) == []
