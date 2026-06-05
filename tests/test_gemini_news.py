"""core.gemini text path (news translate/summarize): budget gate, ledger
charge, and JSON parsing. All HTTP is mocked — no network, no key needed."""

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import core.gemini as gemini
from db.models import Base, GeminiUsage
from db.repositories import gemini_usage as gemini_usage_repo


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _fake_client(payload, captured):
    """A drop-in for httpx.Client that records the posted body and returns payload."""
    class _C:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            captured.update(url=url, headers=headers, body=json)
            return _FakeResp(payload)

    return _C


@pytest.fixture
async def sf():
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def budget(monkeypatch):
    """A configured key + generous budget + cheap text cost."""
    monkeypatch.setattr(gemini.settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(gemini.settings, "gemini_weekly_budget_usd", 10.0)
    monkeypatch.setattr(gemini.settings, "gemini_text_cost_usd", 0.002)
    monkeypatch.setattr(gemini.settings, "gemini_text_model", "gemini-test")


async def test_generate_text_no_key_returns_none(sf, monkeypatch):
    monkeypatch.setattr(gemini.settings, "gemini_api_key", "")
    async with sf() as s:
        assert await gemini.generate_text(s, prompt="hi") is None


async def test_generate_text_budget_gate_skips_http(sf, monkeypatch, budget):
    # text uses its OWN budget (0 = unlimited); set a cap below the per-call cost
    monkeypatch.setattr(gemini.settings, "news_text_weekly_budget_usd", 0.001)  # < 0.002 cost
    called = False

    def _boom(*a, **k):
        nonlocal called
        called = True
        raise AssertionError("HTTP must not be called when over budget")

    monkeypatch.setattr(gemini, "_call_gemini_text", _boom)
    async with sf() as s:
        assert await gemini.generate_text(s, prompt="hi") is None
        # the gate must not write ANY ledger row (not even a $0 one)
        assert (await s.execute(select(func.count()).select_from(GeminiUsage))).scalar() == 0
    assert called is False


async def test_generate_text_budget_exactly_at_limit_proceeds(sf, monkeypatch, budget):
    # spent + cost == budget is allowed (gate is strict `>`). Seed TEXT spend so the
    # next call lands exactly on the limit.
    monkeypatch.setattr(gemini.settings, "news_text_weekly_budget_usd", 10.0)
    monkeypatch.setattr(gemini.settings, "gemini_text_cost_usd", 0.002)
    monkeypatch.setattr(gemini, "_call_gemini_text", lambda *a, **k: "ok")
    async with sf() as s:
        await gemini_usage_repo.record(s, category_id=None, cost_usd=9.998,
                                       model="seed", ok=True, kind="news_text")
        await s.commit()
        assert await gemini.generate_text(s, prompt="hi") == "ok"  # 9.998 + 0.002 == 10.0


# ── the REAL _call_gemini_text (httpx mocked, function actually executed) ─────

def test_call_gemini_text_json_mode_and_multipart(monkeypatch):
    captured: dict = {}
    payload = {"candidates": [{"content": {"parts": [{"text": "hel"}, {"text": "lo"}]}}]}
    monkeypatch.setattr(gemini.httpx, "Client", _fake_client(payload, captured))
    monkeypatch.setattr(gemini.settings, "gemini_api_key", "secret-key")
    monkeypatch.setattr(gemini.settings, "gemini_text_model", "m")
    out = gemini._call_gemini_text("PROMPT", response_json=True)
    assert out == "hello"  # multi-part text concatenated
    # the JSON-mode request body shape the whole news-text feature depends on
    assert captured["body"]["generationConfig"]["responseMimeType"] == "application/json"
    assert captured["headers"]["x-goog-api-key"] == "secret-key"
    assert captured["body"]["contents"][0]["parts"][0]["text"] == "PROMPT"


def test_call_gemini_text_no_json_config_when_plain(monkeypatch):
    captured: dict = {}
    payload = {"candidates": [{"content": {"parts": [{"text": "hi"}]}}]}
    monkeypatch.setattr(gemini.httpx, "Client", _fake_client(payload, captured))
    monkeypatch.setattr(gemini.settings, "gemini_api_key", "k")
    assert gemini._call_gemini_text("p") == "hi"
    assert "generationConfig" not in captured["body"]


def test_call_gemini_text_empty_raises(monkeypatch):
    monkeypatch.setattr(gemini.httpx, "Client", _fake_client({"candidates": []}, {}))
    monkeypatch.setattr(gemini.settings, "gemini_api_key", "k")
    with pytest.raises(ValueError):
        gemini._call_gemini_text("p")


async def test_generate_text_success_charges_ledger(sf, monkeypatch, budget):
    monkeypatch.setattr(gemini, "_call_gemini_text", lambda *a, **k: "the answer")
    async with sf() as s:
        out = await gemini.generate_text(s, prompt="hi", kind="news_text")
        await s.commit()
        assert out == "the answer"
        row = (await s.execute(select(GeminiUsage))).scalar_one()
        assert row.kind == "news_text"
        assert row.ok is True
        assert float(row.cost_usd) == pytest.approx(0.002)


async def test_generate_text_failure_records_zero_cost(sf, monkeypatch, budget):
    def _raise(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(gemini, "_call_gemini_text", _raise)
    async with sf() as s:
        out = await gemini.generate_text(s, prompt="hi", kind="news_text")
        await s.commit()
        assert out is None
        row = (await s.execute(select(GeminiUsage))).scalar_one()
        assert row.ok is False
        assert float(row.cost_usd) == 0.0


async def test_weekly_spend_split_by_kind(sf):
    # the text budget and image budget gate on DISJOINT spend: image-only vs everything-else
    async with sf() as s:
        await gemini_usage_repo.record(s, category_id=None, cost_usd=0.04, model="img", ok=True, kind="image")
        await gemini_usage_repo.record(s, category_id=None, cost_usd=0.002, model="txt", ok=True, kind="news_text")
        await gemini_usage_repo.record(s, category_id=None, cost_usd=0.001, model="txt", ok=True, kind="cta_pick")
        await s.flush()
        assert await gemini_usage_repo.weekly_image_spend(s) == pytest.approx(0.04)
        assert await gemini_usage_repo.weekly_text_spend(s) == pytest.approx(0.003)
        assert await gemini_usage_repo.weekly_spend(s) == pytest.approx(0.043)


async def test_translate_summarize_parses_json(sf, monkeypatch, budget):
    payload = '{"en":{"title":"T","summary":"S"},"fa":{"title":"عنوان","summary":"خلاصه"}}'
    monkeypatch.setattr(gemini, "_call_gemini_text", lambda *a, **k: payload)
    async with sf() as s:
        out = await gemini.translate_summarize_news(s, title="x", body="y", target_langs=("en", "fa"))
    assert out == {"en": {"title": "T", "summary": "S"},
                   "fa": {"title": "عنوان", "summary": "خلاصه"}}


async def test_translate_summarize_drops_malformed_entries(sf, monkeypatch, budget):
    # "en" is missing its summary → dropped; "fa" is complete → kept.
    payload = '{"en":{"title":"T"},"fa":{"title":"F","summary":"S"}}'
    monkeypatch.setattr(gemini, "_call_gemini_text", lambda *a, **k: payload)
    async with sf() as s:
        out = await gemini.translate_summarize_news(s, title="x", body="y", target_langs=("en", "fa"))
    assert out == {"fa": {"title": "F", "summary": "S"}}


async def test_translate_summarize_non_json_returns_none(sf, monkeypatch, budget):
    monkeypatch.setattr(gemini, "_call_gemini_text", lambda *a, **k: "sorry, no JSON here")
    async with sf() as s:
        assert await gemini.translate_summarize_news(s, title="x", body="y") is None
