"""Category sync + image generation for the Mini App.

``sync_categories`` refreshes the category deck from Polymarket (top tags by
volume). ``generate_pending_images`` fills in Gemini card images for visible
categories that lack one — stopping as soon as the weekly budget is reached
(``generate_category_image`` returns None once over budget).
"""

from __future__ import annotations

import asyncio
import logging

from core import gemini
from db.engine import async_session_scope
from db.repositories import categories as categories_repo
from polymarket import markets

logger = logging.getLogger(__name__)


async def sync_categories(limit: int = 30) -> int:
    cats = await asyncio.to_thread(markets.top_categories, limit)
    async with async_session_scope() as session:
        for c in cats:
            await categories_repo.upsert_from_tag(
                session, slug=c["slug"], title=c["title"],
                tag_id=c["tag_id"], tag_slug=c["tag_slug"], volume=c["volume"],
            )
    logger.info("Synced %d categories", len(cats))
    return len(cats)


async def generate_pending_images(max_images: int = 30) -> int:
    """Generate images for categories missing one, until budget or max is hit."""
    generated = 0
    async with async_session_scope() as session:
        pending = await categories_repo.needing_images(session, limit=max_images)
        for cat in pending:
            path = await gemini.generate_category_image(session, cat)
            if path is None:
                # budget exhausted or generation unavailable — stop early
                break
            generated += 1
    logger.info("Generated %d category images", generated)
    return generated
