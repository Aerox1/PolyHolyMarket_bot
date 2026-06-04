"""News pipeline jobs on the PTB JobQueue (replaces NabzarSocial's APScheduler).

* ``crawl_job``  — poll enabled sources, dedup by url_hash, persist backlog items.
* ``render_job`` — translate + resolve CTA + settle image for admin-approved items.

Publishing (channel + per-user DMs) lands in later phases. Both jobs isolate
failures per source / per item with savepoints, mirroring ``settlement_job``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from telegram.error import Forbidden, TelegramError
from telegram.ext import Application, ContextTypes

from bot.news import crawler, cta as cta_mod, publisher, render as render_mod
from core.config import settings
from core.i18n import t
from db.engine import async_session_scope
from db.models import NewsItem, UserNewsPrefs, UserSettings
from db.repositories import appconfig
from db.repositories import news_delivery
from db.repositories import news_items as items_repo
from db.repositories import news_prefs
from db.repositories import news_sources as sources_repo
from db.repositories import pending_intents as intents_repo

logger = logging.getLogger(__name__)

NEWS_CHANNEL_ID_KEY = "news_channel_id"
NEWS_AUTOSEND_KEY = "news_autosend"   # "1" → auto-approve a cycle's top items by SCORE
NEWS_TOP_N_KEY = "news_top_n"         # how many of a cycle's fresh items autosend promotes
# "1" (default) → auto-approve every fresh item that matches a TRENDING Polymarket
# event, so bet-relevant headlines publish hands-free regardless of raw score.
NEWS_AUTOAPPROVE_TRENDING_KEY = "news_autoapprove_trending"
# "1" (default) → post an anonymous engagement poll (the market question + its
# outcomes) under each channel card. Sentiment/social-proof only — betting stays on
# the card buttons.
NEWS_POLL_KEY = "news_poll"


async def crawl_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    async with async_session_scope() as session:
        sources = await sources_repo.enabled(session)
        # snapshot the fields we need so we can close this session before network I/O
        targets = [(s.id, s.url, s.kind, s.category_id) for s in sources]

    created: list[tuple[int, str]] = []  # (item_id, title) of this cycle's fresh items
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
                        item = await items_repo.create(
                            session, url=art.url, url_hash=art.url_hash,
                            title_orig=art.title or art.url, body_orig=art.body,
                            lang_orig=art.lang, hero_image_url=art.hero_image,
                            source_id=source_id, category_id=category_id,
                            dedup_hash=dh, score=crawler.score_article(art),
                        )
                        added += 1
                        created.append((item.id, item.title_orig or ""))
                except IntegrityError:
                    continue  # concurrent insert of the same url_hash — fine (dedup race)
                except Exception:  # noqa: BLE001 — isolate a bad item, keep the rest + mark_checked
                    logger.warning("news item insert failed (source %s)", source_id, exc_info=True)
                    continue
            await sources_repo.mark_checked(session, source_id, f"ok:{added}")
    if targets:
        logger.info("news crawl: polled %d sources", len(targets))

    if not created:
        return

    # Read the auto-approval policy in a short scope, then do the TRENDING match
    # OUTSIDE any transaction (it's a network call — never hold a DB connection
    # across Gamma I/O), then write the approvals.
    async with async_session_scope() as session:
        autoapprove_trending = (await appconfig.get(session, NEWS_AUTOAPPROVE_TRENDING_KEY, "1")) != "0"
        autosend = (await appconfig.get(session, NEWS_AUTOSEND_KEY)) == "1"
        top_n = int(await appconfig.get_float(session, NEWS_TOP_N_KEY, 5))

    # 1) Bet-relevant auto-approval (the user's ask): every fresh item whose headline
    #    matches a currently-trending Polymarket event goes straight to render→publish.
    matched = await cta_mod.trending_matches(created) if autoapprove_trending else set()

    async with async_session_scope() as session:
        if matched:
            n = await items_repo.approve_ids(session, list(matched))
            if n:
                logger.info("news: auto-approved %d of %d fresh items matching trending markets",
                            n, len(created))
        # 2) Score-based autosend (off by default): promote the cycle's top-N by score
        #    regardless of market match. The bet-relevant gate at publish still applies.
        if autosend:
            promoted = await items_repo.auto_approve_ids(session, [c[0] for c in created], top_n)
            if promoted:
                logger.info("news autosend: auto-approved %d of %d fresh items by score",
                            promoted, len(created))


async def render_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    bot_username = getattr(context.bot, "username", None)
    async with async_session_scope() as session:
        item_ids = [i.id for i in await items_repo.needing_render(session, limit=10)]
    # Render each item in its OWN short-lived session. render_item makes a slow
    # translate/summarize LLM call (Claude subprocess / up to 90s Gemini); giving
    # each item its own scope means the DB connection is held in-transaction for ONE
    # item at a time and committed between items — not one connection pinned across
    # the whole 10-item batch — and completed items survive a crash mid-batch.
    # (render_job is max_instances=1, so no concurrent run can race the re-fetch.)
    for item_id in item_ids:
        try:
            async with async_session_scope() as session:
                item = await session.get(NewsItem, item_id)
                if item is None:
                    continue
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


async def _publish_one(bot, item_id: int, *, chat_id: int, lang: str,
                       bot_username: str | None, with_poll: bool = False) -> None:
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
    msg_id = await publisher.post_item_to_channel(bot, snap, chat_id=chat_id, lang=lang,
                                                  bot_username=bot_username, with_poll=with_poll)

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
        ready_ids = []
        with_poll = False
        if chat_id is not None:
            # bet-relevant only (default): withhold items without a matched market
            require_market = (await appconfig.get(session, "news_require_market", "1")) != "0"
            with_poll = (await appconfig.get(session, NEWS_POLL_KEY, "1")) != "0"
            rows = await items_repo.ready_to_publish(session, limit=10, require_market=require_market)
            ready_ids = [i.id for i in rows]
    if not ready_ids:
        return
    if not await publisher.channel_is_admin(context.bot, chat_id):
        logger.warning("bot is not an admin of news channel %s — skipping publish", chat_id)
        return
    bot_username = getattr(context.bot, "username", None)
    for item_id in ready_ids:
        try:
            await _publish_one(context.bot, item_id, chat_id=chat_id, lang=lang,
                               bot_username=bot_username, with_poll=with_poll)
        except Exception:  # noqa: BLE001 — isolate one bad item, keep publishing the rest
            logger.exception("news publish failed for item %s; left for retry", item_id)


# ── per-user delivery (Phase 5) ──────────────────────────────────────────────

def _user_tz(tz_str: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(tz_str or "UTC")
    except Exception:  # noqa: BLE001 — bad/unknown tz string → UTC
        return ZoneInfo("UTC")


def _in_quiet_hours(hour: int, quiet_start: int | None, quiet_end: int | None) -> bool:
    if quiet_start is None or quiet_end is None or quiet_start == quiet_end:
        return False
    if quiet_start < quiet_end:
        return quiet_start <= hour < quiet_end
    return hour >= quiet_start or hour < quiet_end  # wrap-around (e.g. 22→07)


async def _deliver(context, *, mode: str, channel: str, header_key: str) -> None:
    """Shared per-user delivery loop for realtime + digest. ``mode`` selects the
    opted-in users; ``channel`` tags the dedup ledger; digest adds an hour/once-
    a-day gate via the digest_only flag below."""
    bot = context.bot
    digest = channel == "digest"
    async with async_session_scope() as session:
        targets = await news_delivery.users_for(session, mode)
        if not targets:
            return
        # Batch-load prefs + settings for ALL opted-in targets (2 queries) instead of
        # 2 per user. expire_on_commit=False keeps the loaded columns readable after
        # the scope closes, so the per-user loop reads them detached (no re-query).
        user_ids = [t[0] for t in targets]
        prefs_by_id = {
            p.user_id: p
            for p in await session.scalars(
                select(UserNewsPrefs).where(UserNewsPrefs.user_id.in_(user_ids)))
        }
        tz_by_id = {
            s.user_id: s.timezone
            for s in await session.scalars(
                select(UserSettings).where(UserSettings.user_id.in_(user_ids)))
        }
    now = datetime.now(timezone.utc)
    sent = 0
    for user_id, telegram_id, lang in targets:
        if sent >= settings.news_per_tick_cap:
            break
        # opted-in users always have a prefs row (set_delivery created it); defensive.
        prefs = prefs_by_id.get(user_id)
        if prefs is None:
            continue
        local = now.astimezone(_user_tz(tz_by_id.get(user_id) or "UTC"))
        # quiet hours suppress REALTIME pings only — a scheduled daily digest
        # fires at the user's chosen hour regardless.
        if not digest and _in_quiet_hours(local.hour, prefs.quiet_start, prefs.quiet_end):
            continue
        if digest:
            if local.hour != prefs.digest_hour:
                continue
            last = prefs.last_digest_at
            if last is not None:
                if last.tzinfo is None:  # SQLite returns naive — it's stored UTC
                    last = last.replace(tzinfo=timezone.utc)
                if last.astimezone(local.tzinfo).date() == local.date():
                    continue  # already sent a digest today
        # gather candidates in a short read scope (snapshot so we don't hold the
        # connection across the send)
        async with async_session_scope() as session:
            followed = await news_prefs.followed_ids(session, user_id)
            mkts = await news_delivery.user_market_ids(session, user_id)
            limit = prefs.max_per_digest if digest else settings.news_realtime_max
            only_relevant = prefs.only_relevant if digest else True  # realtime is high-signal only
            items = await news_delivery.candidates_for(
                session, user_id, followed_ids=followed, market_ids=mkts,
                only_relevant=only_relevant, limit=limit)
            if not items:
                continue
            snaps = [publisher.snapshot(it) for it in items]
            item_ids = [it.id for it in items]

        text = publisher.build_digest(snaps, lang=lang, header=t(header_key, lang),
                                      bot_username=getattr(bot, "username", None))
        try:
            await bot.send_message(chat_id=telegram_id, text=text, parse_mode="HTML",
                                   disable_web_page_preview=True)
        except Forbidden:  # user blocked the bot — stop delivering to them
            async with async_session_scope() as session:
                await news_prefs.set_delivery(session, user_id, "off")
            continue
        except TelegramError as exc:
            logger.info("news %s send failed for %s: %s", channel, telegram_id, type(exc).__name__)
            continue

        async with async_session_scope() as session:  # record delivery (dedup) post-send
            for iid in item_ids:
                news_delivery.mark_delivered(session, user_id, iid, channel)
            if digest:
                await news_prefs.mark_digest_sent(session, user_id, now)
        sent += 1
        await asyncio.sleep(0.05)  # gentle per-chat pacing


async def news_realtime_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await _deliver(context, mode="realtime", channel="realtime", header_key="bot.news.realtime_header")


async def news_digest_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await _deliver(context, mode="daily", channel="digest", header_key="bot.news.digest_header")


async def news_intents_cleanup_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reap past-TTL pending/resumed bet intents (deferred news-channel bets that
    were never completed) so the table can't grow unbounded."""
    async with async_session_scope() as session:
        n = await intents_repo.expire_stale(session)
    if n:
        logger.info("news: expired %d stale pending bet intents", n)


def register_news_jobs(application: Application) -> None:
    jq = application.job_queue
    if jq is None:
        logger.warning("JobQueue unavailable — news jobs not registered.")
        return
    # max_instances=1 + coalesce: never overlap a slow run with the next tick (the
    # publish job's at-most-once guarantee assumes no concurrent publishers).
    serial = {"max_instances": 1, "coalesce": True}
    # The bet-intent reaper runs REGARDLESS of the crawl/render/publish pipeline: a
    # user can open an `nb-` bet deep-link (creating a pending intent) even when the
    # pipeline is off, so the table must always stay bounded.
    jq.run_repeating(news_intents_cleanup_job, interval=3600, first=300,
                     name="news_intents_cleanup", job_kwargs=serial)
    if not settings.news_pipeline_enabled:
        logger.info("News pipeline disabled (NEWS_PIPELINE_ENABLED=0) — only the bet-intent cleanup job is registered.")
        return
    jq.run_repeating(crawl_job, interval=settings.news_crawl_interval_seconds, first=45,
                     name="news_crawl", job_kwargs=serial)
    jq.run_repeating(render_job, interval=settings.news_render_interval_seconds, first=90,
                     name="news_render", job_kwargs=serial)
    jq.run_repeating(publish_job, interval=settings.news_publish_interval_seconds, first=120,
                     name="news_publish", job_kwargs=serial)
    # per-user delivery
    jq.run_repeating(news_realtime_job, interval=120, first=150, name="news_realtime", job_kwargs=serial)
    jq.run_repeating(news_digest_job, interval=600, first=180, name="news_digest", job_kwargs=serial)
    logger.info("News pipeline jobs registered (crawl=%ss, render=%ss, publish=%ss, +realtime/digest).",
                settings.news_crawl_interval_seconds, settings.news_render_interval_seconds,
                settings.news_publish_interval_seconds)
