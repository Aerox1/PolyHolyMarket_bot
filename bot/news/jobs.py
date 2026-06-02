"""News pipeline jobs on the PTB JobQueue (replaces NabzarSocial's APScheduler).

* ``crawl_job``  — poll enabled sources, dedup by url_hash, persist backlog items.
* ``render_job`` — translate + resolve CTA + settle image for admin-approved items.

Publishing (channel + per-user DMs) lands in later phases. Both jobs isolate
failures per source / per item with savepoints, mirroring ``settlement_job``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from telegram.ext import Application, ContextTypes

from bot.news import crawler, publisher, render as render_mod
from core.config import settings
from db.engine import async_session_scope
from db.models import NewsItem
from db.repositories import appconfig
from db.repositories import news_items as items_repo
from db.repositories import news_sources as sources_repo

logger = logging.getLogger(__name__)

NEWS_CHANNEL_ID_KEY = "news_channel_id"


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


async def _channel_chat_id(session) -> int | None:
    raw = (await appconfig.get(session, NEWS_CHANNEL_ID_KEY) or "").strip()
    if not raw:
        return None
    try:
        return int(raw)  # channel ids are numeric (e.g. -1001234567890)
    except ValueError:
        logger.warning("news_channel_id %r is not numeric — set the numeric channel id", raw)
        return None


async def _publish_one(bot, item_id: int, *, chat_id: int, lang: str, bot_username: str | None) -> None:
    """Publish one item with at-most-once delivery: CLAIM the (item,chat,lang)
    slot (committed) BEFORE the irreversible send, then finalize. A concurrent or
    crashed run is reconciled, never re-sent — a duplicate channel post is worse
    than a rare missed one."""
    # 1) claim
    async with async_session_scope() as session:
        item = await session.get(NewsItem, item_id)
        if item is None or item.status != "ready":
            return
        existing = await items_repo.channel_post(session, item_id, chat_id, lang)
        if existing is not None:  # already claimed/posted by a prior run — reconcile only
            item.status = "sent"
            item.published_at = item.published_at or datetime.now(timezone.utc)
            if existing.message_id and not item.channel_msg_id:
                item.channel_msg_id = existing.message_id
            return
        snap = publisher.snapshot(item)
        try:
            items_repo.record_channel_post(session, item_id=item_id, chat_id=chat_id, message_id=None, lang=lang)
            await session.flush()
        except IntegrityError:
            return  # a concurrent claim won the unique constraint

    # 2) send OUTSIDE any transaction (no DB connection held across network I/O)
    msg_id = await publisher.post_item_to_channel(bot, snap, chat_id=chat_id, lang=lang, bot_username=bot_username)

    # 3) finalize
    async with async_session_scope() as session:
        if msg_id is None:  # transient failure → release the claim so the item retries
            await items_repo.delete_channel_post(session, item_id, chat_id, lang)
            return
        item = await session.get(NewsItem, item_id)
        if item is not None:
            item.status = "sent"
            item.published_at = datetime.now(timezone.utc)
            item.channel_msg_id = msg_id
        await items_repo.set_channel_post_message_id(session, item_id, chat_id, lang, msg_id)


async def publish_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Post ready (admin-approved + rendered) items to the news channel, once each.
    Approval-miss policy: only 'ready' items publish — a missed window never
    auto-promotes. Serialized via job_kwargs(max_instances=1)."""
    lang = settings.news_channel_lang
    async with async_session_scope() as session:
        chat_id = await _channel_chat_id(session)
        ready_ids = [i.id for i in await items_repo.ready_to_publish(session, limit=10)] if chat_id is not None else []
    if not ready_ids:
        return
    if not await publisher.channel_is_admin(context.bot, chat_id):
        logger.warning("bot is not an admin of news channel %s — skipping publish", chat_id)
        return
    bot_username = getattr(context.bot, "username", None)
    for item_id in ready_ids:
        try:
            await _publish_one(context.bot, item_id, chat_id=chat_id, lang=lang, bot_username=bot_username)
        except Exception:  # noqa: BLE001 — isolate one bad item, keep publishing the rest
            logger.exception("news publish failed for item %s; left for retry", item_id)


def register_news_jobs(application: Application) -> None:
    if not settings.news_pipeline_enabled:
        logger.info("News pipeline disabled (NEWS_PIPELINE_ENABLED=0) — jobs not registered.")
        return
    jq = application.job_queue
    if jq is None:
        logger.warning("JobQueue unavailable — news pipeline disabled.")
        return
    # max_instances=1 + coalesce: never overlap a slow run with the next tick (the
    # publish job's at-most-once guarantee assumes no concurrent publishers).
    serial = {"max_instances": 1, "coalesce": True}
    jq.run_repeating(crawl_job, interval=settings.news_crawl_interval_seconds, first=45,
                     name="news_crawl", job_kwargs=serial)
    jq.run_repeating(render_job, interval=settings.news_render_interval_seconds, first=90,
                     name="news_render", job_kwargs=serial)
    jq.run_repeating(publish_job, interval=settings.news_publish_interval_seconds, first=120,
                     name="news_publish", job_kwargs=serial)
    logger.info("News pipeline jobs registered (crawl=%ss, render=%ss, publish=%ss).",
                settings.news_crawl_interval_seconds, settings.news_render_interval_seconds,
                settings.news_publish_interval_seconds)
