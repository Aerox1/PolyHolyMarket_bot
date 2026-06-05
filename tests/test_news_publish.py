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
                cta_market_question=None, cta_outcomes=None, hero_image_url=None)
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


def test_build_caption_drops_summary_that_repeats_title():
    # feeds/old rows lead the body with the H1 → the title must not appear twice
    # (the Adam Hamawy bug). Defensive twin of crawler.clean_body for stored rows.
    it = _item(translations={"en": {
        "title": "Adam Hamawy wins New Jersey primary",
        "summary": "Adam Hamawy wins New Jersey primary\nA former Army surgeon won the seat."}})
    cap = publisher.build_caption(it, lang="en", cap=_CAP)
    assert cap.count("Adam Hamawy wins New Jersey primary") == 1  # title only, not echoed
    assert "A former Army surgeon won the seat." in cap


def test_build_digest_drops_summary_that_repeats_title():
    it = _item(id=9, translations={"en": {
        "title": "Fed holds rates", "summary": "Fed holds rates\nThe central bank paused."}})
    digest = publisher.build_digest([it], lang="en", header="Today", bot_username="B")
    assert digest.count("Fed holds rates") == 1
    assert "The central bank paused." in digest


def test_build_caption_truncates_safely_under_cap():
    it = _item(translations={"en": {"title": "T", "summary": "x" * 5000}})
    cap = publisher.build_caption(it, lang="en", cap=publisher._CAPTION_CAP)
    assert len(cap) <= publisher._CAPTION_CAP
    assert "not financial advice" in cap          # footer (and source) survive the trim
    assert not cap.rstrip().endswith("<")          # never a dangling tag start


def test_fit_trims_on_word_boundary():
    # a budget trim must not stop mid-word — it backs up to the last space + '…'
    out = publisher._fit("alpha beta gamma delta epsilon", 18)
    assert out.endswith("…") and "<" not in out
    assert not out.rstrip("…").endswith("delt")   # no mid-word stub
    assert out.rstrip("…").split()[-1] in {"alpha", "beta", "gamma"}  # whole words only
    # fits already → returned escaped, untouched
    assert publisher._fit("a & b", 999) == "a &amp; b"


def test_build_keyboard_bet_vs_open_and_none():
    # resolved outcomes + known bot → a button per outcome with odds, deep-linking
    # nb-<id>-<index> (outcome resolved server-side at click).
    outs = [{"label": "Yes", "market_id": "0xabc", "side": "yes", "price": 0.6},
            {"label": "No", "market_id": "0xabc", "side": "no", "price": 0.4}]
    bet = publisher.build_keyboard(_item(cta_market_id="0xabc", cta_outcomes=outs,
                                         cta_url="https://t.me/B?start=n-7"), bot_username="B", lang="en")
    urls = [b.url for row in bet.inline_keyboard for b in row]
    texts = [b.text for row in bet.inline_keyboard for b in row]
    assert all(len(row) == 1 for row in bet.inline_keyboard)  # stacked: one button per row
    assert urls == ["https://t.me/B?start=nb-7-0", "https://t.me/B?start=nb-7-1"]
    # action-first CTA with the fixed-$5 potential payout (stake / slippage-capped
    # price): Yes 0.6 → 5/(0.6*1.05)=$7.94 ; No 0.4 → 5/(0.4*1.05)=$11.90
    assert texts[0].startswith("✅ Bet $5 on Yes") and "→ $7.94" in texts[0]
    assert texts[1].startswith("❌ Bet $5 on No") and "→ $11.90" in texts[1]
    # no resolved outcomes → the single "Open in bot" link (unchanged)
    openb = publisher.build_keyboard(_item(cta_market_id=None, cta_url="https://t.me/B?start=n-7"),
                                     bot_username="B", lang="en")
    assert openb.inline_keyboard[0][0].text == "📰 Open in bot"
    # no url + no bot username → no keyboard (None), not an empty markup
    assert publisher.build_keyboard(_item(cta_url=None), bot_username=None, lang="en") is None


def test_outcome_button_shows_fixed_stake_potential():
    # $5 stake, 5% slippage → potential = 5 / clamp(price * 1.05). Conditional, never
    # a guarantee — the button just shows what $5 RETURNS if that outcome resolves.
    o = publisher._outcome_text
    assert o({"label": "Yes", "price": 0.64}).startswith("✅ Bet $5 on Yes → $7.")
    assert o({"label": "No", "price": 0.36}).startswith("❌ Bet $5 on No → $13.")
    assert o({"label": "No", "price": 0.01}).endswith("→ $476")        # $5→~$500 only on a 1% longshot
    assert o({"label": "Brazil", "price": 0.31}).startswith("📈 Bet $5 on Brazil → $")
    assert o({"label": "Yes"}) == "✅ Bet $5 on Yes"                    # no price → no payout suffix


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

async def _seed_ready(url_hash="ph1", outcomes=None, **kw):
    async with async_session_scope() as s:
        it = await items_repo.create(s, url="https://n/" + url_hash, url_hash=url_hash,
                                     title_orig="Ready item", **kw)
        it.status = "ready"
        it.translations.update({"en": {"title": "Ready item", "summary": "summary"}})
        it.cta_url = f"https://t.me/TestBot?start=n-{it.id}"
        # A matched market makes the item publishable under bet-relevant-only (the
        # default); the question is shown next to the Bet buttons.
        it.cta_market_id = "0xMKT"
        it.cta_market_question = "Will this resolve YES?"
        if outcomes is not None:
            it.cta_outcomes = outcomes
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


async def test_publish_job_no_poll_without_outcomes():
    # the default _seed_ready has a market question but NO outcomes → no vote buttons
    await _set_channel()
    await _seed_ready(url_hash="poll3")
    bot = _Bot()
    await news_jobs.publish_job(SimpleNamespace(bot=bot))
    assert [c[0] for c in bot.calls] == ["text"]
    kb = bot.calls[0][1]["reply_markup"]
    assert not any(b.callback_data for row in kb.inline_keyboard for b in row)
