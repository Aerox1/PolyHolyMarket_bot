"""core.claude_text — the Claude Agent SDK news-text transport. The SDK call
(``_query``) is mocked, so no CLI is spawned and no network is hit. Also covers
the provider switch in gemini.translate_summarize_news."""

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import core.gemini as gemini
from core import claude_text
from db.models import Base, GeminiUsage
from db.repositories import gemini_usage as gemini_usage_repo


@pytest.fixture
async def sf():
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
def _have_cli(monkeypatch):
    # pretend a CLI is resolvable + generous budget (tests mock the actual call)
    monkeypatch.setattr(claude_text, "cli_path", lambda: "/fake/claude")
    monkeypatch.setattr(claude_text.settings, "gemini_weekly_budget_usd", 10.0)
    monkeypatch.setattr(claude_text.settings, "claude_text_model", "")


def _afn(value):
    async def _f(*a, **k):
        return value
    return _f


async def test_generate_json_success_charges_ledger(sf, monkeypatch):
    monkeypatch.setattr(claude_text, "_query", _afn(('{"en":{"title":"T","summary":"S"}}', 0.04)))
    async with sf() as s:
        out = await claude_text.generate_json(s, prompt="p", kind="news_text")
        await s.commit()
        assert out == '{"en":{"title":"T","summary":"S"}}'
        row = (await s.execute(select(GeminiUsage))).scalar_one()
        assert row.kind == "news_text" and row.ok is True
        assert float(row.cost_usd) == pytest.approx(0.04)


async def test_generate_json_strips_code_fence(sf, monkeypatch):
    monkeypatch.setattr(claude_text, "_query", _afn(('```json\n{"x":1}\n```', 0.01)))
    async with sf() as s:
        assert await claude_text.generate_json(s, prompt="p") == '{"x":1}'


async def test_generate_json_no_cli_returns_none(sf, monkeypatch):
    monkeypatch.setattr(claude_text, "cli_path", lambda: None)
    called = False

    async def _boom(*a, **k):
        nonlocal called
        called = True
        return ("x", 0.0)

    monkeypatch.setattr(claude_text, "_query", _boom)
    async with sf() as s:
        assert await claude_text.generate_json(s, prompt="p") is None
    assert called is False  # never invoked the SDK


async def test_generate_json_budget_gate_skips_call(sf, monkeypatch):
    # The text budget is separate from images and 0 = unlimited; set a cap > 0 and
    # pre-fill the rolling-week TEXT spend above it → the call is skipped entirely.
    monkeypatch.setattr(claude_text.settings, "news_text_weekly_budget_usd", 0.50)
    called = False

    async def _boom(*a, **k):
        nonlocal called
        called = True
        return ("x", 0.0)

    monkeypatch.setattr(claude_text, "_query", _boom)
    async with sf() as s:
        await gemini_usage_repo.record(s, category_id=None, cost_usd=1.0,
                                       model="claude-cli", ok=True, kind="news_text")
        await s.flush()
        assert await claude_text.generate_json(s, prompt="p") is None
    assert called is False


async def test_generate_json_failure_records_zero_cost(sf, monkeypatch):
    monkeypatch.setattr(claude_text, "_query", _afn((None, 0.0)))  # SDK hiccup
    async with sf() as s:
        out = await claude_text.generate_json(s, prompt="p", kind="news_text")
        await s.commit()
        assert out is None
        row = (await s.execute(select(GeminiUsage))).scalar_one()
        assert row.ok is False and float(row.cost_usd) == 0.0


# ── provider switch in gemini.translate_summarize_news ──────────────────────────

async def test_translate_routes_to_claude_when_provider_claude(sf, monkeypatch):
    monkeypatch.setattr(gemini.settings, "news_text_provider", "claude")
    seen = {}

    async def fake_generate_json(session, *, prompt, kind="news_text", category_id=None):
        seen["prompt"] = prompt
        return '{"en":{"title":"CT","summary":"CS"}}'

    # if it wrongly took the Gemini path, this would blow up
    monkeypatch.setattr(gemini, "_call_gemini_text", lambda *a, **k: (_ for _ in ()).throw(AssertionError("gemini path")))
    monkeypatch.setattr("core.claude_text.generate_json", fake_generate_json)
    async with sf() as s:
        out = await gemini.translate_summarize_news(s, title="x", body="y", target_langs=("en",))
    assert out == {"en": {"title": "CT", "summary": "CS"}}
    assert "ARTICLE TITLE" in seen["prompt"]  # reused the shared prompt builder
