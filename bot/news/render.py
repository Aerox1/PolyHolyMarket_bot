"""Render one approved news item: translate/summarize (all languages) → resolve
the market CTA → settle the image status → mark ``ready`` for publish.

Drives the ``approved → translating → rendering → ready`` transitions. Budget /
egress failures degrade gracefully: the item still ships with source-language
passthrough text and no CTA, rather than getting stuck.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from bot.news import cta as cta_mod
from core import gemini
from core.config import SUPPORTED_LANGUAGES
from db.models import Category, NewsItem

logger = logging.getLogger(__name__)

_SUMMARY_CAP = 600


def _passthrough(item: NewsItem) -> dict[str, dict[str, str]]:
    """Source-language fallback when translation is unavailable (no key / budget /
    egress). The channel lang falls back to EN at publish time if absent."""
    lang = item.lang_orig or "en"
    return {lang: {"title": item.title_orig, "summary": (item.body_orig or item.title_orig)[:_SUMMARY_CAP]}}


async def render_item(
    session: AsyncSession, item: NewsItem, *, bot_username: str | None = None,
    target_langs: tuple[str, ...] = SUPPORTED_LANGUAGES,
) -> NewsItem:
    # 1) translate + summarize (one budget-charged Gemini call for all langs)
    # NOTE (deferred, narrow): the gemini_usage row is written on this session, so
    # if render_item raises AFTER this call (only the Category lookup / attribute
    # sets below), the job's savepoint rollback discards the usage row while real
    # spend occurred → a re-charge on retry. Acceptable for Phase 2; revisit by
    # committing usage in its own scope if it ever bites.
    item.status = "translating"
    translated = await gemini.translate_summarize_news(
        session, title=item.title_orig, body=item.body_orig or "", target_langs=target_langs
    )
    if translated:
        item.translations.update(translated)  # MutableDict — tracked + persisted
    elif not item.translations:
        item.translations.update(_passthrough(item))

    # 2) resolve the market CTA (cached on the row; never per-recipient)
    item.status = "rendering"
    cat = await session.get(Category, item.category_id) if item.category_id else None
    market_id = await cta_mod.best_market_id(
        title=item.title_orig,
        category_tag_slug=(cat.tag_slug if cat else None),
        hint_market_id=item.market_id,
    )
    if market_id:
        item.cta_market_id = market_id
        if bot_username:
            item.cta_url = cta_mod.news_deeplink(bot_username, item_id=item.id, market_id=market_id)
        item.cta_resolved_at = datetime.now(timezone.utc)

    # 3) image: a source hero is publish-ready as-is; AI/Pillow compositing is a
    # Phase-4 concern. No hero → leave 'none' (publish falls back to text).
    if item.hero_image_url:
        item.image_status = "ready"

    item.status = "ready"
    return item
