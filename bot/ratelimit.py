"""Backwards-compatible re-export.

The rate limiter now lives in :mod:`core.ratelimit` so the key-less dashboard and
the mini-app can share it without importing from the ``bot`` package.
"""

from __future__ import annotations

from core.ratelimit import RateLimiter

__all__ = ["RateLimiter"]
