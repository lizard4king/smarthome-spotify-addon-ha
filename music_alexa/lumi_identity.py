"""Short-lived, in-memory display of Lumi memory owner keys.

An owner key is the HMAC person key that Codex Voice expects in
``lumi_memory_owner_keys``. The relay passes only that derived key, never a
raw Alexa person, user or device ID. Observation is limited to an explicitly
opened window of at most 15 minutes and is erased when the window closes.
"""
from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict
from typing import Callable


MAX_OPEN_SECONDS = 900
MAX_SPEAKERS = 16
PERSON_KEY_RE = re.compile(r"[0-9a-f]{64}\Z")


class LumiIdentityObserver:
    """Keep at most 16 verified person keys in RAM while a window is open."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._deadline = 0.0
        self._keys: OrderedDict[str, dict[str, float]] = OrderedDict()

    def open(self, duration_seconds: int) -> int:
        """Open observation for 1..900 seconds and clear any previous window."""
        if (isinstance(duration_seconds, bool) or not isinstance(duration_seconds, int)
                or not 1 <= duration_seconds <= MAX_OPEN_SECONDS):
            raise ValueError("invalid identity window")
        with self._lock:
            self._keys.clear()
            self._deadline = self._clock() + duration_seconds
            return duration_seconds

    def close(self) -> None:
        """Close the window and immediately erase all observed keys."""
        with self._lock:
            self._deadline = 0.0
            self._keys.clear()

    def is_open(self) -> bool:
        with self._lock:
            return self._active_locked()

    def observe(self, person_key: str) -> bool:
        """Record one derived person key; raw identifiers are rejected by format."""
        if not isinstance(person_key, str) or not PERSON_KEY_RE.fullmatch(person_key):
            return False
        with self._lock:
            if not self._active_locked():
                return False
            now = self._clock()
            entry = self._keys.get(person_key)
            if entry is not None:
                entry["count"] += 1
                entry["last"] = now
                return True
            if len(self._keys) >= MAX_SPEAKERS:
                return False
            self._keys[person_key] = {"count": 1, "last": now}
            return True

    def snapshot(self) -> dict[str, object]:
        """Return the keys seen in the open window, newest observation last."""
        with self._lock:
            active = self._active_locked()
            now = self._clock()
            remaining = max(0, int(self._deadline - now)) if active else 0
            speakers = [
                {"person_key": key, "requests": int(entry["count"]),
                 "seconds_since_last": max(0, int(now - entry["last"]))}
                for key, entry in self._keys.items()
            ] if active else []
            return {"open": active, "remaining_seconds": remaining, "speakers": speakers}

    def _active_locked(self) -> bool:
        if self._deadline <= self._clock():
            self._deadline = 0.0
            self._keys.clear()
            return False
        return True
