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


@pytest.fixture(autouse=True)
def _stub_events(monkeypatch):
    # resolve_cta folds search_events + trending_events; stub both to [] by default so
    # CTA tests stay hermetic (individual tests override what they need).
    monkeypatch.setattr(news_cta.markets, "search_events", lambda q, n=20: [])
    monkeypatch.setattr(news_cta.markets, "trending_events", lambda n=40: [])


def _ev(title, mks):
    return {"title": title, "markets": mks}


def _mk(cond, question, yes, no, *, group=None, closed=False):
    m = {"conditionId": cond, "question": question, "outcomes": '["Yes","No"]',
         "clobTokenIds": f'["{cond}-y","{cond}-n"]', "outcomePrices": f'["{yes}","{no}"]',
         "closed": closed, "active": True, "volume24hr": "100"}
    if group is not None:
        m["groupItemTitle"] = group
    return m


# ── CTA: deep links ──────────────────────────────────────────────────────────

def test_news_deeplink():
    # carries the item id (short), not the 66-char conditionId (exceeds the 64-char cap)
    assert news_cta.news_deeplink("Bot", item_id=5) == "https://t.me/Bot?start=n-5"


def test_bet_deeplink_is_index_based():
    assert news_cta.bet_deeplink("Bot", item_id=42, index=0) == "https://t.me/Bot?start=nb-42-0"
    assert news_cta.bet_deeplink("Bot", item_id=42, index=2) == "https://t.me/Bot?start=nb-42-2"


# ── resolve_cta (event-aware, dynamic outcomes) ────────────────────────────────

async def test_resolve_cta_hint_is_binary(monkeypatch):
    monkeypatch.setattr(news_cta.markets, "get_market",
                        lambda cid: {"id": cid, "question": "Q?", "yes_price": 0.6, "no_price": 0.4})
    cta = await news_cta.resolve_cta(title="x", hint_market_id="0xhint")
    assert cta["market_id"] == "0xhint" and {o["side"] for o in cta["outcomes"]} == {"yes", "no"}


async def test_resolve_cta_gates_unrelated(monkeypatch):
    # the World-Cup false-positive must be REJECTED for an unrelated headline
    monkeypatch.setattr(news_cta.markets, "search_events", lambda q, n=20: [
        _ev("World Cup 2026", [_mk("0xwc", "Will Mexico win the 2026 FIFA World Cup?", "0.2", "0.8")])])
    assert await news_cta.resolve_cta(title="Celine Dion heartbroken by singer's death") is None


async def test_resolve_cta_binary_event_yes_no(monkeypatch):
    monkeypatch.setattr(news_cta.markets, "search_events", lambda q, n=20: [
        _ev("US–Iran", [_mk("0xiran", "Will the US and Iran reach a peace deal by June?", "0.12", "0.88")])])
    cta = await news_cta.resolve_cta(title="US and Iran intensify attacks, peace deal in doubt")
    assert cta and cta["market_id"] == "0xiran"
    assert [o["label"] for o in cta["outcomes"]] == ["Yes", "No"]
    assert cta["outcomes"][0]["side"] == "yes" and cta["outcomes"][0]["price"] == 0.12
    assert "peace deal" in cta["question"]   # binary → the specific market question


async def test_resolve_cta_multi_outcome_candidates(monkeypatch):
    ev = _ev("Iowa Governor Election Winner", [
        _mk("0xdem", "Will the Democrats win the Iowa governor race?", "0.62", "0.38", group="Democrats"),
        _mk("0xrep", "Will the Republicans win the Iowa governor race?", "0.36", "0.64", group="Republicans"),
        _mk("0xind", "Will an independent win the Iowa governor race?", "0.02", "0.98", group="Independent"),
    ])
    monkeypatch.setattr(news_cta.markets, "search_events", lambda q, n=20: [ev])
    cta = await news_cta.resolve_cta(title="Democrats eye the Iowa governor race in 2026")
    labels = [o["label"] for o in cta["outcomes"]]
    assert labels[:2] == ["Democrats", "Republicans"]            # sorted by probability
    assert all(o["side"] == "yes" for o in cta["outcomes"])       # each = buy YES on its sub-market
    assert cta["outcomes"][0]["market_id"] == "0xdem"
    assert cta["question"] == "Iowa Governor Election Winner"     # multi → event title


async def test_resolve_cta_matches_trending_event(monkeypatch):
    monkeypatch.setattr(news_cta.markets, "search_events", lambda q, n=20: [])
    monkeypatch.setattr(news_cta.markets, "trending_events", lambda n=40: [
        _ev("Bitcoin price", [_mk("0xbtc", "Will Bitcoin close above $100k in 2026?", "0.3", "0.7")])])
    cta = await news_cta.resolve_cta(title="Bitcoin surges toward the $100k milestone")
    assert cta and cta["market_id"] == "0xbtc"


async def test_resolve_cta_dedups_multi_outcome(monkeypatch):
    # Gamma can repeat a sub-market in event.markets; outcomes must dedup by market_id
    ev = _ev("Ohio Senate Election Winner", [
        _mk("0xa", "Will Democrats win the Ohio Senate election?", "0.5", "0.5", group="Democrats"),
        _mk("0xa", "Will Democrats win the Ohio Senate election? (dup)", "0.5", "0.5", group="Democrats"),
        _mk("0xb", "Will Republicans win the Ohio Senate election?", "0.4", "0.6", group="Republicans"),
    ])
    monkeypatch.setattr(news_cta.markets, "search_events", lambda q, n=20: [ev])
    cta = await news_cta.resolve_cta(title="Ohio Senate election race tightens")
    assert [o["market_id"] for o in cta["outcomes"]] == ["0xa", "0xb"]  # deduped, one per market


async def test_resolve_cta_error_safe(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("gamma down")
    monkeypatch.setattr(news_cta.markets, "search_events", _boom)
    assert await news_cta.resolve_cta(title="anything") is None  # swallowed → no CTA


# ── trending_matches (auto-approval gate) ──────────────────────────────────────

async def test_trending_matches_filters_by_relevance(monkeypatch):
    monkeypatch.setattr(news_cta.markets, "trending_events", lambda n=40: [
        _ev("Fed June rate decision",
            [_mk("0xfed", "Will the Fed hold interest rates in June?", "0.7", "0.3")])])
    matched = await news_cta.trending_matches([
        (1, "Fed holds interest rates steady in June"),  # ≥2 shared significant words
        (2, "Celebrity bakery opens downtown")])         # unrelated
    assert matched == {1}


async def test_trending_matches_empty_and_error(monkeypatch):
    assert await news_cta.trending_matches([]) == set()           # no candidates
    assert await news_cta.trending_matches([(1, "x")]) == set()   # stub → [] events
    def _boom(n=40):
        raise RuntimeError("gamma down")
    monkeypatch.setattr(news_cta.markets, "trending_events", _boom)
    assert await news_cta.trending_matches([(1, "Fed rates June")]) == set()  # error → no matches


# ── render ───────────────────────────────────────────────────────────────────

async def test_render_item_success(monkeypatch):
    monkeypatch.setattr(gemini, "translate_summarize_news",
                        _afn({"en": {"title": "T", "summary": "S"}, "fa": {"title": "ت", "summary": "خ"}}))
    monkeypatch.setattr(news_cta, "resolve_cta", _afn({
        "market_id": "0xmkt", "question": "Will the Fed cut rates?",
        "outcomes": [{"label": "Yes", "market_id": "0xmkt", "side": "yes", "price": 0.5},
                     {"label": "No", "market_id": "0xmkt", "side": "no", "price": 0.5}]}))
    async with async_session_scope() as s:
        item = await items_repo.create(s, url="u1", url_hash="h1", title_orig="Fed cuts",
                                       hero_image_url="https://img/x.jpg")
        item.status = "approved"
        await render_mod.render_item(s, item, bot_username="TestBot")
        assert item.status == "ready"
        assert set(item.translations) == {"en", "fa"}
        assert item.cta_market_id == "0xmkt"
        assert item.cta_market_question == "Will the Fed cut rates?"
        assert item.cta_outcomes and len(item.cta_outcomes) == 2
        assert item.cta_url == f"https://t.me/TestBot?start=n-{item.id}"
        assert item.cta_resolved_at is not None
        assert item.image_status == "ready"  # hero present


async def test_render_item_no_market_renders_dormant(monkeypatch):
    # No relevant market: the item still renders to 'ready' (translation done,
    # deep-link set) but carries no cta_market_id/outcomes. The publish job withholds
    # it while news_require_market is on (bet-relevant only) — see the gating test.
    monkeypatch.setattr(gemini, "translate_summarize_news", _afn(None))  # no key / budget / egress
    monkeypatch.setattr(news_cta, "resolve_cta", _afn(None))
    async with async_session_scope() as s:
        item = await items_repo.create(s, url="u2", url_hash="h2", title_orig="Headline",
                                       body_orig="Body text", lang_orig="en")
        item.status = "approved"
        await render_mod.render_item(s, item, bot_username="TestBot")
        assert item.status == "ready"
        assert item.translations == {"en": {"title": "Headline", "summary": "Body text"}}
        assert item.cta_market_id is None and item.cta_market_question is None and item.cta_outcomes is None
        assert item.cta_url == f"https://t.me/TestBot?start=n-{item.id}"
        assert item.image_status == "none"  # no hero, no AI image in Phase 2


def test_clip_summary_ends_on_sentence_boundary():
    # the TikTok bug: a long body must not stop mid-sentence ('… merchandise
    # through'). _clip_summary ends on the last full sentence within the cap.
    body = ("TikTok announced a new app on Wednesday. It lets fans engage and "
            "explore trending videos. Users can earn Stars for activities. These "
            "benefits include official merchandise through partners and more and "
            + "x" * 600)  # long tail forces a clip
    out = render_mod._clip_summary(body, limit=200)
    assert len(out) <= 200
    assert out.endswith(".")               # clean sentence end, not mid-word
    assert "xxxx" not in out               # the long tail was dropped


def test_clip_summary_word_boundary_fallback_and_short_passthrough():
    # no sentence boundary near the cap → word boundary + ellipsis (never mid-word)
    out = render_mod._clip_summary("alpha beta gamma delta epsilon zeta", limit=18)
    assert out.endswith("…") and " " in out and not out.rstrip("…").endswith("delt")
    # already short → returned untouched (no spurious ellipsis)
    assert render_mod._clip_summary("Short body.", limit=600) == "Short body."
    assert render_mod._clip_summary("", limit=600) == ""


async def test_render_cta_stable_across_rerender(monkeypatch):
    # Re-rendering must NOT re-resolve / reorder the CTA once resolved — the posted
    # channel buttons deep-link by index and must keep pointing at the same outcome.
    calls = {"n": 0}

    async def once(*a, **k):
        calls["n"] += 1
        return {"market_id": "0xm", "question": "Q?",
                "outcomes": [{"label": "Yes", "market_id": "0xm", "side": "yes", "price": 0.5},
                             {"label": "No", "market_id": "0xm", "side": "no", "price": 0.5}]}

    monkeypatch.setattr(gemini, "translate_summarize_news", _afn(None))
    monkeypatch.setattr(news_cta, "resolve_cta", once)
    async with async_session_scope() as s:
        item = await items_repo.create(s, url="rr", url_hash="rrh", title_orig="T")
        item.status = "approved"
        await render_mod.render_item(s, item, bot_username="B")
        assert item.cta_market_id == "0xm" and item.cta_resolved_at is not None
        item.status = "approved"  # admin re-approve / re-render
        await render_mod.render_item(s, item, bot_username="B")
    assert calls["n"] == 1  # resolved exactly once → stable across re-render


def test_caption_and_digest_show_dynamic_outcomes():
    # The post makes the wager explicit (market question) and offers a button per
    # outcome with live odds, deep-linked by INDEX (nb-<id>-<index>).
    from bot.news import publisher
    outs = [{"label": "Yes", "market_id": "0xMKT", "side": "yes", "price": 0.12},
            {"label": "No", "market_id": "0xMKT", "side": "no", "price": 0.88}]
    snap = SimpleNamespace(id=7, title_orig="Big news", body_orig="b", url="https://n/x",
                           translations={"en": {"title": "Big news", "summary": "s"}},
                           hero_image_url=None, cta_url="https://t.me/B?start=n-7",
                           cta_market_id="0xMKT", cta_market_question="Will X happen by July?",
                           cta_outcomes=outs)
    cap = publisher.build_caption(snap, lang="en", cap=4096)
    assert "Will X happen by July?" in cap and "📊" in cap
    dig = publisher.build_digest([snap], lang="en", header="H", bot_username="B")
    assert "Will X happen by July?" in dig and "nb-7-0" in dig and "nb-7-1" in dig
    kb = publisher.build_keyboard(snap, bot_username="B", lang="en")
    texts = [b.text for row in kb.inline_keyboard for b in row]
    assert any("12%" in t for t in texts) and any("88%" in t for t in texts)


def test_caption_and_digest_multi_outcome_buttons():
    from bot.news import publisher
    outs = [{"label": "Democrats", "market_id": "0xD", "side": "yes", "price": 0.62},
            {"label": "Republicans", "market_id": "0xR", "side": "yes", "price": 0.36}]
    snap = SimpleNamespace(id=9, title_orig="Iowa race", body_orig="b", url="https://n/x",
                           translations={"en": {"title": "Iowa race", "summary": "s"}},
                           hero_image_url=None, cta_url=None,
                           cta_market_id="0xD", cta_market_question="Iowa Governor Election Winner",
                           cta_outcomes=outs)
    kb = publisher.build_keyboard(snap, bot_username="B", lang="en")
    texts = [b.text for row in kb.inline_keyboard for b in row]
    urls = [b.url for row in kb.inline_keyboard for b in row]
    assert any("Democrats" in t and "62%" in t for t in texts)
    assert any("Republicans" in t and "36%" in t for t in texts)
    assert "https://t.me/B?start=nb-9-0" in urls and "https://t.me/B?start=nb-9-1" in urls


async def test_ready_to_publish_gates_on_matched_market():
    # bet-relevant only: only items WITH a matched market are publishable; with the
    # flag off, all ready items publish (CTA-less ones get a plain link).
    async with async_session_scope() as s:
        m = await items_repo.create(s, url="m", url_hash="hm", title_orig="With market")
        m.status = "ready"; m.cta_market_id = "0xMKT"
        n = await items_repo.create(s, url="n", url_hash="hn", title_orig="No market")
        n.status = "ready"  # rendered but no market
    async with async_session_scope() as s:
        gated = await items_repo.ready_to_publish(s, require_market=True)
        ungated = await items_repo.ready_to_publish(s, require_market=False)
    assert [i.cta_market_id for i in gated] == ["0xMKT"]  # only the matched one
    assert len(ungated) == 2                              # both when flag off


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
    monkeypatch.setattr(news_cta, "resolve_cta", _afn({"market_id": "0xmkt", "question": "Q?", "outcomes": [{"label": "Yes", "market_id": "0xmkt", "side": "yes", "price": 0.5}, {"label": "No", "market_id": "0xmkt", "side": "no", "price": 0.5}]}))
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
    monkeypatch.setattr(news_cta, "resolve_cta", _afn({"market_id": "0xmkt", "question": "Q?", "outcomes": [{"label": "Yes", "market_id": "0xmkt", "side": "yes", "price": 0.5}, {"label": "No", "market_id": "0xmkt", "side": "no", "price": 0.5}]}))
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
    # the bet-intent reaper registers regardless (intents can be created via an
    # nb- deep-link even with the pipeline off); the crawl/render/publish jobs do not.
    monkeypatch.setattr(news_jobs.settings, "news_pipeline_enabled", False)
    calls: list = []
    news_jobs.register_news_jobs(_recording_app(calls))
    assert calls == ["news_intents_cleanup"]


def test_register_news_jobs_enabled(monkeypatch):
    monkeypatch.setattr(news_jobs.settings, "news_pipeline_enabled", True)
    calls: list = []
    news_jobs.register_news_jobs(_recording_app(calls))
    assert set(calls) == {"news_crawl", "news_render", "news_publish", "news_realtime",
                          "news_digest", "news_intents_cleanup"}
