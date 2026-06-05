"""The news editorial VOICE: render applies the admin-editable tone_prompt to the
LLM title/summary call, drops it for sensitive headlines, and the prompt builder
is tone-aware (no forced 'neutral' when a voice is set)."""

import pytest

from bot.news import cta as news_cta
from bot.news import render as render_mod
from bot.news import voice as voice_mod
from bot.news.sensitivity import is_sensitive
from core import gemini
from db.engine import async_session_scope
from db.repositories import appconfig
from db.repositories import news_items as items_repo


def _afn(value):
    async def _f(*a, **k):
        return value
    return _f


def _capture_translate(store, ret):
    async def _f(session, **kw):
        store.update(kw)
        return ret
    return _f


@pytest.fixture(autouse=True)
def _no_cta(monkeypatch):
    # CTA resolution is irrelevant to the tone wiring; stub it to no-market.
    monkeypatch.setattr(news_cta, "resolve_cta", _afn(None))


# ── is_sensitive gate ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("title,expected", [
    ("Missile attack kills dozens as war escalates", True),
    ("Earthquake leaves hundreds dead", True),
    ("Fed holds rates steady, hints at one cut", False),
    ("Mark Cuban sells most of his Bitcoin", False),
])
def test_is_sensitive(title, expected):
    assert is_sensitive(title) is expected


# ── prompt builder is tone-aware ──────────────────────────────────────────────

def test_build_translate_prompt_tone_aware():
    with_tone = gemini._build_translate_prompt("T", "B", ("en",), "be dry and funny")
    assert "be dry and funny" in with_tone
    assert "neutral summary" not in with_tone           # tone governs register
    assert "ARTICLE TITLE" in with_tone

    no_tone = gemini._build_translate_prompt("T", "B", ("en",), "")
    assert "neutral summary" in no_tone                  # default stays neutral
    assert "ARTICLE TITLE" in no_tone


# ── render wires the voice through ────────────────────────────────────────────

async def test_render_applies_default_voice(monkeypatch):
    store = {}
    monkeypatch.setattr(gemini, "translate_summarize_news",
                        _capture_translate(store, {"en": {"title": "T", "summary": "S"}}))
    async with async_session_scope() as s:
        item = await items_repo.create(s, url="v1", url_hash="vh1", title_orig="Fed holds rates")
        item.status = "approved"
        await render_mod.render_item(s, item, bot_username="B")
    assert store["tone_prompt"] == voice_mod.DEFAULT_TONE_PROMPT
    assert store["tone_prompt"]  # non-empty


async def test_render_drops_voice_for_sensitive_headline(monkeypatch):
    store = {}
    monkeypatch.setattr(gemini, "translate_summarize_news",
                        _capture_translate(store, {"en": {"title": "T", "summary": "S"}}))
    async with async_session_scope() as s:
        item = await items_repo.create(s, url="v2", url_hash="vh2",
                                       title_orig="Missile attack kills dozens in border war")
        item.status = "approved"
        await render_mod.render_item(s, item, bot_username="B")
    assert store["tone_prompt"] == ""   # kill-switch → neutral wire copy


async def test_render_uses_appconfig_override(monkeypatch):
    store = {}
    monkeypatch.setattr(gemini, "translate_summarize_news",
                        _capture_translate(store, {"en": {"title": "T", "summary": "S"}}))
    async with async_session_scope() as s:
        await appconfig.set_(s, voice_mod.NEWS_TONE_PROMPT_KEY, "house style: maximally unhinged")
    async with async_session_scope() as s:
        item = await items_repo.create(s, url="v3", url_hash="vh3", title_orig="Bitcoin rips higher")
        item.status = "approved"
        await render_mod.render_item(s, item, bot_username="B")
    assert store["tone_prompt"] == "house style: maximally unhinged"
