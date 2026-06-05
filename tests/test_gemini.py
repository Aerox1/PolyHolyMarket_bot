"""core.gemini IMAGE path + welcome banner: cards_dir resolution, prompt
builder, the real _call_gemini_image (httpx mocked), disk save, budget gate,
ledger charging, category status flips, and the welcome-banner helpers.

All HTTP is mocked — no network, no key needed. The text path is covered by
tests/test_gemini_news.py; this file complements it on the image surface.
"""

import base64

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import core.gemini as gemini
from db.models import AppConfig, Base, Category, GeminiUsage
from db.repositories import gemini_usage as gemini_usage_repo


# ── shared fakes (mirror tests/test_gemini_news.py) ──────────────────────────

class _FakeResp:
    def __init__(self, payload, *, status_exc=None):
        self._payload = payload
        self._status_exc = status_exc

    def raise_for_status(self):
        if self._status_exc is not None:
            raise self._status_exc

    def json(self):
        return self._payload


def _fake_client(payload, captured=None, *, status_exc=None):
    """Drop-in for httpx.Client: records the posted request, returns payload."""
    captured = captured if captured is not None else {}

    class _C:
        def __init__(self, *a, **k):
            captured.setdefault("init_kwargs", k)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            captured.update(url=url, headers=headers, body=json)
            return _FakeResp(payload, status_exc=status_exc)

    return _C


# A tiny valid PNG-ish blob, base64-encoded the way Gemini returns inlineData.
_RAW_IMG = b"\x89PNG\r\n\x1a\nFAKEIMAGEBYTES"
_B64_IMG = base64.b64encode(_RAW_IMG).decode()


def _image_payload(*, mime="image/png", b64=_B64_IMG, key="inlineData", mime_key="mimeType"):
    return {"candidates": [{"content": {"parts": [{key: {mime_key: mime, "data": b64}}]}}]}


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
async def sf():
    """Isolated async sessionmaker on an in-memory sqlite (pattern b)."""
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def cards(tmp_path, monkeypatch):
    """Point cards_dir at an absolute tmp dir so _save writes nowhere real."""
    monkeypatch.setattr(gemini.settings, "cards_dir", str(tmp_path))
    return tmp_path


@pytest.fixture
def budget(monkeypatch):
    """A configured key + generous budget + known image cost/model."""
    monkeypatch.setattr(gemini.settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(gemini.settings, "gemini_weekly_budget_usd", 10.0)
    monkeypatch.setattr(gemini.settings, "gemini_image_cost_usd", 0.04)
    monkeypatch.setattr(gemini.settings, "gemini_image_model", "img-test")


# ── cards_dir + build_prompt + _save (pure helpers) ─────────────────────────

def test_cards_dir_absolute_is_used_and_created(tmp_path, monkeypatch):
    target = tmp_path / "nested" / "cards"
    monkeypatch.setattr(gemini.settings, "cards_dir", str(target))
    p = gemini.cards_dir()
    assert p == target
    assert p.is_dir()  # mkdir(parents=True) ran


def test_cards_dir_relative_anchored_to_repo_root(monkeypatch):
    monkeypatch.setattr(gemini.settings, "cards_dir", "data/cards")
    p = gemini.cards_dir()
    assert p.is_absolute()
    # relative paths are anchored to the repo root (parent.parent of core/gemini.py)
    assert p.name == "cards" and p.parent.name == "data"


def test_build_prompt_embeds_title_and_forbids_text():
    out = gemini.build_prompt("Trump vs Biden")
    assert "Trump vs Biden" in out
    # the contract that keeps baked-in text out of generated cards
    assert "NO text" in out


def test_save_writes_bytes_and_returns_served_path(cards):
    served = gemini._save("myslug", _RAW_IMG, "image/jpeg")
    assert served == "/cards/myslug.jpg"  # jpeg → jpg per _MIME_EXT
    assert (cards / "myslug.jpg").read_bytes() == _RAW_IMG


def test_save_unknown_mime_falls_back_to_png(cards):
    served = gemini._save("s2", _RAW_IMG, "image/gif")  # not in _MIME_EXT
    assert served == "/cards/s2.png"
    assert (cards / "s2.png").exists()


# ── the REAL _call_gemini_image (httpx mocked, function executed) ────────────

def test_call_gemini_image_decodes_inline_data(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(gemini.httpx, "Client", _fake_client(_image_payload(), captured))
    monkeypatch.setattr(gemini.settings, "gemini_api_key", "secret-img-key")
    monkeypatch.setattr(gemini.settings, "gemini_image_model", "imodel")
    img, mime = gemini._call_gemini_image("PROMPT")
    assert img == _RAW_IMG
    assert mime == "image/png"
    # request shape the image feature depends on
    assert "imodel:generateContent" in captured["url"]
    assert captured["headers"]["x-goog-api-key"] == "secret-img-key"
    assert captured["body"]["generationConfig"]["responseModalities"] == ["IMAGE"]
    assert captured["body"]["contents"][0]["parts"][0]["text"] == "PROMPT"


def test_call_gemini_image_accepts_snake_case_inline_data(monkeypatch):
    # Gemini sometimes returns snake_case keys (inline_data / mime_type).
    payload = _image_payload(mime="image/webp", key="inline_data", mime_key="mime_type")
    monkeypatch.setattr(gemini.httpx, "Client", _fake_client(payload))
    monkeypatch.setattr(gemini.settings, "gemini_api_key", "k")
    img, mime = gemini._call_gemini_image("p")
    assert img == _RAW_IMG
    assert mime == "image/webp"


def test_call_gemini_image_no_image_raises(monkeypatch):
    # candidates present but no inlineData → ValueError("no image ...")
    payload = {"candidates": [{"content": {"parts": [{"text": "oops"}]}}]}
    monkeypatch.setattr(gemini.httpx, "Client", _fake_client(payload))
    monkeypatch.setattr(gemini.settings, "gemini_api_key", "k")
    with pytest.raises(ValueError):
        gemini._call_gemini_image("p")


def test_call_gemini_image_retries_transport_error_then_raises(monkeypatch):
    # All attempts hit a TransportError → the last one is re-raised after retries.
    attempts = {"n": 0}

    class _C:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **k):
            attempts["n"] += 1
            raise httpx.ConnectError("down")  # subclass of httpx.TransportError

    monkeypatch.setattr(gemini.httpx, "Client", _C)
    monkeypatch.setattr(gemini.settings, "gemini_api_key", "k")
    with pytest.raises(httpx.TransportError):
        gemini._call_gemini_image("p", attempts=2)
    assert attempts["n"] == 2  # retried the configured number of times


def test_call_gemini_image_status_error_not_retried(monkeypatch):
    # A non-transport error (HTTPStatusError from raise_for_status) propagates
    # immediately — it is NOT caught by the TransportError retry branch.
    req = httpx.Request("POST", "http://x")
    resp = httpx.Response(500, request=req)
    err = httpx.HTTPStatusError("500", request=req, response=resp)
    monkeypatch.setattr(gemini.httpx, "Client", _fake_client(None, status_exc=err))
    monkeypatch.setattr(gemini.settings, "gemini_api_key", "k")
    with pytest.raises(httpx.HTTPStatusError):
        gemini._call_gemini_image("p")


# ── generate_image: gate / happy / error ────────────────────────────────────

async def test_generate_image_no_key_returns_none(sf, monkeypatch):
    monkeypatch.setattr(gemini.settings, "gemini_api_key", "")
    # HTTP must never be reached
    monkeypatch.setattr(gemini, "_call_gemini_image",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no http")))
    async with sf() as s:
        assert await gemini.generate_image(s, slug="x", prompt="p") is None
        # no ledger row written when there is no key
        assert (await s.execute(select(func.count()).select_from(GeminiUsage))).scalar() == 0


async def test_generate_image_budget_gate_skips_http(sf, monkeypatch, budget, cards):
    monkeypatch.setattr(gemini.settings, "gemini_weekly_budget_usd", 0.0)  # nothing left
    called = {"hit": False}

    def _boom(*a, **k):
        called["hit"] = True
        raise AssertionError("HTTP must not be called when over budget")

    monkeypatch.setattr(gemini, "_call_gemini_image", _boom)
    async with sf() as s:
        assert await gemini.generate_image(s, slug="x", prompt="p", category_id=None) is None
        # the gate writes NO ledger row at all (not even $0)
        assert (await s.execute(select(func.count()).select_from(GeminiUsage))).scalar() == 0
    assert called["hit"] is False


async def test_generate_image_budget_exactly_at_limit_proceeds(sf, monkeypatch, budget, cards):
    # gate is strict `>`, so spent + cost == budget is allowed.
    monkeypatch.setattr(gemini, "_call_gemini_image", lambda *a, **k: (_RAW_IMG, "image/png"))
    async with sf() as s:
        await gemini_usage_repo.record(s, category_id=None, cost_usd=9.96,
                                       model="seed", ok=True, kind="image")
        await s.commit()
        out = await gemini.generate_image(s, slug="edge", prompt="p")  # 9.96 + 0.04 == 10.0
    assert out == "/cards/edge.png"
    assert (cards / "edge.png").exists()


async def test_generate_image_uses_live_appconfig_budget(sf, monkeypatch, budget, cards):
    # A low app_config budget overrides the (generous) settings default and gates.
    def _boom(*a, **k):
        raise AssertionError("HTTP must not run when app_config budget is exhausted")

    monkeypatch.setattr(gemini, "_call_gemini_image", _boom)
    async with sf() as s:
        s.add(AppConfig(key=gemini.appconfig.GEMINI_WEEKLY_BUDGET, value="0.01"))
        await s.commit()
        # 0 spent + 0.04 cost > 0.01 budget → skipped
        assert await gemini.generate_image(s, slug="x", prompt="p") is None


async def test_generate_image_happy_path_saves_and_charges(sf, monkeypatch, budget, cards):
    monkeypatch.setattr(gemini, "_call_gemini_image", lambda *a, **k: (_RAW_IMG, "image/jpeg"))
    async with sf() as s:
        out = await gemini.generate_image(s, slug="happy", prompt="p", category_id=None)
        await s.commit()
        assert out == "/cards/happy.jpg"
        assert (cards / "happy.jpg").read_bytes() == _RAW_IMG
        row = (await s.execute(select(GeminiUsage))).scalar_one()
        assert row.ok is True
        assert row.kind == "image"  # default kind for the image path
        assert row.model == "img-test"
        assert float(row.cost_usd) == pytest.approx(0.04)


async def test_generate_image_failure_records_zero_cost(sf, monkeypatch, budget, cards):
    def _raise(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(gemini, "_call_gemini_image", _raise)
    async with sf() as s:
        out = await gemini.generate_image(s, slug="fail", prompt="p", category_id=None)
        await s.commit()
        assert out is None
        # nothing written to disk
        assert not (cards / "fail.png").exists()
        # exactly one ledger row, recorded at $0 (failure, not double-counted)
        row = (await s.execute(select(GeminiUsage))).scalar_one()
        assert row.ok is False
        assert float(row.cost_usd) == 0.0


# ── generate_category_image: status transitions ─────────────────────────────

async def _seed_category(s, *, slug="sports", title="Sports", prompt_override=None):
    cat = Category(slug=slug, title=title, prompt_override=prompt_override)
    s.add(cat)
    await s.flush()
    return cat


async def test_generate_category_image_no_key_returns_none_no_status_change(sf, monkeypatch):
    monkeypatch.setattr(gemini.settings, "gemini_api_key", "")
    async with sf() as s:
        cat = await _seed_category(s)
        await s.commit()
        assert await gemini.generate_category_image(s, cat) is None
        await s.commit()
        # set_image was never called → still the default "none"
        refreshed = await s.get(Category, cat.id)
        assert refreshed.image_status == "none"


async def test_generate_category_image_ready_on_success(sf, monkeypatch, budget, cards):
    monkeypatch.setattr(gemini, "_call_gemini_image", lambda *a, **k: (_RAW_IMG, "image/png"))
    async with sf() as s:
        cat = await _seed_category(s, slug="crypto", title="Crypto")
        await s.commit()
        path = await gemini.generate_category_image(s, cat)
        await s.commit()
        assert path == "/cards/crypto.png"
        refreshed = await s.get(Category, cat.id)
        assert refreshed.image_status == "ready"
        assert refreshed.image_path == "/cards/crypto.png"
        assert refreshed.image_generated_at is not None
        # default template prompt was stored (no override) and mentions the title
        assert "Crypto" in refreshed.image_prompt


async def test_generate_category_image_failed_status_when_budget_blocks(sf, monkeypatch, budget, cards):
    monkeypatch.setattr(gemini.settings, "gemini_weekly_budget_usd", 0.0)  # blocks generate_image
    async with sf() as s:
        cat = await _seed_category(s, slug="weather", title="Weather")
        await s.commit()
        path = await gemini.generate_category_image(s, cat)
        await s.commit()
        assert path is None
        refreshed = await s.get(Category, cat.id)
        # generating → failed (path stayed None)
        assert refreshed.image_status == "failed"
        assert refreshed.image_path is None


async def test_generate_category_image_uses_prompt_override(sf, monkeypatch, budget, cards):
    seen = {}

    def _capture(prompt, *a, **k):
        seen["prompt"] = prompt
        return (_RAW_IMG, "image/png")

    monkeypatch.setattr(gemini, "_call_gemini_image", _capture)
    async with sf() as s:
        cat = await _seed_category(s, slug="ovr", title="Title",
                                   prompt_override="  CUSTOM admin prompt  ")
        await s.commit()
        await gemini.generate_category_image(s, cat)
        await s.commit()
        assert seen["prompt"] == "CUSTOM admin prompt"  # stripped, override wins
        refreshed = await s.get(Category, cat.id)
        assert refreshed.image_prompt == "CUSTOM admin prompt"


# ── welcome banner helpers ───────────────────────────────────────────────────

def test_welcome_image_file_none_when_absent(cards):
    assert gemini.welcome_image_file() is None


def test_welcome_image_file_found_first_extension(cards):
    # write a webp; the helper probes png → jpg → webp and returns the existing one
    (cards / f"{gemini.WELCOME_SLUG}.webp").write_bytes(_RAW_IMG)
    found = gemini.welcome_image_file()
    assert found is not None and found.name == f"{gemini.WELCOME_SLUG}.webp"


async def test_welcome_prompt_default_when_unset(sf):
    async with sf() as s:
        assert await gemini.welcome_prompt(s) == gemini.DEFAULT_WELCOME_PROMPT


async def test_welcome_prompt_uses_appconfig_override(sf):
    async with sf() as s:
        s.add(AppConfig(key=gemini.WELCOME_PROMPT_KEY, value="my banner prompt"))
        await s.commit()
        assert await gemini.welcome_prompt(s) == "my banner prompt"


async def test_generate_welcome_image_returns_cached_when_exists(sf, monkeypatch, cards):
    # An existing file + force=False → no generation; returns the stored path.
    (cards / f"{gemini.WELCOME_SLUG}.png").write_bytes(_RAW_IMG)

    def _boom(*a, **k):
        raise AssertionError("must not generate when a cached banner exists")

    monkeypatch.setattr(gemini, "generate_image", _boom)
    async with sf() as s:
        s.add(AppConfig(key=gemini.WELCOME_PATH_KEY, value="/cards/welcome.png"))
        await s.commit()
        out = await gemini.generate_welcome_image(s)
        assert out == "/cards/welcome.png"


async def test_generate_welcome_image_generates_and_stores_path(sf, monkeypatch, budget, cards):
    monkeypatch.setattr(gemini, "_call_gemini_image", lambda *a, **k: (_RAW_IMG, "image/png"))
    async with sf() as s:
        out = await gemini.generate_welcome_image(s, force=True)
        await s.commit()
        assert out == "/cards/welcome.png"
        assert (cards / "welcome.png").exists()
        # served path persisted to app_config for the dashboard/bot to find
        row = await s.get(AppConfig, gemini.WELCOME_PATH_KEY)
        assert row is not None and row.value == "/cards/welcome.png"


async def test_generate_welcome_image_failure_leaves_path_unset(sf, monkeypatch, budget, cards):
    def _raise(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(gemini, "_call_gemini_image", _raise)
    async with sf() as s:
        out = await gemini.generate_welcome_image(s, force=True)
        await s.commit()
        assert out is None
        # path key never written on failure
        assert await s.get(AppConfig, gemini.WELCOME_PATH_KEY) is None
