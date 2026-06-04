"""Extra coverage for the news pipeline — the branches the existing
tests/test_news_pipeline.py, test_news_publish.py, test_news_crawler.py and
test_news_delivery.py do NOT exercise.

Covers (jobs.py): the crawl item-insert error/integrity isolation, the render
isolation path, the non-numeric channel-id guard, _publish_one's not-ready /
reconcile / transient-failure branches, publish_job per-item isolation, the
bad-tz fallback, the no-targets + per-tick-cap + Forbidden/TelegramError delivery
branches, the intent-cleanup count path, and the JobQueue-None guard.
(crawler.py): deps-unavailable, HTTP >=400, single-HTML challenge/empty, empty
hero content, missing-link RSS entries, and the channel-level language fallback
when only the channel declares a language.
(publisher.py): channel_is_admin true/false/exception, post_item transient
TelegramError on photo + text + plain-fallback, and build_digest's two-button vs
single-link branches.

Telegram, Gemini, Polymarket and httpx are all mocked — never the network.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from telegram.error import BadRequest, Forbidden, NetworkError, TelegramError

from bot.news import crawler
from bot.news import jobs as news_jobs
from bot.news import publisher
from bot.news.crawler import FetchedArticle
from db.engine import async_session_scope
from db.models import (
    Category,
    NewsChannelPost,
    NewsItem,
    NewsSource,
    PendingIntent,
    User,
)
from db.repositories import appconfig
from db.repositories import news_items as items_repo
from db.repositories import news_prefs


def _afn(value):
    async def _f(*a, **k):
        return value
    return _f


@pytest.fixture(autouse=True)
def _stub_events(monkeypatch):
    # crawl_job folds a trending-market match into auto-approval; keep it hermetic.
    from bot.news import cta as news_cta
    monkeypatch.setattr(news_cta.markets, "trending_events", lambda n=40: [])
    monkeypatch.setattr(news_cta.markets, "search_events", lambda q, n=20: [])


# ── helpers ───────────────────────────────────────────────────────────────────

async def _seed_source(url="https://feed/rss", kind="rss"):
    async with async_session_scope() as s:
        src = NewsSource(name="Feed", url=url, url_hash=crawler.url_hash(url), kind=kind, enabled=True)
        s.add(src)
        await s.flush()
        return src.id


async def _count_items():
    async with async_session_scope() as s:
        return (await s.execute(select(func.count()).select_from(NewsItem))).scalar()


async def _user(telegram_id=700, **kw):
    async with async_session_scope() as s:
        u = User(telegram_id=telegram_id, username="u", language="en", status="active", **kw)
        s.add(u)
        await s.flush()
        return u.id


async def _topic(slug="t-extra", **kw):
    async with async_session_scope() as s:
        c = Category(slug=slug, title="Topic", kind="news", **kw)
        s.add(c)
        await s.flush()
        return c.id


async def _sent_item(url_hash, *, category_id=None, cta_market_id=None):
    async with async_session_scope() as s:
        it = NewsItem(url="https://n/" + url_hash, url_hash=url_hash, title_orig="Item " + url_hash,
                      status="sent", category_id=category_id, cta_market_id=cta_market_id,
                      published_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                      translations={"en": {"title": "Item", "summary": "s"}})
        s.add(it)
        await s.flush()
        return it.id


async def _seed_ready(url_hash="rdy", **kw):
    async with async_session_scope() as s:
        it = await items_repo.create(s, url="https://n/" + url_hash, url_hash=url_hash,
                                     title_orig="Ready item", **kw)
        it.status = "ready"
        it.translations.update({"en": {"title": "Ready item", "summary": "summary"}})
        it.cta_url = f"https://t.me/TestBot?start=n-{it.id}"
        return it.id


async def _set_prefs(uid, **kw):
    async with async_session_scope() as s:
        p = await news_prefs.get_or_create(s, uid)
        for k, v in kw.items():
            setattr(p, k, v)


async def _set_channel(value="-1001234567890"):
    async with async_session_scope() as s:
        await appconfig.set_(s, "news_channel_id", value)


class _Msg:
    def __init__(self, mid):
        self.message_id = mid


class _Bot:
    """Mock Telegram bot: records every send, returns a message with id 202/101."""
    def __init__(self):
        self.calls = []
        self.username = "TestBot"

    async def send_photo(self, **kw):
        self.calls.append(("photo", kw))
        return _Msg(101)

    async def send_message(self, **kw):
        self.calls.append(("text", kw))
        return _Msg(202)

    async def get_me(self):
        return SimpleNamespace(id=1)

    async def get_chat_member(self, chat_id, user_id):
        return SimpleNamespace(status="administrator")


# ══════════════════════════════════════════════════════════════════════════════
# jobs.py — crawl item-insert isolation (lines 74-78)
# ══════════════════════════════════════════════════════════════════════════════

async def test_crawl_job_isolates_bad_item_insert(monkeypatch):
    """A single failing item insert must NOT abort the batch: the other item is
    still persisted and the source is marked ok with the surviving count."""
    await _seed_source()
    good = FetchedArticle("https://x/good", crawler.url_hash("https://x/good"), "Good", "b", "en", None)
    bad = FetchedArticle("https://x/bad", crawler.url_hash("https://x/bad"), "Bad", "b", "en", None)
    monkeypatch.setattr(crawler, "fetch_articles", _afn([good, bad]))

    real_create = items_repo.create
    calls = {"n": 0}

    async def flaky_create(session, **kw):
        calls["n"] += 1
        if kw.get("url") == "https://x/bad":  # blow up on exactly one item
            raise RuntimeError("boom item")
        return await real_create(session, **kw)

    monkeypatch.setattr(news_jobs.items_repo, "create", flaky_create)
    await news_jobs.crawl_job(SimpleNamespace())
    # only the good item survived; the bad one was isolated (line 76-78)
    assert await _count_items() == 1
    async with async_session_scope() as s:
        it = await s.scalar(select(NewsItem))
        assert it.url == "https://x/good"
        src = await s.scalar(select(NewsSource))
        assert src.last_status == "ok:1"  # mark_checked still ran after the isolated failure


async def test_crawl_job_isolates_integrity_error(monkeypatch):
    """IntegrityError on insert (dedup race) is swallowed per item (line 74-75)."""
    from sqlalchemy.exc import IntegrityError

    await _seed_source()
    art = FetchedArticle("https://x/dup", crawler.url_hash("https://x/dup"), "Dup", "b", "en", None)
    monkeypatch.setattr(crawler, "fetch_articles", _afn([art]))

    async def integrity_create(session, **kw):
        raise IntegrityError("dup", None, Exception("unique"))

    monkeypatch.setattr(news_jobs.items_repo, "create", integrity_create)
    await news_jobs.crawl_job(SimpleNamespace())
    assert await _count_items() == 0  # the race-losing insert is dropped, batch survives
    async with async_session_scope() as s:
        src = await s.scalar(select(NewsSource))
        assert src.last_status == "ok:0"


# ══════════════════════════════════════════════════════════════════════════════
# jobs.py — render isolation (lines 93-95)
# ══════════════════════════════════════════════════════════════════════════════

async def test_render_job_isolates_failing_item(monkeypatch):
    """render_item raising must be isolated; the item is left for retry (still
    'approved', not advanced) and the job does not propagate the error."""
    async with async_session_scope() as s:
        item = await items_repo.create(s, url="rerr", url_hash="rerr1", title_orig="Will fail")
        item.status = "approved"

    async def boom(session, item, **kw):
        raise RuntimeError("render exploded")

    monkeypatch.setattr(news_jobs.render_mod, "render_item", boom)
    # must not raise
    await news_jobs.render_job(SimpleNamespace(bot=SimpleNamespace(username="B")))
    async with async_session_scope() as s:
        it = await s.scalar(select(NewsItem))
        assert it.status == "approved"  # untouched, left for retry


async def test_render_job_no_bot_username(monkeypatch):
    """context.bot without a username attribute → bot_username falls to None
    (getattr default), exercising the getattr branch on line 85."""
    seen = {}

    async def fake_render(session, item, *, bot_username=None):
        seen["bu"] = bot_username
        item.status = "ready"

    monkeypatch.setattr(news_jobs.render_mod, "render_item", fake_render)
    async with async_session_scope() as s:
        it = await items_repo.create(s, url="rnb", url_hash="rnb1", title_orig="No bot username")
        it.status = "approved"
    # context.bot is an object WITHOUT a `username` attr
    await news_jobs.render_job(SimpleNamespace(bot=SimpleNamespace()))
    assert seen["bu"] is None


# ══════════════════════════════════════════════════════════════════════════════
# jobs.py — _channel_chat_id guard (lines 104-106)
# ══════════════════════════════════════════════════════════════════════════════

async def test_channel_chat_id_parses_numeric():
    await _set_channel("-1009999")
    async with async_session_scope() as s:
        assert await news_jobs._channel_chat_id(s) == -1009999


async def test_channel_chat_id_none_when_unset():
    async with async_session_scope() as s:
        assert await news_jobs._channel_chat_id(s) is None


async def test_channel_chat_id_none_when_non_numeric():
    """A non-numeric channel id (e.g. an @handle) is rejected → None (line 104-106)."""
    await _set_channel("@somechannel")
    async with async_session_scope() as s:
        assert await news_jobs._channel_chat_id(s) is None


async def test_publish_job_noop_with_non_numeric_channel():
    """End-to-end: a non-numeric channel id makes chat_id None → no items collected
    → publish_job returns without an admin check or a send."""
    await _set_channel("not-a-number")
    await _seed_ready(url_hash="nn1")
    bot = _Bot()
    await news_jobs.publish_job(SimpleNamespace(bot=bot))
    assert bot.calls == []


# ══════════════════════════════════════════════════════════════════════════════
# jobs.py — _publish_one branches (lines 118-131, 139-140)
# ══════════════════════════════════════════════════════════════════════════════

async def test_publish_one_skips_when_not_ready():
    """An item that isn't 'ready' (e.g. went back to backlog) is skipped — no
    claim, no send (line 117-118)."""
    async with async_session_scope() as s:
        it = await items_repo.create(s, url="pn1", url_hash="pn1", title_orig="Backlog")
        item_id = it.id  # status defaults to 'backlog'
    bot = _Bot()
    await news_jobs._publish_one(bot, item_id, chat_id=-100, lang="en", bot_username="TestBot")
    assert bot.calls == []
    async with async_session_scope() as s:
        posts = (await s.execute(select(func.count()).select_from(NewsChannelPost))).scalar()
        assert posts == 0


async def test_publish_one_skips_missing_item():
    """A vanished item id (None from session.get) is skipped cleanly."""
    bot = _Bot()
    await news_jobs._publish_one(bot, 999999, chat_id=-100, lang="en", bot_username="TestBot")
    assert bot.calls == []


async def test_publish_one_reconciles_already_claimed(monkeypatch):
    """A prior run already recorded a channel post (with a message_id) but the item
    is still 'ready' (crash before finalize): reconcile to 'sent' + adopt the
    existing message_id, WITHOUT re-sending (lines 119-125)."""
    item_id = await _seed_ready(url_hash="recon")
    async with async_session_scope() as s:
        # simulate a committed-but-not-finalized claim from a prior run
        items_repo.record_channel_post(s, item_id=item_id, chat_id=-100, message_id=555, lang="en")

    bot = _Bot()
    await news_jobs._publish_one(bot, item_id, chat_id=-100, lang="en", bot_username="TestBot")
    assert bot.calls == []  # reconcile only — never re-sent
    async with async_session_scope() as s:
        it = await s.get(NewsItem, item_id)
        assert it.status == "sent"
        assert it.channel_msg_id == 555  # adopted from the existing post
        assert it.published_at is not None


async def test_publish_one_releases_claim_on_transient_failure(monkeypatch):
    """When post_item_to_channel returns None (transient send failure), the claim
    is RELEASED so the item retries next tick — item stays 'ready', no post row
    (lines 138-140)."""
    item_id = await _seed_ready(url_hash="trans")
    monkeypatch.setattr(news_jobs.publisher, "post_item_to_channel", _afn(None))

    bot = _Bot()
    await news_jobs._publish_one(bot, item_id, chat_id=-100, lang="en", bot_username="TestBot")
    async with async_session_scope() as s:
        it = await s.get(NewsItem, item_id)
        assert it.status == "ready"  # not advanced — left for retry
        posts = (await s.execute(select(func.count()).select_from(NewsChannelPost))).scalar()
        assert posts == 0  # claim released


async def test_publish_one_finalizes_on_success(monkeypatch):
    """Happy finalize path through _publish_one directly: returns a msg id → item
    'sent', channel_msg_id set, post row message_id updated (line 141-146)."""
    item_id = await _seed_ready(url_hash="okfin")
    monkeypatch.setattr(news_jobs.publisher, "post_item_to_channel", _afn(777))

    bot = _Bot()
    await news_jobs._publish_one(bot, item_id, chat_id=-100, lang="en", bot_username="TestBot")
    async with async_session_scope() as s:
        it = await s.get(NewsItem, item_id)
        assert it.status == "sent" and it.channel_msg_id == 777
        post = await s.scalar(select(NewsChannelPost))
        assert post.message_id == 777


# ══════════════════════════════════════════════════════════════════════════════
# jobs.py — publish_job per-item isolation (lines 166-167)
# ══════════════════════════════════════════════════════════════════════════════

async def test_publish_job_isolates_failing_item(monkeypatch):
    """If _publish_one raises for one item, publish_job logs + continues (line
    166-167) — the job itself never raises."""
    await _set_channel()
    await _seed_ready(url_hash="iso1")

    async def boom(*a, **k):
        raise RuntimeError("publish exploded")

    monkeypatch.setattr(news_jobs, "_publish_one", boom)
    bot = _Bot()
    # must not raise despite _publish_one blowing up
    await news_jobs.publish_job(SimpleNamespace(bot=bot))


# ══════════════════════════════════════════════════════════════════════════════
# jobs.py — _user_tz fallback (lines 172-176)
# ══════════════════════════════════════════════════════════════════════════════

def test_user_tz_valid():
    from zoneinfo import ZoneInfo
    assert news_jobs._user_tz("America/New_York") == ZoneInfo("America/New_York")


def test_user_tz_none_defaults_utc():
    from zoneinfo import ZoneInfo
    assert news_jobs._user_tz(None) == ZoneInfo("UTC")


def test_user_tz_bad_string_defaults_utc():
    """An unknown tz string must fall back to UTC, not raise (lines 175-176)."""
    from zoneinfo import ZoneInfo
    assert news_jobs._user_tz("Not/AReal_Zone") == ZoneInfo("UTC")


# ══════════════════════════════════════════════════════════════════════════════
# jobs.py — _deliver: no targets / per-tick cap / Forbidden / TelegramError
# ══════════════════════════════════════════════════════════════════════════════

async def test_deliver_no_targets_returns(monkeypatch):
    """No opted-in users → _deliver returns immediately (line 195-196). Bot is a
    sentinel that would explode if touched."""
    class _ExplodingBot:
        username = "TestBot"

        async def send_message(self, **kw):
            raise AssertionError("must not send when there are no targets")

    # no users opted into realtime at all
    await news_jobs.news_realtime_job(SimpleNamespace(bot=_ExplodingBot()))


async def test_deliver_per_tick_cap_breaks(monkeypatch):
    """With the per-tick cap set to 1, only one user is served this tick (line
    200-201 break)."""
    monkeypatch.setattr(news_jobs.settings, "news_per_tick_cap", 1)
    cid = await _topic("captopic")
    # two users, both opted into realtime, both following the topic with a fresh item
    await _sent_item("cap1", category_id=cid)
    for tg in (711, 712):
        uid = await _user(tg)
        await _set_prefs(uid, delivery="realtime")
        async with async_session_scope() as s:
            await news_prefs.toggle_follow(s, uid, cid)

    bot = _Bot()
    await news_jobs.news_realtime_job(SimpleNamespace(bot=bot))
    assert len(bot.calls) == 1  # cap hit after the first send


async def test_deliver_forbidden_turns_off_delivery():
    """A Forbidden (user blocked the bot) must flip the user's delivery to 'off'
    so we stop trying (lines 238-241)."""
    cid = await _topic("forbid")
    await _sent_item("fb1", category_id=cid)
    uid = await _user(713)
    await _set_prefs(uid, delivery="realtime")
    async with async_session_scope() as s:
        await news_prefs.toggle_follow(s, uid, cid)

    class _BlockedBot:
        username = "TestBot"

        async def send_message(self, **kw):
            raise Forbidden("bot was blocked by the user")

    await news_jobs.news_realtime_job(SimpleNamespace(bot=_BlockedBot()))
    async with async_session_scope() as s:
        p = await news_prefs.get_or_create(s, uid)
        assert p.delivery == "off"  # disabled after the block


async def test_deliver_telegram_error_skips_user():
    """A generic TelegramError on send is logged + the user is skipped, but
    delivery stays on and no delivered ledger row is written (lines 242-244)."""
    cid = await _topic("tgerr")
    await _sent_item("te1", category_id=cid)
    uid = await _user(714)
    await _set_prefs(uid, delivery="realtime")
    async with async_session_scope() as s:
        await news_prefs.toggle_follow(s, uid, cid)

    class _FlakyBot:
        username = "TestBot"

        async def send_message(self, **kw):
            raise NetworkError("temporary network glitch")

    await news_jobs.news_realtime_job(SimpleNamespace(bot=_FlakyBot()))
    from db.models import NewsDelivered
    async with async_session_scope() as s:
        p = await news_prefs.get_or_create(s, uid)
        assert p.delivery == "realtime"  # NOT turned off (transient)
        delivered = (await s.execute(select(func.count()).select_from(NewsDelivered))).scalar()
        assert delivered == 0  # nothing recorded — the send failed


async def test_digest_skips_when_already_sent_today_naive_last():
    """Digest dedup with a NAIVE last_digest_at (as SQLite returns): same date in
    the user's tz → no second digest (lines 216-220, naive→UTC normalization)."""
    cid = await _topic("naive-dig")
    await _sent_item("nd1", category_id=cid)
    uid = await _user(715)
    hour = datetime.now(timezone.utc).hour
    # store a NAIVE last_digest_at for "now" (today) → must be treated as UTC
    naive_now = datetime.now(timezone.utc).replace(tzinfo=None)
    await _set_prefs(uid, delivery="daily", digest_hour=hour, only_relevant=False,
                     last_digest_at=naive_now)
    bot = _Bot()
    await news_jobs.news_digest_job(SimpleNamespace(bot=bot))
    assert bot.calls == []  # already sent today (naive last normalized to UTC)


# ══════════════════════════════════════════════════════════════════════════════
# jobs.py — intents cleanup count path (lines 266-269)
# ══════════════════════════════════════════════════════════════════════════════

async def test_intents_cleanup_expires_stale():
    """A past-TTL pending intent is reaped (status→expired) and the count branch
    (line 268-269) is taken."""
    uid = await _user(716)
    async with async_session_scope() as s:
        s.add(PendingIntent(
            user_id=uid, news_item_id=None, market_id="0xM", outcome="YES",
            source="news", status="pending", idempotency_key="k-stale",
            expires_at=datetime.now(timezone.utc) - timedelta(hours=1)))
    await news_jobs.news_intents_cleanup_job(SimpleNamespace())
    async with async_session_scope() as s:
        row = await s.scalar(select(PendingIntent))
        assert row.status == "expired"


async def test_intents_cleanup_noop_when_nothing_stale():
    """No stale intents → the count is 0, the log branch is skipped, no error."""
    uid = await _user(717)
    async with async_session_scope() as s:
        s.add(PendingIntent(
            user_id=uid, news_item_id=None, market_id="0xM", outcome="YES",
            source="news", status="pending", idempotency_key="k-fresh",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=10)))
    await news_jobs.news_intents_cleanup_job(SimpleNamespace())
    async with async_session_scope() as s:
        row = await s.scalar(select(PendingIntent))
        assert row.status == "pending"  # untouched


# ══════════════════════════════════════════════════════════════════════════════
# jobs.py — register guard when JobQueue is None (lines 275-276)
# ══════════════════════════════════════════════════════════════════════════════

def test_register_news_jobs_no_job_queue(monkeypatch):
    """application.job_queue is None → registration warns and returns without
    touching anything (lines 274-276)."""
    monkeypatch.setattr(news_jobs.settings, "news_pipeline_enabled", True)
    app = SimpleNamespace(job_queue=None)
    # must not raise
    news_jobs.register_news_jobs(app)


# ══════════════════════════════════════════════════════════════════════════════
# crawler.py — fetch_articles guard/error branches
# ══════════════════════════════════════════════════════════════════════════════

async def test_fetch_articles_raises_when_deps_unavailable(monkeypatch):
    """With the optional parse deps flagged unavailable, fetch_articles raises a
    clear RuntimeError BEFORE any HTTP (line 216-217). The crawl job catches it."""
    monkeypatch.setattr(crawler, "_DEPS_AVAILABLE", False)
    with pytest.raises(RuntimeError, match="deps not installed"):
        await crawler.fetch_articles("https://feed/rss")


async def test_fetch_articles_http_error_status_returns_empty(monkeypatch):
    """A >=400 status from the top-level fetch returns [] (line 218-220)."""
    if not crawler._DEPS_AVAILABLE:
        pytest.skip("news parse deps not installed")
    monkeypatch.setattr(crawler, "_http_get", _afn((404, "", "text/html")))
    assert await crawler.fetch_articles("https://x/missing") == []


# These exercise real feedparser + BeautifulSoup; skip on minimal installs.
_parse = pytest.mark.skipif(not crawler._DEPS_AVAILABLE, reason="news parse deps not installed")


def _router(routes):
    async def _fake_http_get(url):
        return routes[url]
    return _fake_http_get


@_parse
async def test_fetch_articles_single_html_empty_extract_returns_empty(monkeypatch):
    """A single HTML page where trafilatura extracts nothing → [] (line 250-252)."""
    page = "<html><head><title>Empty</title></head><body></body></html>"
    monkeypatch.setattr(crawler, "_http_get", _router({"https://site/empty": (200, page, "text/html")}))
    monkeypatch.setattr(crawler.trafilatura, "extract", lambda body, **k: None)
    assert await crawler.fetch_articles("https://site/empty", kind="auto") == []


@_parse
async def test_fetch_articles_single_html_challenge_returns_empty(monkeypatch):
    """A single HTML page whose extracted text looks like a bot-challenge page →
    [] (line 251-252, challenge branch)."""
    page = "<html><head><title>Blocked</title></head><body>x</body></html>"
    monkeypatch.setattr(crawler, "_http_get", _router({"https://site/block": (200, page, "text/html")}))
    monkeypatch.setattr(crawler.trafilatura, "extract", lambda body, **k: "Attention Required! Just a moment...")
    assert await crawler.fetch_articles("https://site/block", kind="auto") == []


@_parse
async def test_fetch_articles_single_html_no_og_image(monkeypatch):
    """_hero_from_html returns None when there is no og:image meta (line 201)."""
    page = "<html><head><title>No Image</title></head><body>body</body></html>"
    monkeypatch.setattr(crawler, "_http_get", _router({"https://site/noimg": (200, page, "text/html")}))
    monkeypatch.setattr(crawler.trafilatura, "extract", lambda body, **k: "Some real article body here.")
    arts = await crawler.fetch_articles("https://site/noimg", kind="auto")
    assert len(arts) == 1
    assert arts[0].hero_image is None
    assert arts[0].title == "No Image"
    assert arts[0].lang is None  # single-HTML lang is always None


@_parse
async def test_fetch_articles_rss_skips_entries_without_link(monkeypatch):
    """RSS entries missing <link> are skipped (line 230-231); the no-language feed
    leaves lang=None (channel fallback is also None)."""
    rss = ("""<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item><title>No link here</title></item>
  <item><title>Has link</title><link>https://news.example.com/ok</link></item>
</channel></rss>""")
    routes = {
        "https://feed/nolink": (200, rss, "application/rss+xml"),
        "https://news.example.com/ok": (200, "<html><body>ok</body></html>", "text/html"),
    }
    monkeypatch.setattr(crawler, "_http_get", _router(routes))
    monkeypatch.setattr(crawler.trafilatura, "extract", lambda body, **k: "Article body extracted ok.")
    arts = await crawler.fetch_articles("https://feed/nolink", kind="auto")
    assert [a.title for a in arts] == ["Has link"]  # the link-less entry was skipped
    assert arts[0].lang is None  # no entry lang, no channel <language>


@_parse
async def test_fetch_articles_rss_skips_failing_inner_fetch(monkeypatch):
    """An inner article fetch that raises UnsafeUrlError is skipped, not fatal
    (line 233-236)."""
    rss = ("""<?xml version="1.0"?>
<rss version="2.0"><channel><language>en</language>
  <item><title>Bad inner</title><link>https://news.example.com/bad</link></item>
  <item><title>Good inner</title><link>https://news.example.com/good</link></item>
</channel></rss>""")

    async def fake_http_get(url):
        if url.endswith("/bad"):
            raise crawler.UnsafeUrlError("blocked inner")
        if url.endswith("/good"):
            return (200, "<html><body>good</body></html>", "text/html")
        return (200, rss, "application/rss+xml")

    monkeypatch.setattr(crawler, "_http_get", fake_http_get)
    monkeypatch.setattr(crawler.trafilatura, "extract", lambda body, **k: "Good article body content.")
    arts = await crawler.fetch_articles("https://feed/mixed", kind="auto")
    assert [a.title for a in arts] == ["Good inner"]  # bad inner skipped
    assert arts[0].lang == "en"  # channel-level language fallback


@_parse
async def test_fetch_articles_rss_skips_inner_400(monkeypatch):
    """An inner article returning >=400 is skipped (line 237-238)."""
    rss = ("""<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item><title>404 inner</title><link>https://news.example.com/missing</link></item>
</channel></rss>""")
    routes = {
        "https://feed/inner404": (200, rss, "application/rss+xml"),
        "https://news.example.com/missing": (503, "", "text/html"),
    }
    monkeypatch.setattr(crawler, "_http_get", _router(routes))
    monkeypatch.setattr(crawler.trafilatura, "extract", lambda body, **k: "unused")
    assert await crawler.fetch_articles("https://feed/inner404", kind="auto") == []


def test_hero_from_html_ignores_empty_content():
    """og:image present but with empty/whitespace content → None (line 200)."""
    if not crawler._DEPS_AVAILABLE:
        pytest.skip("news parse deps not installed")
    assert crawler._hero_from_html('<meta property="og:image" content="   ">') is None
    assert crawler._hero_from_html("<html><body>no meta</body></html>") is None


def test_looks_like_rss_and_challenge_helpers():
    """Direct unit coverage for the cheap content sniffers."""
    assert crawler._looks_like_rss("<rss version='2.0'>") is True
    assert crawler._looks_like_rss("<feed xmlns='...'>") is True
    assert crawler._looks_like_rss("<html><body>") is False
    assert crawler._looks_like_challenge_page("") is False
    assert crawler._looks_like_challenge_page("Just a moment...") is True
    assert crawler._looks_like_challenge_page("normal article text") is False
    # the "status code"+"403" composite marker
    assert crawler._looks_like_challenge_page("returned status code 403 oops") is True


def test_title_from_html_missing_title():
    if not crawler._DEPS_AVAILABLE:
        pytest.skip("news parse deps not installed")
    assert crawler._title_from_html("<html><body>no title</body></html>") == ""


def test_ip_is_blocked_invalid_string():
    """A non-IP string is treated as blocked (line 84-86)."""
    assert crawler._ip_is_blocked("not-an-ip") is True
    assert crawler._ip_is_blocked("8.8.8.8") is False
    assert crawler._ip_is_blocked("127.0.0.1") is True


async def test_assert_public_url_dns_failure(monkeypatch):
    """getaddrinfo raising OSError → UnsafeUrlError (line 108-110)."""
    def boom(*a, **k):
        raise OSError("dns down")
    monkeypatch.setattr(crawler.socket, "getaddrinfo", boom)
    with pytest.raises(crawler.UnsafeUrlError, match="dns resolution failed"):
        await crawler._assert_public_url("https://no-such-host.example/feed")


async def test_assert_public_url_empty_resolution(monkeypatch):
    """getaddrinfo returning no addresses → rejected (line 112-113)."""
    monkeypatch.setattr(crawler.socket, "getaddrinfo", lambda *a, **k: [])
    with pytest.raises(crawler.UnsafeUrlError, match="non-public address"):
        await crawler._assert_public_url("https://empty.example/feed")


async def test_http_get_403_retry_drops_referer(monkeypatch):
    """A 403 on the first (with-Referer) hit triggers a bare retry; the second hit
    (no Referer) succeeds (line 183-184)."""
    from tests.test_news_crawler import _FakeClient, _FakeStream
    fake = _FakeClient([
        _FakeStream(403, {"content-type": "text/html"}, b"forbidden"),  # with referer
        _FakeStream(200, {"content-type": "text/html"}, b"ok body"),    # bare retry
    ])
    monkeypatch.setattr(crawler.httpx, "AsyncClient", lambda *a, **k: fake)
    status, text, ctype = await crawler._http_get("https://8.8.8.8/cdn")
    assert status == 200 and text == "ok body"
    # both hits were to the same URL (the bare retry, not a redirect)
    assert fake.requested == ["https://8.8.8.8/cdn", "https://8.8.8.8/cdn"]


async def test_http_get_too_many_redirects(monkeypatch):
    """More than _MAX_REDIRECTS hops → UnsafeUrlError (line 191)."""
    from tests.test_news_crawler import _FakeClient, _FakeStream
    # always redirect to the same public host → never terminates within the cap
    streams = [_FakeStream(302, {"location": "https://8.8.8.8/next"})
               for _ in range(crawler._MAX_REDIRECTS + 2)]
    fake = _FakeClient(streams)
    monkeypatch.setattr(crawler.httpx, "AsyncClient", lambda *a, **k: fake)
    with pytest.raises(crawler.UnsafeUrlError, match="too many redirects"):
        await crawler._http_get("https://8.8.8.8/start")


async def test_stream_capped_rejects_oversize_content_length(monkeypatch):
    """A content-length header exceeding the cap is rejected up front (line
    155-156) before streaming the body."""
    from tests.test_news_crawler import _FakeClient, _FakeStream
    monkeypatch.setattr(crawler.settings, "news_crawl_max_bytes", 10)
    fake = _FakeClient([_FakeStream(200, {"content-type": "text/html", "content-length": "9999"}, b"x")])
    monkeypatch.setattr(crawler.httpx, "AsyncClient", lambda *a, **k: fake)
    with pytest.raises(crawler.UnsafeUrlError, match="response too large"):
        await crawler._http_get("https://8.8.8.8/declared-big")


# ══════════════════════════════════════════════════════════════════════════════
# publisher.py — channel_is_admin true / false / exception
# ══════════════════════════════════════════════════════════════════════════════

async def test_channel_is_admin_true():
    class _Bot2:
        async def get_me(self):
            return SimpleNamespace(id=42)

        async def get_chat_member(self, chat_id, user_id):
            return SimpleNamespace(status="creator")

    assert await publisher.channel_is_admin(_Bot2(), -100) is True


async def test_channel_is_admin_false_when_member():
    class _Bot2:
        async def get_me(self):
            return SimpleNamespace(id=42)

        async def get_chat_member(self, chat_id, user_id):
            return SimpleNamespace(status="member")

    assert await publisher.channel_is_admin(_Bot2(), -100) is False


async def test_channel_is_admin_false_on_telegram_error():
    """A TelegramError during the admin check is caught → False (line 156-158)."""
    class _Bot2:
        async def get_me(self):
            raise TelegramError("chat not found")

        async def get_chat_member(self, chat_id, user_id):  # pragma: no cover - never reached
            return SimpleNamespace(status="administrator")

    assert await publisher.channel_is_admin(_Bot2(), -100) is False


# ══════════════════════════════════════════════════════════════════════════════
# publisher.py — post_item_to_channel transient failure branches
# ══════════════════════════════════════════════════════════════════════════════

def _pitem(**kw):
    base = dict(id=9, title_orig="Headline", body_orig="Body", url="https://news/x",
                translations={"en": {"title": "Headline", "summary": "Body"}},
                cta_url=None, cta_market_id=None, hero_image_url=None)
    base.update(kw)
    return SimpleNamespace(**base)


async def test_post_photo_transient_telegram_error_returns_none():
    """A non-BadRequest TelegramError on send_photo → None (transient, item left
    for retry) WITHOUT a text fallback (lines 175-177)."""
    class _PhotoFlaky:
        username = "TestBot"

        async def send_photo(self, **kw):
            raise NetworkError("photo upload glitch")

        async def send_message(self, **kw):  # pragma: no cover - must NOT be called
            raise AssertionError("must not fall back to text on a transient photo error")

    mid = await publisher.post_item_to_channel(
        _PhotoFlaky(), _pitem(hero_image_url="https://img/x.jpg"),
        chat_id=-100, lang="en", bot_username="TestBot")
    assert mid is None


async def test_post_text_transient_telegram_error_returns_none():
    """A non-BadRequest TelegramError on the text send → None (line 192-194)."""
    class _TextFlaky:
        username = "TestBot"

        async def send_message(self, **kw):
            raise NetworkError("send glitch")

    mid = await publisher.post_item_to_channel(
        _TextFlaky(), _pitem(), chat_id=-100, lang="en", bot_username="TestBot")
    assert mid is None


async def test_post_plain_fallback_then_fails_returns_none():
    """BadRequest on the HTML send triggers the plain-text fallback; if THAT also
    fails (TelegramError), None is returned (lines 188-191)."""
    class _BothFail:
        username = "TestBot"

        def __init__(self):
            self.calls = []

        async def send_message(self, **kw):
            self.calls.append(kw)
            if kw.get("parse_mode") == "HTML":
                raise BadRequest("can't parse entities")
            raise NetworkError("plain send also failed")

    bot = _BothFail()
    mid = await publisher.post_item_to_channel(
        bot, _pitem(), chat_id=-100, lang="en", bot_username="TestBot")
    assert mid is None
    assert len(bot.calls) == 2  # HTML attempt + plain fallback attempt
    assert "parse_mode" not in bot.calls[1]  # the fallback was plain


async def test_post_photo_success_returns_message_id():
    """Hero present + send_photo succeeds → returns the photo message id (line
    170-172). The text path is never taken."""
    bot = _Bot()
    mid = await publisher.post_item_to_channel(
        bot, _pitem(hero_image_url="https://img/x.jpg"),
        chat_id=-100, lang="en", bot_username="TestBot")
    assert mid == 101
    assert [c[0] for c in bot.calls] == ["photo"]


# ══════════════════════════════════════════════════════════════════════════════
# publisher.py — build_digest branches
# ══════════════════════════════════════════════════════════════════════════════

def test_build_digest_outcome_links_when_outcomes_and_bot():
    """An item with resolved outcomes + known bot username yields one bet deep-link
    per outcome inline, by index (nb-<id>-<index>)."""
    it = SimpleNamespace(id=3, title_orig="Mkt item", body_orig="b",
                         translations={"en": {"title": "Mkt item", "summary": "A summary."}},
                         cta_url="https://news/x", cta_market_id="0xMKT", cta_market_question="Q?",
                         cta_outcomes=[{"label": "Yes", "market_id": "0xMKT", "side": "yes", "price": 0.6},
                                       {"label": "No", "market_id": "0xMKT", "side": "no", "price": 0.4}])
    out = publisher.build_digest([it], lang="en", header="Hdr", bot_username="TestBot")
    assert "start=nb-3-0" in out and "start=nb-3-1" in out
    assert "<b>Hdr</b>" in out
    assert "not financial advice" in out  # NFA footer always appended
    assert "A summary." in out


def test_build_digest_single_link_when_no_market():
    """No resolved market → a single Trade/Open link, not the two-button row (lines
    121-125)."""
    it = SimpleNamespace(id=4, title_orig="Plain item", body_orig="b",
                         translations={"en": {"title": "Plain item", "summary": "Sum."}},
                         cta_url="https://example/article", cta_market_id=None)
    out = publisher.build_digest([it], lang="en", header="", bot_username="TestBot")
    assert "https://example/article" in out
    assert "nb-4-y" not in out  # no two-button bet links
    assert "<b>" in out  # the title is still bold
    # no header → no leading header block
    assert not out.startswith("<b></b>")


def test_build_digest_trade_label_when_market_but_no_bot():
    """A resolved market but NO bot username falls to the single-link branch with
    the 'Trade this market' label (line 116 False → 123-125 with cta_market_id)."""
    it = SimpleNamespace(id=5, title_orig="Mkt no bot", body_orig="b",
                         translations={"en": {"title": "Mkt no bot", "summary": "Sum."}},
                         cta_url="https://news/mkt", cta_market_id="0xMKT")
    out = publisher.build_digest([it], lang="en", header="H", bot_username=None)
    assert "Trade this market" in out  # market label, single link
    assert "nb-5-y" not in out


def test_build_digest_no_link_when_no_url():
    """An item with no cta_url and no plain url → just title + footer, no link
    line (line 122-125 with link falsy)."""
    it = SimpleNamespace(id=6, title_orig="No link", body_orig=None,
                         translations={}, cta_url=None, cta_market_id=None, url=None)
    out = publisher.build_digest([it], lang="en", header="H", bot_username="TestBot")
    assert "No link" in out  # falls back to title_orig
    assert "🔗" not in out  # no link emoji line
    assert "not financial advice" in out
