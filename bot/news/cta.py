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


def news_deeplink(bot_username: str, *, item_id: int) -> str:
    """t.me deep-link back into the bot, carrying the NEWS ITEM id (``n-<id>``).

    The market id is NOT encoded directly: a Polymarket conditionId is 66 chars
    and Telegram caps the start payload at 64. Instead /start loads the item and
    routes to its cached ``cta_market_id`` (tap-to-trade) when present, else opens
    the bot. Mirrors start.py's start-arg convention (``r-<code>``)."""
    return f"https://t.me/{bot_username}?start=n-{item_id}"


def bet_deeplink(bot_username: str, *, item_id: int, outcome: str) -> str:
    """Deep-link that pre-selects a bet OUTCOME on the item's market (``nb-<id>-y``
    / ``nb-<id>-n``). Only the item id + a single y/n char are encoded (≈17 chars,
    well under the 64-char cap); the token_id and price are resolved server-side
    at click time from the trusted ``cta_market_id`` — never carried in the link
    — so an edited payload can at most pick a different item or flip the outcome,
    never inject an arbitrary token or amount."""
    o = "y" if str(outcome).upper().startswith("Y") else "n"
    return f"https://t.me/{bot_username}?start=nb-{item_id}-{o}"
