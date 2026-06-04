"""Tiny in-memory per-key sliding-window rate limiter.

Guards against accidental floods / abuse on money-handling surfaces (bot commands,
the admin login, the mini-app bet endpoint). Per-process — fine for a single
process; swap for Redis if ever sharded. Keys may be ints (telegram/user ids) or
strings (client IPs).
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Hashable


class RateLimiter:
    def __init__(self, max_events: int = 20, window_seconds: float = 10.0) -> None:
        self._max = max_events
        self._window = window_seconds
        self._hits: dict[Hashable, deque[float]] = defaultdict(deque)

    def allow(self, key: Hashable) -> bool:
        now = time.monotonic()
        q = self._hits[key]
        cutoff = now - self._window
        while q and q[0] < cutoff:
            q.popleft()
        if len(q) >= self._max:
            return False
        q.append(now)
        return True

    def clear(self) -> None:
        """Drop all tracked keys (used by tests; also frees memory)."""
        self._hits.clear()
