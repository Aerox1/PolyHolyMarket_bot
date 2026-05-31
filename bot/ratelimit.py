"""Tiny in-memory per-user sliding-window rate limiter.

Guards against accidental floods / abuse on a money-handling bot. Per-process
(fine for a single bot process); swap for Redis if the bot is ever sharded.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, max_events: int = 20, window_seconds: float = 10.0) -> None:
        self._max = max_events
        self._window = window_seconds
        self._hits: dict[int, deque[float]] = defaultdict(deque)

    def allow(self, user_id: int) -> bool:
        now = time.monotonic()
        q = self._hits[user_id]
        cutoff = now - self._window
        while q and q[0] < cutoff:
            q.popleft()
        if len(q) >= self._max:
            return False
        q.append(now)
        return True
