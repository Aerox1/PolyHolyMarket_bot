"""Tests for webapp.sync — category deck refresh + pending image generation.

External deps (Polymarket tags, Gemini, runtime config, spend ledger) are all
monkeypatched on the `sync` module — no network, no real Gemini calls. The 1.5s
inter-image spacing sleep is stubbed to a no-op so the suite stays fast.
"""

import pytest

from db.engine import async_session_scope
from db.repositories import categories as categories_repo
from webapp import sync


# ── helpers ───────────────────────────────────────────────────────────────────

def _tag(slug, *, title=None, tag_id="t", tag_slug=None, volume=100.0):
    return {
        "slug": slug,
        "title": title or slug.title(),
        "tag_id": tag_id,
        "tag_slug": tag_slug or slug,
        "volume": volume,
    }


async def _seed_visible(slug, *, volume=100.0, image_status="none"):
    """Insert one visible category via the repo, optionally fixing image_status."""
    async with async_session_scope() as s:
        cat = await categories_repo.upsert_from_tag(
            s, slug=slug, title=slug.title(), tag_id="t", tag_slug=slug, volume=volume,
        )
        cat.image_status = image_status  # default from upsert is "none" already
        cat.hidden = False
    async with async_session_scope() as s:
        return (await categories_repo.get_by_slug(s, slug)).id


# ── sync_categories ─────────────────────────────────────────────────────────────

async def test_sync_categories_upserts_and_returns_count(monkeypatch):
    tags = [_tag("politics", volume=900.0), _tag("sports", volume=500.0),
            _tag("crypto", volume=200.0)]
    # top_categories is plain (called via asyncio.to_thread) — monkeypatch the function.
    monkeypatch.setattr(sync.markets, "top_categories", lambda limit: tags)

    n = await sync.sync_categories(limit=10)

    assert n == 3
    async with async_session_scope() as s:
        slugs = {c.slug for c in await categories_repo.list_visible(s)}
        assert {"politics", "sports", "crypto"} <= slugs
        pol = await categories_repo.get_by_slug(s, "politics")
        assert pol.title == "Politics"
        assert float(pol.volume) == pytest.approx(900.0)
        assert pol.tag_slug == "politics"


async def test_sync_categories_empty_list(monkeypatch):
    monkeypatch.setattr(sync.markets, "top_categories", lambda limit: [])
    n = await sync.sync_categories()
    assert n == 0
    async with async_session_scope() as s:
        assert await categories_repo.list_visible(s) == []


async def test_sync_categories_updates_existing_row(monkeypatch):
    # First sync seeds the row; second sync with new title/volume must update it.
    monkeypatch.setattr(sync.markets, "top_categories",
                        lambda limit: [_tag("politics", title="Old", volume=1.0)])
    await sync.sync_categories()
    monkeypatch.setattr(sync.markets, "top_categories",
                        lambda limit: [_tag("politics", title="New", volume=2.0)])
    n = await sync.sync_categories()

    assert n == 1
    async with async_session_scope() as s:
        rows = await categories_repo.list_visible(s)
        pol = [c for c in rows if c.slug == "politics"]
        assert len(pol) == 1  # upsert by slug — no duplicate
        assert pol[0].title == "New"
        assert float(pol[0].volume) == pytest.approx(2.0)


# ── generate_pending_images ─────────────────────────────────────────────────────

async def test_generate_pending_images_happy_path(monkeypatch):
    for slug in ("politics", "sports", "crypto"):
        await _seed_visible(slug)

    # Stub the 1.5s spacing wait + external deps.
    async def _no_sleep(*a, **k):
        pass
    monkeypatch.setattr(sync.asyncio, "sleep", _no_sleep)

    calls = []

    async def _gen(session, cat):
        calls.append(cat.slug)
        return f"/static/img/{cat.slug}.png"

    monkeypatch.setattr(sync.gemini, "generate_category_image", _gen)
    # Budget comfortably above 3 * cost; zero already spent.
    async def _budget(session, key, default):
        return 100.0
    async def _spend(session):
        return 0.0
    monkeypatch.setattr(sync.appconfig, "get_float", _budget)
    monkeypatch.setattr(sync.gemini_usage, "weekly_spend", _spend)

    n = await sync.generate_pending_images(max_images=30)

    assert n == 3
    assert len(calls) == 3  # one generate call per pending category


async def test_generate_pending_images_budget_stop_early(monkeypatch):
    await _seed_visible("politics")
    await _seed_visible("sports")

    async def _no_sleep(*a, **k):
        pass
    monkeypatch.setattr(sync.asyncio, "sleep", _no_sleep)

    calls = []

    async def _gen(session, cat):
        calls.append(cat.slug)
        return "/x.png"

    monkeypatch.setattr(sync.gemini, "generate_category_image", _gen)
    # weekly_spend + cost > budget on the FIRST iteration -> break before any gen.
    budget = 1.0
    async def _budget(session, key, default):
        return budget
    async def _spend(session):
        return budget  # spend == budget, so spend + cost > budget
    monkeypatch.setattr(sync.appconfig, "get_float", _budget)
    monkeypatch.setattr(sync.gemini_usage, "weekly_spend", _spend)

    n = await sync.generate_pending_images()

    assert n == 0
    assert calls == []  # stopped before any generate call


async def test_generate_pending_images_transient_failure_skipped(monkeypatch):
    # 3 pending; the middle one returns None (transient) and must be skipped,
    # while the loop continues and counts the others.
    for slug, vol in (("politics", 300.0), ("sports", 200.0), ("crypto", 100.0)):
        await _seed_visible(slug, volume=vol)

    async def _no_sleep(*a, **k):
        pass
    monkeypatch.setattr(sync.asyncio, "sleep", _no_sleep)

    calls = []

    async def _gen(session, cat):
        calls.append(cat.slug)
        return None if cat.slug == "sports" else f"/{cat.slug}.png"

    monkeypatch.setattr(sync.gemini, "generate_category_image", _gen)
    async def _budget(session, key, default):
        return 100.0
    async def _spend(session):
        return 0.0
    monkeypatch.setattr(sync.appconfig, "get_float", _budget)
    monkeypatch.setattr(sync.gemini_usage, "weekly_spend", _spend)

    n = await sync.generate_pending_images()

    assert len(calls) == 3      # all three attempted (failure does not halt)
    assert n == 2               # only successful generations counted


async def test_generate_pending_images_no_pending(monkeypatch):
    # No categories at all -> nothing to do, returns 0, no gen calls.
    async def _no_sleep(*a, **k):
        pass
    monkeypatch.setattr(sync.asyncio, "sleep", _no_sleep)

    calls = []

    async def _gen(session, cat):
        calls.append(cat.slug)
        return "/x.png"

    monkeypatch.setattr(sync.gemini, "generate_category_image", _gen)
    async def _budget(session, key, default):
        return 100.0
    async def _spend(session):
        return 0.0
    monkeypatch.setattr(sync.appconfig, "get_float", _budget)
    monkeypatch.setattr(sync.gemini_usage, "weekly_spend", _spend)

    n = await sync.generate_pending_images()
    assert n == 0
    assert calls == []


async def test_generate_pending_images_hidden_and_ready_excluded(monkeypatch):
    # "ready" image + hidden category are not eligible (needing_images filters them).
    await _seed_visible("politics", image_status="ready")
    async with async_session_scope() as s:
        cat = await categories_repo.upsert_from_tag(
            s, slug="secret", title="Secret", tag_id="t", tag_slug="secret", volume=50.0)
        cat.hidden = True
        cat.image_status = "none"
    await _seed_visible("sports", image_status="none")  # only eligible one

    async def _no_sleep(*a, **k):
        pass
    monkeypatch.setattr(sync.asyncio, "sleep", _no_sleep)

    calls = []

    async def _gen(session, cat):
        calls.append(cat.slug)
        return f"/{cat.slug}.png"

    monkeypatch.setattr(sync.gemini, "generate_category_image", _gen)
    async def _budget(session, key, default):
        return 100.0
    async def _spend(session):
        return 0.0
    monkeypatch.setattr(sync.appconfig, "get_float", _budget)
    monkeypatch.setattr(sync.gemini_usage, "weekly_spend", _spend)

    n = await sync.generate_pending_images()
    assert calls == ["sports"]  # hidden + ready skipped by the query
    assert n == 1
