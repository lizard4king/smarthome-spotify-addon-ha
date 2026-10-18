"""Server-owned periodic catalog/cache refresh and explicit write boundary."""
from __future__ import annotations

import os
from pathlib import Path
import threading
import time

try:
    from .lumi_notion_catalog import NotionCatalog, load_catalog
    from .lumi_notion_sync import NotionSync
    from .lumi_notion_write import NotionWriter
    from .lumi_notion_commands import NotionCommandService
except ImportError:
    from lumi_notion_catalog import NotionCatalog, load_catalog
    from lumi_notion_sync import NotionSync
    from lumi_notion_write import NotionWriter
    from lumi_notion_commands import NotionCommandService


class NotionRuntime:
    """Never pass tokens or external content to the command parser or model."""

    def __init__(self, directory: Path, store, *, read_token: str, write_token: str,
                 write_enabled: bool, catalog_factory=NotionCatalog,
                 sync_factory=NotionSync, writer_factory=NotionWriter, clock=time.time):
        self.directory = Path(directory)
        self.catalog_path = self.directory / "notion-catalog.json"
        self.store = store
        self._read_token = read_token
        self._write_token = write_token
        self._write_enabled = write_enabled
        self._catalog_factory = catalog_factory
        self._sync_factory = sync_factory
        self._writer_factory = writer_factory
        self._clock = clock
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._targets = {}
        self._allowed = []
        self._catalog_confirmed_at = None
        self.status = "not-configured" if not read_token else "awaiting-sync"
        # Do not trust a pre-restart catalog for writes without live discovery.

    def refresh(self) -> bool:
        if not self._read_token:
            self.status = "not-configured"
            return False
        try:
            self._catalog_factory(self._read_token).sync(self.catalog_path)
            catalog = load_catalog(self.catalog_path)
            with self._lock:
                self._allowed = catalog["page_ids"]
                self._targets = catalog["targets"]
                self._catalog_confirmed_at = self._clock()
                self.store.revoke_except(self._allowed)
            self._sync_factory(self.store, self._read_token, max_pages=10000,
                               max_total_seconds=900, max_blocks=100000).sync(self._allowed)
            self.status = "ready"
            return True
        except Exception:
            # Preserve last complete text cache, but never claim a successful refresh.
            self.status = "refresh-failed"
            return False

    def handle(self, question, request_id, *, authorized):
        with self._lock:
            fresh = (self._catalog_confirmed_at is not None
                     and 0 <= self._clock() - self._catalog_confirmed_at < 900)
            enabled = self._write_enabled and bool(self._write_token) and fresh
            if enabled and authorized:
                writer = self._writer_factory(self.directory / "notion-writes.sqlite3",
                                              self._write_token, self._allowed)
            else:
                writer = None
            service = NotionCommandService(writer, self._targets)
            return service.handle(question, request_id, authorized=authorized and enabled)

    def start(self):
        if self._thread is not None:
            return
        def run():
            while not self._stop.is_set():
                self.refresh()
                if self._stop.wait(600):
                    return
        self._thread = threading.Thread(target=run, name="notion-refresh", daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()


def prepare_runtime(directory, settings, store):
    """Environment is read only by server code, never inherited by Codex CLI."""
    return NotionRuntime(directory, store,
                         read_token=os.environ.get("LUMI_NOTION_READ_TOKEN", ""),
                         write_token=os.environ.get("LUMI_NOTION_WRITE_TOKEN", ""),
                         write_enabled=settings.notion_write_enabled)
