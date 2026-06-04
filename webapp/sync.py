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
from core.config import settings
from db.engine import async_session_scope
from db.repositories import appconfig, categories as categories_repo, gemini_usage
from polymarket import markets

logger = logging.getLogger(__name__)

_GEN_DELAY_SECONDS = 1.5  # spacing between image calls to avoid rate limits


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
    """Generate images for categories missing one.

    Stops when the weekly budget is reached, but a transient per-image failure
    just skips that category and continues (so one flaky call doesn't halt the
    whole batch)."""
    generated = 0
    cost = settings.gemini_image_cost_usd
    # Snapshot the work-list (ids) in a short scope, then process each image in its
    # OWN session so progress (status + path + usage) is committed per image and a
    # DB connection isn't pinned idle across the multi-minute generation loop.
    async with async_session_scope() as session:
        pending_ids = [c.id for c in await categories_repo.needing_images(session, limit=max_images)]
    for i, cat_id in enumerate(pending_ids):
        async with async_session_scope() as session:
            budget = await appconfig.get_float(
                session, appconfig.GEMINI_WEEKLY_BUDGET, settings.gemini_weekly_budget_usd)
            if await gemini_usage.weekly_spend(session) + cost > budget:
                logger.info("Gemini weekly budget reached — stopping image generation")
                break
            cat = await categories_repo.get(session, cat_id)
            if cat is None:
                continue
            if i:
                await asyncio.sleep(_GEN_DELAY_SECONDS)
            path = await gemini.generate_category_image(session, cat)
            if path:
                generated += 1
            # else: transient failure (or this one over budget) — skip, continue
    logger.info("Generated %d category images", generated)
    return generated
