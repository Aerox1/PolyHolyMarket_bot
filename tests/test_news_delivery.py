"""Phase 5 — per-user news delivery: prefs + topic follows, relevance/dedup
candidate selection, the realtime + digest jobs, and the /news settings screen."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from bot.handlers import news as news_handler
from bot.news import jobs as news_jobs
from db.engine import async_session_scope
from db.models import Bet, Category, NewsDelivered, NewsItem, User, UserNewsPrefs, UserTopicFollow
from db.repositories import news_delivery, news_prefs


async def _user(telegram_id=600, **kw):
    async with async_session_scope() as s:
        u = User(telegram_id=telegram_id, username="u", language="en", status="active", **kw)
        s.add(u)
        await s.flush()
        return u.id


async def _topic(slug="econ", **kw):
    async with async_session_scope() as s:
        c = Category(slug=slug, title="Economy", kind="news", **kw)
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


# ── prefs + topic follows ─────────────────────────────────────────────────────

async def test_prefs_defaults_and_setters():
    uid = await _user(601)
    async with async_session_scope() as s:
        p = await news_prefs.get_or_create(s, uid)
        assert p.delivery == "daily" and p.digest_hour == 9 and p.only_relevant is False
        await news_prefs.set_delivery(s, uid, "realtime")
        await news_prefs.set_digest_hour(s, uid, 30)  # clamped to 23
        assert await news_prefs.toggle_relevant(s, uid) is True
    async with async_session_scope() as s:
        p = await news_prefs.get_or_create(s, uid)
        assert p.delivery == "realtime" and p.digest_hour == 23 and p.only_relevant is True


async def test_topic_follow_toggle():
    uid = await _user(602)
    cid = await _topic("crypto")
    async with async_session_scope() as s:
        assert await news_prefs.toggle_follow(s, uid, cid) is True
    async with async_session_scope() as s:
        assert await news_prefs.followed_ids(s, uid) == {cid}
        assert await news_prefs.toggle_follow(s, uid, cid) is False
    async with async_session_scope() as s:
        assert await news_prefs.followed_ids(s, uid) == set()


async def test_list_news_topics_excludes_market_and_hidden():
    await _topic("news-topic")  # kind=news (helper default)
    async with async_session_scope() as s:
        s.add(Category(slug="mkt-only", title="Markets", kind="market"))
        s.add(Category(slug="hidden-news", title="Hidden", kind="news", hidden=True))
        await s.flush()
        topics = await news_prefs.list_news_topics(s)
    slugs = {c.slug for c in topics}
    assert "news-topic" in slugs and "mkt-only" not in slugs and "hidden-news" not in slugs


# ── candidate selection (relevance + dedup) ──────────────────────────────────

async def test_candidates_match_topic_and_market_and_exclude_delivered():
    uid = await _user(603)
    cid = await _topic("politics")
    topic_item = await _sent_item("t1", category_id=cid)
    market_item = await _sent_item("t2", cta_market_id="0xMKT")
    await _sent_item("t3")  # irrelevant
    delivered_item = await _sent_item("t4", category_id=cid)
    async with async_session_scope() as s:
        s.add(Bet(user_id=uid, market_id="0xMKT", token_id="1", outcome="YES", amount_usd=5))
        news_delivery.mark_delivered(s, uid, delivered_item, "digest")
    async with async_session_scope() as s:
        followed = {cid}
        mkts = await news_delivery.user_market_ids(s, uid)
        assert mkts == {"0xMKT"}
        got = {i.id for i in await news_delivery.candidates_for(
            s, uid, followed_ids=followed, market_ids=mkts, only_relevant=True, limit=10)}
    assert got == {topic_item, market_item}  # t3 irrelevant, t4 already delivered


async def test_candidates_only_relevant_with_no_signal_returns_empty():
    uid = await _user(604)
    await _sent_item("u1")
    async with async_session_scope() as s:
        got = await news_delivery.candidates_for(s, uid, followed_ids=set(), market_ids=set(),
                                                 only_relevant=True, limit=10)
    assert got == []


# ── delivery jobs ─────────────────────────────────────────────────────────────

class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, **kw):
        self.sent.append(kw)
        return SimpleNamespace(message_id=1)


async def _set_prefs(uid, **kw):
    async with async_session_scope() as s:
        p = await news_prefs.get_or_create(s, uid)
        for k, v in kw.items():
            setattr(p, k, v)


async def test_realtime_job_delivers_relevant_and_dedups():
    uid = await _user(605)
    cid = await _topic("macro")
    item_id = await _sent_item("r1", category_id=cid)
    await _set_prefs(uid, delivery="realtime")
    async with async_session_scope() as s:
        await news_prefs.toggle_follow(s, uid, cid)
    bot = _Bot()
    await news_jobs.news_realtime_job(SimpleNamespace(bot=bot))
    assert len(bot.sent) == 1 and "Item" in bot.sent[0]["text"]
    async with async_session_scope() as s:
        assert (await s.execute(select(func.count()).select_from(NewsDelivered))).scalar() == 1
    # second run: already delivered → no resend
    await news_jobs.news_realtime_job(SimpleNamespace(bot=bot))
    assert len(bot.sent) == 1


async def test_realtime_skips_when_not_relevant():
    uid = await _user(606)
    await _sent_item("r2")  # no topic/market match
    await _set_prefs(uid, delivery="realtime")
    bot = _Bot()
    await news_jobs.news_realtime_job(SimpleNamespace(bot=bot))
    assert bot.sent == []  # realtime is high-signal only


async def test_digest_job_respects_hour_and_once_per_day():
    uid = await _user(607)
    cid = await _topic("elections")
    await _sent_item("d1", category_id=cid)
    hour = datetime.now(timezone.utc).hour
    await _set_prefs(uid, delivery="daily", digest_hour=hour, only_relevant=False)
    bot = _Bot()
    await news_jobs.news_digest_job(SimpleNamespace(bot=bot))
    assert len(bot.sent) == 1
    async with async_session_scope() as s:
        p = await news_prefs.get_or_create(s, uid)
        assert p.last_digest_at is not None
    # same hour, already sent today → no second digest
    await news_jobs.news_digest_job(SimpleNamespace(bot=bot))
    assert len(bot.sent) == 1


async def test_digest_skips_wrong_hour():
    uid = await _user(608)
    cid = await _topic("sports")
    await _sent_item("d2", category_id=cid)
    wrong = (datetime.now(timezone.utc).hour + 3) % 24
    await _set_prefs(uid, delivery="daily", digest_hour=wrong, only_relevant=False)
    bot = _Bot()
    await news_jobs.news_digest_job(SimpleNamespace(bot=bot))
    assert bot.sent == []


async def test_quiet_hours_blocks_realtime():
    uid = await _user(609)
    cid = await _topic("world")
    await _sent_item("q1", category_id=cid)
    h = datetime.now(timezone.utc).hour
    await _set_prefs(uid, delivery="realtime")
    async with async_session_scope() as s:
        await news_prefs.toggle_follow(s, uid, cid)
        await news_prefs.set_quiet_hours(s, uid, h, (h + 1) % 24)  # window covers 'now'
    bot = _Bot()
    await news_jobs.news_realtime_job(SimpleNamespace(bot=bot))
    assert bot.sent == []


async def test_digest_ignores_quiet_hours():
    # quiet hours suppress realtime only; a scheduled digest still fires
    uid = await _user(611)
    cid = await _topic("econ-q")
    await _sent_item("dq1", category_id=cid)
    h = datetime.now(timezone.utc).hour
    await _set_prefs(uid, delivery="daily", digest_hour=h, only_relevant=False)
    async with async_session_scope() as s:
        await news_prefs.set_quiet_hours(s, uid, h, (h + 1) % 24)
    bot = _Bot()
    await news_jobs.news_digest_job(SimpleNamespace(bot=bot))
    assert len(bot.sent) == 1


def test_in_quiet_hours_logic():
    assert news_jobs._in_quiet_hours(23, 22, 7) is True   # wrap-around night
    assert news_jobs._in_quiet_hours(3, 22, 7) is True
    assert news_jobs._in_quiet_hours(12, 22, 7) is False
    assert news_jobs._in_quiet_hours(12, None, None) is False


# ── /news settings screen ─────────────────────────────────────────────────────

async def test_settings_screen_renders(monkeypatch):
    captured = {}

    async def fake_screen(update, context, *, text, reply_markup=None, **kw):
        captured["text"] = text
        captured["kb"] = reply_markup

    monkeypatch.setattr(news_handler.common, "screen", fake_screen)
    uid = await _user(610)
    ctx = SimpleNamespace(user_data={"lang": "en", "db_user_id": uid})
    update = SimpleNamespace(callback_query=None, effective_message=None)
    await news_handler.show_settings_screen(update, ctx)
    assert "News preferences" in captured["text"]
    assert captured["kb"] is not None
