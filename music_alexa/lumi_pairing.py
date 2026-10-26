"""Short-lived, in-memory observation of explicitly allowed Lumi pair IDs.

The caller must invoke ``observe_verified_request`` only after authenticating
the Alexa-originating relay request and validating its exact skill ID. This
module deliberately accepts IDs only; it never accepts or stores request data.
"""
from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict
from typing import Callable


MAX_OPEN_SECONDS = 900
MAX_PAIRS = 16
MAX_PROFILES_PER_PAIR = 16
PERSON_KEY_RE = re.compile(r"[0-9a-f]{64}\Z")
SKILL_ID_RE = re.compile(r"amzn1\.ask\.skill\.[0-9a-fA-F-]{36}\Z")
DEVICE_ID_RE = re.compile(r"amzn1\.ask\.device\.[A-Za-z0-9._-]{1,1000}\Z")
USER_ID_RE = re.compile(r"amzn1\.ask\.account\.[A-Za-z0-9._-]{1,1000}\Z")


class LumiPairingObserver:
    """Keep at most 16 verified skill/device/user tuples in RAM for <=15 min."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._deadline = 0.0
        self._pairs: OrderedDict[tuple[str, str, str], set[str]] = OrderedDict()

    def open(self, duration_seconds: int) -> int:
        """Open observation for 1..900 seconds and clear any previous window."""
        if (isinstance(duration_seconds, bool) or not isinstance(duration_seconds, int)
                or not 1 <= duration_seconds <= MAX_OPEN_SECONDS):
            raise ValueError("invalid pairing window")
        with self._lock:
            self._pairs.clear()
            self._deadline = self._clock() + duration_seconds
            return duration_seconds

    def close(self) -> None:
        """Close the window and immediately erase all observed identifiers."""
        with self._lock:
            self._deadline = 0.0
            self._pairs.clear()

    def observe_verified_request(
        self,
        *,
        application_id: str,
        expected_skill_id: str,
        device_id: str,
        user_id: str,
        person_key: str = "",
    ) -> bool:
        """Observe IDs only when the active window and exact skill match."""
        if (not isinstance(application_id, str)
                or not SKILL_ID_RE.fullmatch(application_id)
                or not isinstance(expected_skill_id, str)
                or not SKILL_ID_RE.fullmatch(expected_skill_id)
                or application_id != expected_skill_id
                or not isinstance(device_id, str)
                or not DEVICE_ID_RE.fullmatch(device_id)
                or not isinstance(user_id, str)
                or not USER_ID_RE.fullmatch(user_id)
                or not isinstance(person_key, str)
                or (person_key != "" and not PERSON_KEY_RE.fullmatch(person_key))):
            return False

        pair = (application_id, device_id, user_id)
        with self._lock:
            if not self._active_locked():
                return False
            if pair in self._pairs:
                profiles = self._pairs[pair]
                if person_key and person_key not in profiles:
                    if len(profiles) >= MAX_PROFILES_PER_PAIR:
                        return False
                    profiles.add(person_key)
                return True
            if len(self._pairs) >= MAX_PAIRS:
                # Keep the first verified observations; never evict one silently.
                return False
            self._pairs[pair] = {person_key} if person_key else set()
            return True

    def snapshot(self) -> dict[str, object]:
        """Return a bounded ID-only view and remaining window time."""
        with self._lock:
            active = self._active_locked()
            remaining = max(0, int(self._deadline - self._clock())) if active else 0
            pairs = [
                {"application_id": skill, "device_id": device, "user_id": user,
                 **({"person_keys": sorted(profiles)} if profiles else {})}
                for (skill, device, user), profiles in self._pairs.items()
            ] if active else []
            return {"open": active, "remaining_seconds": remaining, "pairs": pairs}

    def _active_locked(self) -> bool:
        if self._deadline <= self._clock():
            self._deadline = 0.0
            self._pairs.clear()
            return False
        return True
