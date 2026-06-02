"""News pipeline: CTA resolution, the render orchestrator, and the crawl/render
jobs. Gemini, Polymarket and the crawler are mocked — no network, no parse deps."""

from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from bot.news import cta as news_cta
from bot.news import crawler
from bot.news import jobs as news_jobs
from bot.news import render as render_mod
from bot.news.crawler import FetchedArticle
from core import gemini
from db.engine import async_session_scope
from db.models import NewsItem, NewsSource
from db.repositories import news_items as items_repo


def _afn(value):
    async def _f(*a, **k):
        return value
    return _f


# ── CTA ──────────────────────────────────────────────────────────────────────

def test_news_deeplink():
    # carries the item id (short), not the 66-char conditionId (exceeds the 64-char cap)
    assert news_cta.news_deeplink("Bot", item_id=5) == "https://t.me/Bot?start=n-5"


async def test_best_market_id_prefers_hint():
    assert await news_cta.best_market_id(title="x", hint_market_id="0xhint") == "0xhint"


async def test_best_market_id_uses_category_then_search(monkeypatch):
    monkeypatch.setattr(news_cta.markets, "category_markets", lambda slug, n: [{"id": "0xcat"}])
    monkeypatch.setattr(news_cta.markets, "search_markets", lambda q, n: [{"id": "0xsearch"}])
    assert await news_cta.best_market_id(title="x", category_tag_slug="crypto") == "0xcat"
    # no category → falls back to title search
    assert await news_cta.best_market_id(title="Fed cuts rates") == "0xsearch"


async def test_best_market_id_none_and_error_safe(monkeypatch):
    monkeypatch.setattr(news_cta.markets, "search_markets", lambda q, n: [])
    assert await news_cta.best_market_id(title="nothing") is None

    def _boom(*a, **k):
        raise RuntimeError("gamma down")

    monkeypatch.setattr(news_cta.markets, "search_markets", _boom)
    assert await news_cta.best_market_id(title="x") is None  # swallowed → no CTA


# ── render ───────────────────────────────────────────────────────────────────

async def test_render_item_success(monkeypatch):
    monkeypatch.setattr(gemini, "translate_summarize_news",
                        _afn({"en": {"title": "T", "summary": "S"}, "fa": {"title": "ت", "summary": "خ"}}))
    monkeypatch.setattr(news_cta, "best_market_id", _afn("0xmkt"))
    async with async_session_scope() as s:
        item = await items_repo.create(s, url="u1", url_hash="h1", title_orig="Fed cuts",
                                       hero_image_url="https://img/x.jpg")
        item.status = "approved"
        await render_mod.render_item(s, item, bot_username="TestBot")
        assert item.status == "ready"
        assert set(item.translations) == {"en", "fa"}
        assert item.cta_market_id == "0xmkt"
        assert item.cta_url == f"https://t.me/TestBot?start=n-{item.id}"  # item id, not the 66-char cond id
        assert item.cta_resolved_at is not None
        assert item.image_status == "ready"  # hero present


async def test_render_item_passthrough_when_translation_unavailable(monkeypatch):
    monkeypatch.setattr(gemini, "translate_summarize_news", _afn(None))  # no key / budget / egress
    monkeypatch.setattr(news_cta, "best_market_id", _afn(None))
    async with async_session_scope() as s:
        item = await items_repo.create(s, url="u2", url_hash="h2", title_orig="Headline",
                                       body_orig="Body text", lang_orig="en")
        item.status = "approved"
        await render_mod.render_item(s, item, bot_username="TestBot")
        assert item.status == "ready"
        assert item.translations == {"en": {"title": "Headline", "summary": "Body text"}}
        assert item.cta_market_id is None
        # deep-link still set (opens the item in-bot) even without a market CTA
        assert item.cta_url == f"https://t.me/TestBot?start=n-{item.id}"
        assert item.image_status == "none"  # no hero, no AI image in Phase 2


# ── jobs ─────────────────────────────────────────────────────────────────────

async def _seed_source(url="https://feed/rss", kind="rss"):
    async with async_session_scope() as s:
        src = NewsSource(name="Feed", url=url, url_hash=crawler.url_hash(url), kind=kind, enabled=True)
        s.add(src)
        await s.flush()
        return src.id


async def _count_items():
    async with async_session_scope() as s:
        return (await s.execute(select(func.count()).select_from(NewsItem))).scalar()


async def test_crawl_job_creates_and_dedups(monkeypatch):
    await _seed_source()
    arts = [
        FetchedArticle("https://x/1", crawler.url_hash("https://x/1"), "One", "body one", "en", None),
        FetchedArticle("https://x/2", crawler.url_hash("https://x/2"), "Two", "body two", None, "https://img"),
    ]
    monkeypatch.setattr(crawler, "fetch_articles", _afn(arts))
    await news_jobs.crawl_job(SimpleNamespace())
    assert await _count_items() == 2
    # second pass: same url_hashes already exist → no duplicates
    await news_jobs.crawl_job(SimpleNamespace())
    assert await _count_items() == 2
    async with async_session_scope() as s:
        src = await s.scalar(select(NewsSource))
        assert src.last_status == "ok:0"  # second pass added nothing


async def test_crawl_job_dedups_cross_source_by_title(monkeypatch):
    # same story, DIFFERENT url → must be deduped via dedup_hash (normalized title)
    await _seed_source()
    first = [FetchedArticle("https://a/1", crawler.url_hash("https://a/1"), "Big Story", "b", "en", None)]
    monkeypatch.setattr(crawler, "fetch_articles", _afn(first))
    await news_jobs.crawl_job(SimpleNamespace())
    assert await _count_items() == 1
    repost = [FetchedArticle("https://b/2", crawler.url_hash("https://b/2"), "big   story", "b2", "en", None)]
    monkeypatch.setattr(crawler, "fetch_articles", _afn(repost))
    await news_jobs.crawl_job(SimpleNamespace())
    assert await _count_items() == 1  # repost suppressed by dedup_hash


async def test_crawl_job_marks_source_error_on_failure(monkeypatch):
    await _seed_source(url="https://bad/feed")

    async def _boom(*a, **k):
        raise RuntimeError("dns fail")

    monkeypatch.setattr(crawler, "fetch_articles", _boom)
    await news_jobs.crawl_job(SimpleNamespace())
    async with async_session_scope() as s:
        src = await s.scalar(select(NewsSource))
        assert src.last_status.startswith("error")
    assert await _count_items() == 0


async def test_render_job_processes_approved(monkeypatch):
    monkeypatch.setattr(gemini, "translate_summarize_news", _afn({"en": {"title": "T", "summary": "S"}}))
    monkeypatch.setattr(news_cta, "best_market_id", _afn("0xmkt"))
    async with async_session_scope() as s:
        item = await items_repo.create(s, url="r1", url_hash="rh1", title_orig="Approved one")
        item.status = "approved"
    ctx = SimpleNamespace(bot=SimpleNamespace(username="TestBot"))
    await news_jobs.render_job(ctx)
    async with async_session_scope() as s:
        item = await s.scalar(select(NewsItem))
        assert item.status == "ready"
        assert item.cta_market_id == "0xmkt"


async def test_render_job_skips_backlog(monkeypatch):
    # backlog items are NOT rendered (only admin-approved) — guards the approval gate
    monkeypatch.setattr(gemini, "translate_summarize_news", _afn({"en": {"title": "T", "summary": "S"}}))
    monkeypatch.setattr(news_cta, "best_market_id", _afn("0xmkt"))
    async with async_session_scope() as s:
        await items_repo.create(s, url="b1", url_hash="bh1", title_orig="Still backlog")
    await news_jobs.render_job(SimpleNamespace(bot=SimpleNamespace(username="B")))
    async with async_session_scope() as s:
        item = await s.scalar(select(NewsItem))
        assert item.status == "backlog"  # untouched


# ── job registration gating ──────────────────────────────────────────────────

def _recording_app(calls):
    return SimpleNamespace(job_queue=SimpleNamespace(
        run_repeating=lambda *a, **k: calls.append(k.get("name"))))


def test_register_news_jobs_disabled(monkeypatch):
    monkeypatch.setattr(news_jobs.settings, "news_pipeline_enabled", False)
    calls: list = []
    news_jobs.register_news_jobs(_recording_app(calls))
    assert calls == []


def test_register_news_jobs_enabled(monkeypatch):
    monkeypatch.setattr(news_jobs.settings, "news_pipeline_enabled", True)
    calls: list = []
    news_jobs.register_news_jobs(_recording_app(calls))
    assert set(calls) == {"news_crawl", "news_render", "news_publish"}
