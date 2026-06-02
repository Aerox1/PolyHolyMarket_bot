"""News → Polymarket market CTA. Resolves the most relevant market for an
article (once, at render time — cached on the row) and builds the bot deep-link.

All Polymarket calls are blocking Gamma HTTP, wrapped in ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import logging

from polymarket import markets

logger = logging.getLogger(__name__)


async def best_market_id(
    *, title: str, category_tag_slug: str | None = None, hint_market_id: str | None = None
) -> str | None:
    """Resolve the conditionId of the most relevant market, or None.

    Order: an explicit hint → the article's category (top market) → a title search.
    Best-effort: upstream errors resolve to None (the item still ships, no CTA)."""
    if hint_market_id:
        return hint_market_id
    try:
        if category_tag_slug:
            rows = await asyncio.to_thread(markets.category_markets, category_tag_slug, 1)
            if rows:
                return rows[0]["id"]
        if title:
            rows = await asyncio.to_thread(markets.search_markets, title, 1)
            if rows:
                return rows[0]["id"]
    except Exception as exc:  # noqa: BLE001 — CTA is optional; never fail the render
        logger.info("CTA market resolution failed: %s", type(exc).__name__)
    return None


def news_deeplink(bot_username: str, *, item_id: int, market_id: str | None = None) -> str:
    """t.me deep-link back into the bot. ``nm-<cond>`` jumps to the market panel
    (tap-to-trade); ``n-<item>`` opens the news item. Mirrors start.py's start-arg
    convention."""
    payload = f"nm-{market_id}" if market_id else f"n-{item_id}"
    return f"https://t.me/{bot_username}?start={payload}"
