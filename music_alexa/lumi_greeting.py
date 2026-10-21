"""Immediate, local-only Lumi launch greetings.

No conversation content, names, model calls, or persistent state are used.
The only retained state is a small in-memory replay cache keyed by opaque IDs.
"""
from __future__ import annotations

import hashlib
import math
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable


MAX_PRINCIPALS = 256
MAX_REQUESTS_PER_PRINCIPAL = 32
MAX_TTL_SECONDS = 600
PRINCIPAL_RE = re.compile(r"[0-9a-f]{64}\Z")

_GREETINGS = (
    "Hallo. Schön, dich zu hören.",
    "Da bist du ja. Ich bin ganz Ohr.",
    "Ah, Gesellschaft. Sehr angenehm.",
    "Schön, dass du da bist.",
    "Hallo. Wie schön, dich zu hören.",
    "Willkommen. Ich spare mir die Fanfaren.",
    "Hallo. Ohne große Begrüßungsrede.",
    "Schön, dich zu hören. Ganz ohne Fanfaren.",
    "Ah, ein Gespräch. Sehr schön.",
    "Hallo. Ich bin ganz Ohr.",
    "Schön, dass du da bist. Das freut mich.",
    "Willkommen. Kurz und herzlich.",
    "Da bist du. Ein angenehmer Auftakt.",
    "Hallo. Das ist ein schöner Anfang.",
    "Schön, dass du da bist. Ich mache es kurz.",
    "Ah, ein Gespräch. Ich bin gespannt.",
)


@dataclass
class _PrincipalState:
    expires_at: float
    last_index: int | None = None
    requests: OrderedDict[str, tuple[int, float]] = field(default_factory=OrderedDict)


class LocalGreetings:
    """Choose a short German greeting without blocking on model or network I/O.

    Replays of a request ID return the original choice while its bounded cache
    entry remains live. New request IDs never repeat the immediately previous
    choice for the same principal. All retained state expires within 600s.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        if not callable(clock):
            raise ValueError("clock must be callable")
        self._clock = clock
        self._lock = threading.RLock()
        self._principals: OrderedDict[str, _PrincipalState] = OrderedDict()

    def select(self, principal: str, request_id: str) -> str:
        """Return a replay-stable greeting for an opaque principal/request pair."""
        if not isinstance(principal, str) or not PRINCIPAL_RE.fullmatch(principal):
            raise ValueError("principal must be a lowercase 64-character opaque hex ID")
        if (not isinstance(request_id, str) or not request_id
                or len(request_id) > 1024):
            raise ValueError("request_id must contain 1..1024 characters")

        now = self._clock()
        if isinstance(now, bool) or not isinstance(now, (int, float)):
            raise ValueError("clock returned an invalid time")
        try:
            if not math.isfinite(now):
                raise ValueError("clock returned a non-finite time")
        except (OverflowError, TypeError, ValueError) as exc:
            raise ValueError("clock returned an invalid time") from exc
        request_key = hashlib.sha256(request_id.encode("utf-8")).hexdigest()

        with self._lock:
            self._expire(now)
            state = self._principals.get(principal)
            if state is None:
                state = _PrincipalState(expires_at=now + MAX_TTL_SECONDS)
                self._principals[principal] = state
            else:
                self._principals.move_to_end(principal)
                state.expires_at = now + MAX_TTL_SECONDS

            cached = state.requests.get(request_key)
            if cached is not None and cached[1] > now:
                state.requests.move_to_end(request_key)
                return _GREETINGS[cached[0]]

            digest = hashlib.sha256(
                principal.encode("ascii") + b"\0" + request_id.encode("utf-8")
            ).digest()
            index = int.from_bytes(digest[:8], "big") % len(_GREETINGS)
            if index == state.last_index:
                index = (index + 1) % len(_GREETINGS)

            state.last_index = index
            state.requests[request_key] = (index, now + MAX_TTL_SECONDS)
            state.requests.move_to_end(request_key)
            while len(state.requests) > MAX_REQUESTS_PER_PRINCIPAL:
                state.requests.popitem(last=False)

            while len(self._principals) > MAX_PRINCIPALS:
                self._principals.popitem(last=False)
            return _GREETINGS[index]

    def _expire(self, now: float) -> None:
        expired = [key for key, state in self._principals.items()
                   if state.expires_at <= now]
        for key in expired:
            del self._principals[key]
        for state in self._principals.values():
            for request_key, (_, expires_at) in list(state.requests.items()):
                if expires_at <= now:
                    del state.requests[request_key]
