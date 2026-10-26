"""Bounded catalog of pages accessible to a Notion integration.

Completed search pagination atomically replaces the catalog.
More than 10000 search results is explicitly incomplete and preserves the old catalog.
Search includes database pages exposed by the integration.
Search access is integration access, not proof of an exhaustive account inventory.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Callable
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

try:
    from .lumi_knowledge import _uuid, _validate_url
    from .lumi_notion_sync import _NoRedirect, NOTION_VERSION
except ImportError:
    from lumi_knowledge import _uuid, _validate_url
    from lumi_notion_sync import _NoRedirect, NOTION_VERSION

SEARCH_URL = "https://api.notion.com/v1/search"
MAX_PAGES = 10_000
MAX_CATALOG_BYTES = 8 * 1024 * 1024
MAX_RESPONSE_BYTES = 1_048_576
MAX_TOTAL_SECONDS = 120.0
REQUEST_TIMEOUT_SECONDS = 5.0


class CatalogError(Exception):
    """Content-free catalog failure; existing catalog remains untouched."""


class CatalogIncomplete(CatalogError):
    """Search reported incompleteness or exceeded a supported resource limit."""


def _transport(url, headers, body, timeout):
    request = Request(url, headers=headers, data=body, method="POST")
    try:
        return build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=timeout)
    except HTTPError as response:
        return response


def title_aliases(pages: list[dict[str, str]]) -> dict[str, str]:
    """Resolve normalized titles only when exactly one page owns that title."""
    groups: dict[str, list[str]] = {}
    for page in pages:
        key = page["title"].strip().casefold()
        if key:
            groups.setdefault(key, []).append(page["id"])
    return {key: ids[0] for key, ids in groups.items() if len(ids) == 1}


class NotionCatalog:
    def __init__(self, token: str, *, post_transport: Callable = _transport,
                 monotonic: Callable = time.monotonic):
        if not isinstance(token, str) or not token or any(c in token for c in "\r\n"):
            raise ValueError("Notion read token is required")
        self._token = token
        self._transport = post_transport
        self._clock = monotonic

    def discover(self) -> list[dict[str, str]]:
        deadline = self._clock() + MAX_TOTAL_SECONDS
        def remaining():
            value = deadline - self._clock()
            if value <= 0:
                raise CatalogError("Notion catalog deadline exceeded")
            return min(value, REQUEST_TIMEOUT_SECONDS)
        pages = []
        seen_ids = set()
        seen_cursors = set()
        cursor = None
        while True:
            payload = {"filter": {"value": "page", "property": "object"}, "page_size": 100}
            if cursor is not None:
                payload["start_cursor"] = cursor
            response = None
            try:
                response = self._transport(SEARCH_URL, {
                    "Authorization": "Bearer " + self._token,
                    "Notion-Version": NOTION_VERSION, "Content-Type": "application/json",
                    "Accept": "application/json",
                }, json.dumps(payload).encode(), remaining())
                status = getattr(response, "status", getattr(response, "code", 0))
                url = response.geturl() if callable(getattr(response, "geturl", None)) else getattr(response, "url", None)
                if url is not None and url != SEARCH_URL:
                    raise CatalogError("Notion catalog redirect rejected")
                if status != 200:
                    raise CatalogError("Notion catalog request rejected")
                raw = bytearray()
                while len(raw) <= MAX_RESPONSE_BYTES:
                    remaining()
                    chunk = response.read(min(65536, MAX_RESPONSE_BYTES + 1 - len(raw)))
                    if not chunk:
                        break
                    raw.extend(chunk)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise CatalogError("Notion catalog response too large")
                document = json.loads(raw)
                remaining()
            except CatalogError:
                raise
            except Exception:
                raise CatalogError("Notion catalog request failed") from None
            finally:
                if response is not None:
                    try:
                        response.close()
                    except Exception:
                        pass
            try:
                if not isinstance(document, dict) or document.get("object") != "list":
                    raise ValueError()
                # Optional additive fields also appear on pinned API versions.
                # An explicit partial result must never replace the previous catalog.
                if "request_status" in document:
                    request_status = document["request_status"]
                    if not isinstance(request_status, dict) or request_status.get("type") != "complete":
                        raise CatalogIncomplete("Notion catalog search was not complete")
                results = document.get("results")
                more = document.get("has_more")
                next_cursor = document.get("next_cursor")
                if not isinstance(results, list) or len(results) > 100 or type(more) is not bool:
                    raise ValueError()
                for item in results:
                    if not isinstance(item, dict) or item.get("object") != "page":
                        raise ValueError()
                    page_id = _uuid(item.get("id"), "id")
                    if page_id in seen_ids:
                        raise ValueError()
                    seen_ids.add(page_id)
                    if len(seen_ids) > MAX_PAGES:
                        raise CatalogIncomplete("Notion catalog incomplete: more than 10000 pages")
                    if type(item.get("archived")) is not bool or type(item.get("in_trash")) is not bool:
                        raise ValueError()
                    if item["archived"] or item["in_trash"]:
                        continue
                    properties = item.get("properties")
                    if not isinstance(properties, dict):
                        raise ValueError()
                    titles = [p for p in properties.values() if isinstance(p, dict) and p.get("type") == "title"]
                    if len(titles) != 1 or not isinstance(titles[0].get("title"), list):
                        raise ValueError()
                    parts = titles[0]["title"]
                    if any(not isinstance(p, dict) or not isinstance(p.get("plain_text"), str) for p in parts):
                        raise ValueError()
                    title = "".join(p["plain_text"] for p in parts)
                    if len(title) > 200:
                        raise ValueError()
                    pages.append({"id": page_id, "title": title, "url": _validate_url(item.get("url"), page_id)})
                if not more:
                    if next_cursor is not None:
                        raise ValueError()
                    return pages
                if not isinstance(next_cursor, str) or not next_cursor or len(next_cursor) > 2048 or next_cursor in seen_cursors:
                    raise ValueError()
                seen_cursors.add(next_cursor)
                cursor = next_cursor
            except CatalogError:
                raise
            except Exception:
                raise CatalogError("Notion catalog response invalid") from None

    def sync(self, output: str | os.PathLike) -> dict[str, int | bool]:
        pages = self.discover()
        document = {"version": 1, "complete": True, "pages": pages, "aliases": title_aliases(pages)}
        encoded = json.dumps(document, ensure_ascii=False).encode("utf-8")
        if len(encoded) > MAX_CATALOG_BYTES:
            raise CatalogIncomplete("Notion catalog incomplete: size limit exceeded")
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
            raise ValueError("Notion catalog symlink rejected")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             prefix=".notion-catalog-", delete=False) as stream:
                temporary = Path(stream.name)
                os.chmod(temporary, 0o600)
                stream.write(encoded.decode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return {"complete": True, "pages": len(pages)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--once", action="store_true", required=True)
    args = parser.parse_args(argv)
    try:
        result = NotionCatalog(os.environ.get("LUMI_NOTION_READ_TOKEN", "")).sync(args.output)
    except (CatalogError, ValueError, OSError):
        print("Notion catalog incomplete or unavailable; previous catalog preserved.")
        return 1
    print(json.dumps(result))
    return 0




def load_catalog(path: str | os.PathLike) -> dict:
    """Load a bounded catalog; derive aliases instead of trusting stored mappings."""
    source = Path(path)
    if source.is_symlink() or any(parent.is_symlink() for parent in source.parents):
        raise ValueError("Notion catalog symlink rejected")
    with source.open("rb") as stream:
        raw = stream.read(MAX_CATALOG_BYTES + 1)
    if len(raw) > MAX_CATALOG_BYTES:
        raise ValueError("Notion catalog too large")
    try:
        document = json.loads(raw)
        if (not isinstance(document, dict) or set(document) != {"version", "complete", "pages", "aliases"}
                or type(document["version"]) is not int or document["version"] != 1
                or document["complete"] is not True or not isinstance(document["pages"], list)
                or len(document["pages"]) > MAX_PAGES):
            raise ValueError()
        pages = []
        ids = set()
        for item in document["pages"]:
            if not isinstance(item, dict) or set(item) != {"id", "title", "url"}:
                raise ValueError()
            page_id = _uuid(item["id"], "id")
            if page_id in ids or not isinstance(item["title"], str) or len(item["title"]) > 200:
                raise ValueError()
            ids.add(page_id)
            pages.append({"id": page_id, "title": item["title"], "url": _validate_url(item["url"], page_id)})
        aliases = title_aliases(pages)
        if document["aliases"] != aliases:
            raise ValueError()
        return {"page_ids": [page["id"] for page in pages], "targets": aliases}
    except (ValueError, TypeError, KeyError, UnicodeDecodeError):
        raise ValueError("Notion catalog invalid") from None


if __name__ == "__main__":
    raise SystemExit(main())
