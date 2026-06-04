"""Render one approved news item: translate/summarize (all languages) → resolve
the market CTA → settle the image status → mark ``ready`` for publish.

Drives the ``approved → translating → rendering → ready`` transitions. Budget /
egress failures degrade gracefully: the item still ships with source-language
passthrough text and no CTA, rather than getting stuck.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from bot.news import cta as cta_mod
from core import gemini
from core.config import SUPPORTED_LANGUAGES
from db.models import Category, NewsItem

logger = logging.getLogger(__name__)

_SUMMARY_CAP = 600


def _clip_summary(text: str | None, limit: int = _SUMMARY_CAP) -> str:
    """Clip a long body to a clean teaser ≤ ``limit`` chars.

    Ends on the LAST COMPLETE SENTENCE within the window so it never stops
    mid-sentence (the 'TikTok Pro Events … merchandise through' bug, where the raw
    body was hard-cut at 600). If no sentence boundary is reasonably near, falls
    back to a word boundary with an '…'. Cheap, no LLM."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    window = text[:limit]
    end = max((window.rfind(p) for p in (". ", ".\n", "! ", "!\n", "? ", "?\n")), default=-1)
    if end >= limit * 0.5:                       # a full sentence ends well into the window
        return window[: end + 1].strip()         # keep whole sentences, no ellipsis needed
    sp = window.rfind(" ")
    cut = (window[:sp] if sp > 0 else window).rstrip(" ,;:–—-\n")
    return (cut + "…") if cut else window


def _passthrough(item: NewsItem) -> dict[str, dict[str, str]]:
    """Source-language fallback when translation is unavailable (no key / budget /
    egress). The channel lang falls back to EN at publish time if absent."""
    lang = item.lang_orig or "en"
    return {lang: {"title": item.title_orig, "summary": _clip_summary(item.body_orig or item.title_orig)}}


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

    # 2) resolve the bet CTA (cached on the row; never per-recipient). Event-aware +
    # relevance-gated: multi-outcome events keep their real choices, unrelated markets
    # are never attached.
    item.status = "rendering"
    # The deep-link always points at this item (n-<id>) regardless of the market.
    if bot_username:
        item.cta_url = cta_mod.news_deeplink(bot_username, item_id=item.id)
    # Resolve the bet CTA ONCE and keep it stable: a re-render must not reorder
    # cta_outcomes, because the already-posted channel buttons deep-link by INDEX
    # (nb-<id>-<index>) and would otherwise point at a different outcome than shown.
    if not item.cta_resolved_at:
        cat = await session.get(Category, item.category_id) if item.category_id else None
        cta = await cta_mod.resolve_cta(
            title=item.title_orig,
            category_tag_slug=(cat.tag_slug if cat else None),
            hint_market_id=item.market_id,
        )
        if cta:
            item.cta_market_id = cta.get("market_id")
            q = cta.get("question")
            item.cta_market_question = q[:300] if q else None
            item.cta_outcomes = cta.get("outcomes") or None
            item.cta_resolved_at = datetime.now(timezone.utc)

    # 3) image: a source hero is publish-ready as-is; AI/Pillow compositing is a
    # Phase-4 concern. No hero → leave 'none' (publish falls back to text).
    if item.hero_image_url:
        item.image_status = "ready"

    # An item with no matching market still renders to 'ready', but the publish job
    # withholds it from the channel while news_require_market is on (bet-relevant
    # only) — it stays dormant rather than posting as a bare, betless headline.
    item.status = "ready"
    return item
