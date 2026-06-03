"""News channel publishing: caption/CTA building, the publish job (idempotent +
marks sent), and the /start deep-link routing. Telegram is mocked."""

from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from bot.handlers import start
from bot.news import cta as news_cta
from bot.news import jobs as news_jobs
from bot.news import publisher
from core import gemini  # noqa: F401 (import parity with other news tests)
from db.engine import async_session_scope
from db.models import NewsChannelPost, NewsItem
from db.repositories import appconfig
from db.repositories import news_items as items_repo


def _item(**kw):
    base = dict(id=7, title_orig="Fed holds rates", body_orig="The Fed kept rates.",
                url="https://news/x", translations={}, cta_url=None, cta_market_id=None,
                hero_image_url=None)
    base.update(kw)
    return SimpleNamespace(**base)


# ── caption + keyboard ────────────────────────────────────────────────────────

_CAP = publisher._TEXT_CAP


def test_build_caption_has_title_summary_source_and_nfa():
    it = _item(translations={"en": {"title": "Big news", "summary": "It happened."}})
    cap = publisher.build_caption(it, lang="en", cap=_CAP)
    assert "<b>Big news</b>" in cap
    assert "It happened." in cap
    assert "Source" in cap and 'href="https://news/x"' in cap
    assert "not financial advice" in cap  # NFA footer always present


def test_build_caption_escapes_html():
    it = _item(translations={"en": {"title": "<script>x</script>", "summary": "a & b <i>"}})
    cap = publisher.build_caption(it, lang="en", cap=_CAP)
    assert "<script>" not in cap and "&lt;script&gt;" in cap
    assert "a &amp; b" in cap


def test_build_caption_falls_back_to_original_then_any_lang():
    it = _item(translations={})  # nothing translated
    assert "Fed holds rates" in publisher.build_caption(it, lang="en", cap=_CAP)
    it2 = _item(translations={"fa": {"title": "تیتر", "summary": "خلاصه"}})
    assert "تیتر" in publisher.build_caption(it2, lang="en", cap=_CAP)  # any available lang


def test_build_caption_truncates_safely_under_cap():
    it = _item(translations={"en": {"title": "T", "summary": "x" * 5000}})
    cap = publisher.build_caption(it, lang="en", cap=publisher._CAPTION_CAP)
    assert len(cap) <= publisher._CAPTION_CAP
    assert "not financial advice" in cap          # footer (and source) survive the trim
    assert not cap.rstrip().endswith("<")          # never a dangling tag start


def test_build_keyboard_bet_vs_open_and_none():
    # resolved market + known bot → a direct two-button bet CTA (Bet YES / Bet NO),
    # deep-linking nb-<id>-y / nb-<id>-n (outcome resolved server-side at click).
    bet = publisher.build_keyboard(_item(cta_market_id="0xabc", cta_url="https://t.me/B?start=n-7"),
                                   bot_username="B", lang="en")
    row = bet.inline_keyboard[0]
    assert [b.url for b in row] == ["https://t.me/B?start=nb-7-y", "https://t.me/B?start=nb-7-n"]
    assert "YES" in row[0].text and "NO" in row[1].text
    # no resolved market → the single "Open in bot" link (unchanged)
    openb = publisher.build_keyboard(_item(cta_market_id=None, cta_url="https://t.me/B?start=n-7"),
                                     bot_username="B", lang="en")
    assert openb.inline_keyboard[0][0].text == "📰 Open in bot"
    # no url + no bot username → no keyboard (None), not an empty markup
    assert publisher.build_keyboard(_item(cta_url=None), bot_username=None, lang="en") is None


# ── post_item_to_channel (mocked bot) ─────────────────────────────────────────

class _Msg:
    def __init__(self, mid):
        self.message_id = mid


class _Bot:
    def __init__(self, photo_fail=False):
        self.calls = []
        self._photo_fail = photo_fail
        self.username = "TestBot"

    async def send_photo(self, **kw):
        self.calls.append(("photo", kw))
        if self._photo_fail:
            from telegram.error import BadRequest
            raise BadRequest("bad image")
        return _Msg(101)

    async def send_message(self, **kw):
        self.calls.append(("text", kw))
        return _Msg(202)

    async def get_me(self):
        return SimpleNamespace(id=1)

    async def get_chat_member(self, chat_id, user_id):
        return SimpleNamespace(status="administrator")


async def test_post_text_when_no_hero():
    bot = _Bot()
    mid = await publisher.post_item_to_channel(bot, _item(), chat_id=-100, lang="en", bot_username="TestBot")
    assert mid == 202 and bot.calls[0][0] == "text"
    assert bot.calls[0][1]["parse_mode"] == "HTML"


async def test_post_photo_then_text_fallback_on_bad_image():
    bot = _Bot(photo_fail=True)
    mid = await publisher.post_item_to_channel(bot, _item(hero_image_url="https://img/x.jpg"),
                                               chat_id=-100, lang="en", bot_username="TestBot")
    assert mid == 202  # fell back to text
    assert [c[0] for c in bot.calls] == ["photo", "text"]


async def test_post_falls_back_to_plain_on_parse_error():
    """A BadRequest (HTML parse failure) must NOT pin the item — it retries as
    plain text (no parse_mode) so a poison caption can't loop forever."""
    from telegram.error import BadRequest

    class _ParseFail(_Bot):
        async def send_message(self, **kw):
            self.calls.append(("text", kw))
            if kw.get("parse_mode") == "HTML":
                raise BadRequest("can't parse entities")
            return _Msg(303)

    bot = _ParseFail()
    mid = await publisher.post_item_to_channel(bot, _item(), chat_id=-100, lang="en", bot_username="TestBot")
    assert mid == 303
    assert bot.calls[0][1].get("parse_mode") == "HTML"   # first attempt
    assert "parse_mode" not in bot.calls[1][1]            # plain fallback


# ── publish_job ───────────────────────────────────────────────────────────────

async def _seed_ready(url_hash="ph1", **kw):
    async with async_session_scope() as s:
        it = await items_repo.create(s, url="https://n/" + url_hash, url_hash=url_hash,
                                     title_orig="Ready item", **kw)
        it.status = "ready"
        it.translations.update({"en": {"title": "Ready item", "summary": "summary"}})
        it.cta_url = f"https://t.me/TestBot?start=n-{it.id}"
        return it.id


async def _set_channel(value="-1001234567890"):
    async with async_session_scope() as s:
        await appconfig.set_(s, "news_channel_id", value)


async def test_publish_job_posts_and_marks_sent_and_is_idempotent():
    await _set_channel()
    item_id = await _seed_ready()
    bot = _Bot()
    await news_jobs.publish_job(SimpleNamespace(bot=bot))
    async with async_session_scope() as s:
        it = s if False else await s.get(NewsItem, item_id)
        assert it.status == "sent" and it.channel_msg_id == 202 and it.published_at is not None
        posts = (await s.execute(select(func.count()).select_from(NewsChannelPost))).scalar()
        assert posts == 1
    sends_after_first = len(bot.calls)
    # second run must NOT re-post (idempotent via news_channel_posts)
    await news_jobs.publish_job(SimpleNamespace(bot=bot))
    assert len(bot.calls) == sends_after_first
    async with async_session_scope() as s:
        assert (await s.execute(select(func.count()).select_from(NewsChannelPost))).scalar() == 1


async def test_publish_job_noop_without_channel():
    await _seed_ready(url_hash="ph2")
    bot = _Bot()
    await news_jobs.publish_job(SimpleNamespace(bot=bot))  # no channel configured
    assert bot.calls == []
    async with async_session_scope() as s:
        it = await s.scalar(select(NewsItem))
        assert it.status == "ready"  # untouched


async def test_publish_job_skips_when_bot_not_channel_admin():
    await _set_channel()
    await _seed_ready(url_hash="ph3")

    class _NotAdmin(_Bot):
        async def get_chat_member(self, chat_id, user_id):
            return SimpleNamespace(status="member")

    bot = _NotAdmin()
    await news_jobs.publish_job(SimpleNamespace(bot=bot))
    assert all(c[0] not in ("photo", "text") for c in bot.calls)  # never posted
    async with async_session_scope() as s:
        assert (await s.scalar(select(NewsItem))).status == "ready"


# ── /start deep-link ──────────────────────────────────────────────────────────

async def test_open_news_item_routes_to_market(monkeypatch):
    seen = {}

    async def fake_market(update, context, mid):
        seen["mid"] = mid
        return True

    async def fake_dash(update, context):
        seen["dash"] = True

    monkeypatch.setattr(start.discover, "show_market_by_id", fake_market)
    monkeypatch.setattr(start, "show_dashboard", fake_dash)
    async with async_session_scope() as s:
        it = await items_repo.create(s, url="u", url_hash="dl1", title_orig="t")
        it.cta_market_id = "0xMKT"
        item_id = it.id
    await start._open_news_item(None, None, item_id)
    assert seen.get("mid") == "0xMKT" and "dash" not in seen


async def test_open_news_item_falls_back_to_dashboard(monkeypatch):
    seen = {}

    async def fake_dash(update, context):
        seen["dash"] = True

    monkeypatch.setattr(start, "show_dashboard", fake_dash)
    async with async_session_scope() as s:
        it = await items_repo.create(s, url="u2", url_hash="dl2", title_orig="t")  # no cta_market_id
        item_id = it.id
    await start._open_news_item(None, None, item_id)
    assert seen.get("dash") is True
