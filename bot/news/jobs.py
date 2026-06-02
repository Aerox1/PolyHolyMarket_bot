"""News pipeline jobs on the PTB JobQueue (replaces NabzarSocial's APScheduler).

* ``crawl_job``  — poll enabled sources, dedup by url_hash, persist backlog items.
* ``render_job`` — translate + resolve CTA + settle image for admin-approved items.

Publishing (channel + per-user DMs) lands in later phases. Both jobs isolate
failures per source / per item with savepoints, mirroring ``settlement_job``.
"""

from __future__ import annotations

import logging

from sqlalchemy.exc import IntegrityError
from telegram.ext import Application, ContextTypes

from bot.news import crawler, render as render_mod
from core.config import settings
from db.engine import async_session_scope
from db.repositories import news_items as items_repo
from db.repositories import news_sources as sources_repo

logger = logging.getLogger(__name__)


async def crawl_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    async with async_session_scope() as session:
        sources = await sources_repo.enabled(session)
        # snapshot the fields we need so we can close this session before network I/O
        targets = [(s.id, s.url, s.kind, s.category_id) for s in sources]

    for source_id, url, kind, category_id in targets:
        try:
            articles = await crawler.fetch_articles(
                url, kind=kind, limit=settings.news_crawl_per_source_limit
            )
        except Exception as exc:  # noqa: BLE001 — one bad feed must not abort the batch
            logger.warning("news crawl failed for source %s: %s", source_id, type(exc).__name__)
            async with async_session_scope() as session:
                await sources_repo.mark_checked(session, source_id, f"error:{type(exc).__name__}"[:64])
            continue

        added = 0
        async with async_session_scope() as session:
            for art in articles:
                dh = crawler.dedup_hash(art.title or art.url)
                try:
                    async with session.begin_nested():
                        # dedup on the exact URL AND on a normalized-title hash
                        # (same story reposted across feeds)
                        if await items_repo.exists_by_url_hash(session, art.url_hash) \
                                or await items_repo.exists_by_dedup_hash(session, dh):
                            continue
                        await items_repo.create(
                            session, url=art.url, url_hash=art.url_hash,
                            title_orig=art.title or art.url, body_orig=art.body,
                            lang_orig=art.lang, hero_image_url=art.hero_image,
                            source_id=source_id, category_id=category_id,
                            dedup_hash=dh, score=crawler.score_article(art),
                        )
                        added += 1
                except IntegrityError:
                    continue  # concurrent insert of the same url_hash — fine (dedup race)
                except Exception:  # noqa: BLE001 — isolate a bad item, keep the rest + mark_checked
                    logger.warning("news item insert failed (source %s)", source_id, exc_info=True)
                    continue
            await sources_repo.mark_checked(session, source_id, f"ok:{added}")
    if targets:
        logger.info("news crawl: polled %d sources", len(targets))


async def render_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    bot_username = getattr(context.bot, "username", None)
    async with async_session_scope() as session:
        items = await items_repo.needing_render(session, limit=10)
        for item in items:
            item_id = item.id  # savepoint rollback expires ORM attrs — capture first
            try:
                async with session.begin_nested():
                    await render_mod.render_item(session, item, bot_username=bot_username)
            except Exception:  # noqa: BLE001 — isolate one bad item, keep rendering the rest
                logger.exception("news render failed for item %s; left for retry", item_id)
                continue


def register_news_jobs(application: Application) -> None:
    if not settings.news_pipeline_enabled:
        logger.info("News pipeline disabled (NEWS_PIPELINE_ENABLED=0) — jobs not registered.")
        return
    jq = application.job_queue
    if jq is None:
        logger.warning("JobQueue unavailable — news pipeline disabled.")
        return
    jq.run_repeating(crawl_job, interval=settings.news_crawl_interval_seconds, first=45, name="news_crawl")
    jq.run_repeating(render_job, interval=settings.news_render_interval_seconds, first=90, name="news_render")
    logger.info("News pipeline jobs registered (crawl=%ss, render=%ss).",
                settings.news_crawl_interval_seconds, settings.news_render_interval_seconds)
