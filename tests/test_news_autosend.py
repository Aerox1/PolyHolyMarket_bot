"""Autosend wiring: items_repo.auto_approve_ids / approve_ids + crawl_job's two
auto-approval paths — TRENDING-market match (on by default, the bet-relevant
autosend) and top-N by SCORE (off by default). Feeds + Gamma are mocked; no
network. See bot/news/jobs.py crawl_job + db/repositories/news_items.py."""

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from bot.news import crawler
from bot.news import cta as news_cta
from bot.news import jobs as news_jobs
from db.engine import async_session_scope
from db.models import NewsItem, NewsSource
from db.repositories import appconfig
from db.repositories import news_items as items_repo


@pytest.fixture(autouse=True)
def _stub_events(monkeypatch):
    # crawl_job now folds a trending-market match into auto-approval; stub the Gamma
    # calls to [] by default so every test stays hermetic (matching tests override).
    monkeypatch.setattr(news_cta.markets, "trending_events", lambda n=40: [])
    monkeypatch.setattr(news_cta.markets, "search_events", lambda q, n=20: [])


def _art(title, url, score):
    return SimpleNamespace(title=title, url=url, url_hash=f"h:{url}", body="b",
                           lang="en", hero_image=None, _score=score)


def _ev(title, mks):
    return {"title": title, "markets": mks}


def _mk(cond, question, yes, no):
    return {"conditionId": cond, "question": question, "outcomes": '["Yes","No"]',
            "clobTokenIds": f'["{cond}-y","{cond}-n"]', "outcomePrices": f'["{yes}","{no}"]',
            "closed": False, "active": True, "volume24hr": "100"}


async def _seed_source(url="https://feed.example/rss"):
    async with async_session_scope() as s:
        src = NewsSource(name="Feed", url=url, url_hash=f"uh:{url}", kind="auto", enabled=True)
        s.add(src)
        await s.flush()
        return src.id


async def _statuses():
    async with async_session_scope() as s:
        return {r.title_orig: r.status for r in await s.scalars(select(NewsItem))}


# ── auto_approve_ids ─────────────────────────────────────────────────────────────

async def test_auto_approve_ids_promotes_top_by_score():
    async with async_session_scope() as s:
        ids = []
        for title, sc in [("a", 0.1), ("b", 0.9), ("c", 0.5)]:
            it = await items_repo.create(s, url=f"u/{title}", url_hash=f"h/{title}",
                                         title_orig=title, score=sc)
            ids.append(it.id)
    async with async_session_scope() as s:
        assert await items_repo.auto_approve_ids(s, ids, 2) == 2  # only the top 2
    statuses = await _statuses()
    assert statuses["b"] == "approved" and statuses["c"] == "approved"  # 0.9, 0.5
    assert statuses["a"] == "backlog"                                   # 0.1 stays


async def test_auto_approve_ids_guards():
    async with async_session_scope() as s:
        assert await items_repo.auto_approve_ids(s, [], 5) == 0       # no ids
        assert await items_repo.auto_approve_ids(s, [999999], 5) == 0  # unknown id
        it = await items_repo.create(s, url="u", url_hash="h", title_orig="x", score=1.0)
        assert await items_repo.auto_approve_ids(s, [it.id], 0) == 0   # limit 0


async def test_auto_approve_ids_only_touches_backlog():
    async with async_session_scope() as s:
        it = await items_repo.create(s, url="u", url_hash="h", title_orig="x", score=1.0)
        it.status = "sent"  # already published — must not be re-approved
        iid = it.id
    async with async_session_scope() as s:
        assert await items_repo.auto_approve_ids(s, [iid], 5) == 0
    assert (await _statuses())["x"] == "sent"


# ── crawl_job autosend integration ───────────────────────────────────────────────

def _mock_feed(monkeypatch, arts):
    async def fake_fetch(url, kind="auto", limit=10):
        return arts
    monkeypatch.setattr(crawler, "fetch_articles", fake_fetch)
    monkeypatch.setattr(crawler, "score_article", lambda a: a._score)


async def test_crawl_autosend_on_promotes_top_n(monkeypatch):
    await _seed_source()
    _mock_feed(monkeypatch, [_art("low", "https://x/1", 0.1),
                             _art("high", "https://x/2", 0.9),
                             _art("mid", "https://x/3", 0.5)])
    async with async_session_scope() as s:
        await appconfig.set_(s, news_jobs.NEWS_AUTOSEND_KEY, "1")
        await appconfig.set_(s, news_jobs.NEWS_TOP_N_KEY, "2")

    await news_jobs.crawl_job(SimpleNamespace())

    statuses = await _statuses()
    assert statuses == {"high": "approved", "mid": "approved", "low": "backlog"}


async def test_crawl_autosend_off_keeps_everything_backlog(monkeypatch):
    await _seed_source()
    _mock_feed(monkeypatch, [_art("a", "https://x/1", 0.9), _art("b", "https://x/2", 0.5)])
    # news_autosend unset → default off AND no trending match (stub → []): items
    # must wait for manual approval.
    await news_jobs.crawl_job(SimpleNamespace())
    assert set((await _statuses()).values()) == {"backlog"}


# ── approve_ids (no score cap, backlog-only) ─────────────────────────────────────

async def test_approve_ids_promotes_all_backlog():
    async with async_session_scope() as s:
        ids = [(await items_repo.create(s, url=f"u/{t}", url_hash=f"h/{t}", title_orig=t)).id
               for t in ("a", "b", "c")]
    async with async_session_scope() as s:
        assert await items_repo.approve_ids(s, ids) == 3  # ALL of them, no cap
    assert set((await _statuses()).values()) == {"approved"}


async def test_approve_ids_guards_and_backlog_only():
    async with async_session_scope() as s:
        assert await items_repo.approve_ids(s, []) == 0          # no ids
        assert await items_repo.approve_ids(s, [999999]) == 0    # unknown id
        it = await items_repo.create(s, url="u", url_hash="h", title_orig="x")
        it.status = "sent"  # already published — must not be re-approved
        iid = it.id
    async with async_session_scope() as s:
        assert await items_repo.approve_ids(s, [iid]) == 0
    assert (await _statuses())["x"] == "sent"


# ── crawl_job trending auto-approve (on by default) ──────────────────────────────

async def test_crawl_autoapprove_trending_matches(monkeypatch):
    """A fresh item matching a trending market is auto-approved even with autosend
    OFF; an unrelated item stays in backlog. (The user's bet-relevant autosend.)"""
    await _seed_source()
    _mock_feed(monkeypatch, [
        _art("Fed holds interest rates steady at June meeting", "https://x/1", 0.1),
        _art("Local bakery wins a dessert contest", "https://x/2", 0.9)])
    monkeypatch.setattr(news_cta.markets, "trending_events", lambda n=40: [
        _ev("Fed June rate decision",
            [_mk("0xfed", "Will the Fed hold interest rates in June?", "0.7", "0.3")])])
    # news_autosend stays OFF; trending auto-approve defaults ON
    await news_jobs.crawl_job(SimpleNamespace())
    statuses = await _statuses()
    assert statuses["Fed holds interest rates steady at June meeting"] == "approved"
    assert statuses["Local bakery wins a dessert contest"] == "backlog"


async def test_crawl_autoapprove_trending_can_be_disabled(monkeypatch):
    await _seed_source()
    _mock_feed(monkeypatch, [_art("Fed holds interest rates in June", "https://x/1", 0.5)])
    monkeypatch.setattr(news_cta.markets, "trending_events", lambda n=40: [
        _ev("Fed", [_mk("0xfed", "Will the Fed hold interest rates in June?", "0.7", "0.3")])])
    async with async_session_scope() as s:
        await appconfig.set_(s, news_jobs.NEWS_AUTOAPPROVE_TRENDING_KEY, "0")
    await news_jobs.crawl_job(SimpleNamespace())
    assert set((await _statuses()).values()) == {"backlog"}
