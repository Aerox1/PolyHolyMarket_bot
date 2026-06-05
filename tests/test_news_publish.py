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
    # action-first CTA: "✅ Bet Yes · 60%" / "❌ Bet No · 40%"
    assert texts[0].startswith("✅ Bet Yes") and "60%" in texts[0]
    assert texts[1].startswith("❌ Bet No") and "40%" in texts[1]
    # no resolved outcomes → the single "Open in bot" link (unchanged)
    openb = publisher.build_keyboard(_item(cta_market_id=None, cta_url="https://t.me/B?start=n-7"),
                                     bot_username="B", lang="en")
    assert openb.inline_keyboard[0][0].text == "📰 Open in bot"
    # no url + no bot username → no keyboard (None), not an empty markup
    assert publisher.build_keyboard(_item(cta_url=None), bot_username=None, lang="en") is None


# ── inline engagement poll (callback vote buttons ON the card) ─────────────────

def _votes(kb):
    return [b for row in kb.inline_keyboard for b in row if b.callback_data]


def test_build_keyboard_inline_poll_buttons_and_tallies():
    outs = [{"label": "Yes", "market_id": "0xM", "side": "yes", "price": 0.6},
            {"label": "No", "market_id": "0xM", "side": "no", "price": 0.4}]
    it = _item(id=7, cta_market_id="0xM", cta_outcomes=outs, cta_url="https://t.me/B?start=n-7")
    # with_poll → vote buttons (callback nv:<id>:<i>) appended UNDER the bet buttons,
    # mirroring the outcomes by index; no % until votes exist
    kb = publisher.build_keyboard(it, bot_username="B", lang="en", with_poll=True)
    assert [b.callback_data for b in _votes(kb)] == ["nv:7:0", "nv:7:1"]
    assert [b.text for b in _votes(kb)] == ["🗳 Yes", "🗳 No"]
    # the bet URL buttons are still there, above the poll (one message, both)
    urls = [b.url for row in kb.inline_keyboard for b in row if b.url]
    assert urls == ["https://t.me/B?start=nb-7-0", "https://t.me/B?start=nb-7-1"]
    # tallies → live share of all votes on the item
    kb2 = publisher.build_keyboard(it, bot_username="B", lang="en", with_poll=True, tallies={0: 3, 1: 1})
    assert [b.text for b in _votes(kb2)] == ["🗳 Yes · 75%", "🗳 No · 25%"]
    # without with_poll → no vote buttons (bet URLs only) — unchanged legacy shape
    assert _votes(publisher.build_keyboard(it, bot_username="B", lang="en")) == []
    # multi-outcome events keep their real labels, laid out 2-up
    multi = [{"label": "Dems"}, {"label": "Reps"}, {"label": "Other"}]
    kb3 = publisher.build_keyboard(_item(id=9, cta_market_id="0xM", cta_outcomes=multi),
                                   bot_username="B", lang="en", with_poll=True)
    assert [b.callback_data for b in _votes(kb3)] == ["nv:9:0", "nv:9:1", "nv:9:2"]


# ── poll "spice" voice (funny vibe-check labels; bet buttons stay clean) ────────

_BIN = [{"label": "Yes", "market_id": "0xM", "side": "yes", "price": 0.6},
        {"label": "No", "market_id": "0xM", "side": "no", "price": 0.4}]


def _bet_texts(kb):
    return [b.text for row in kb.inline_keyboard for b in row if b.url and b.text.startswith(("✅", "❌", "📈"))]


def test_poll_spice_remaps_binary_pair_but_keeps_index_and_bet_buttons():
    it = _item(id=7, cta_market_id="0xM", cta_outcomes=_BIN, cta_url="https://t.me/B?start=n-7")
    # spice 1 → translation-safe house pair
    kb1 = publisher.build_keyboard(it, bot_username="B", lang="en", with_poll=True, poll_spice=1)
    assert [b.text for b in _votes(kb1)] == ["🗳 HELL YEAH", "🗳 HELL NO"]
    # spice 2 → spicy flagship
    kb2 = publisher.build_keyboard(it, bot_username="B", lang="en", with_poll=True, poll_spice=2)
    assert [b.text for b in _votes(kb2)] == ["🗳 Hell Yeah!", "🗳 Fuck No!"]
    # vote attribution unchanged (still index-keyed) and the REAL bet buttons untouched
    assert [b.callback_data for b in _votes(kb2)] == ["nv:7:0", "nv:7:1"]
    bets = _bet_texts(kb2)
    assert bets[0].startswith("✅ Bet Yes") and bets[1].startswith("❌ Bet No")


def test_poll_spice_respects_yes_no_order():
    rev = [{"label": "No", "price": 0.4}, {"label": "Yes", "price": 0.6}]  # No first
    it = _item(id=8, cta_market_id="0xM", cta_outcomes=rev)
    kb = publisher.build_keyboard(it, bot_username="B", lang="en", with_poll=True, poll_spice=2)
    # the "Yes" side (index 1) must get the yes-label, not positional
    assert [b.text for b in _votes(kb)] == ["🗳 Fuck No!", "🗳 Hell Yeah!"]


def test_poll_spice_leaves_multi_outcome_labels_real():
    multi = [{"label": "Dems"}, {"label": "Reps"}, {"label": "Other"}]
    it = _item(id=9, cta_market_id="0xM", cta_outcomes=multi)
    kb = publisher.build_keyboard(it, bot_username="B", lang="en", with_poll=True, poll_spice=2)
    assert [b.text for b in _votes(kb)] == ["🗳 Dems", "🗳 Reps", "🗳 Other"]


def test_poll_spice_sensitive_headline_forces_neutral():
    it = _item(id=10, title_orig="Missile attack kills dozens as war escalates",
               cta_market_id="0xM", cta_outcomes=_BIN)
    kb = publisher.build_keyboard(it, bot_username="B", lang="en", with_poll=True, poll_spice=2)
    assert [b.text for b in _votes(kb)] == ["🗳 Yes", "🗳 No"]  # kill-switch → neutral


def test_poll_spice_zero_and_non_english_stay_neutral():
    it = _item(id=11, cta_market_id="0xM", cta_outcomes=_BIN)
    assert [b.text for b in _votes(publisher.build_keyboard(
        it, bot_username="B", lang="en", with_poll=True, poll_spice=0))] == ["🗳 Yes", "🗳 No"]
    # never ship English profanity to a non-EN channel
    assert [b.text for b in _votes(publisher.build_keyboard(
        it, bot_username="B", lang="fa", with_poll=True, poll_spice=2))] == ["🗳 Yes", "🗳 No"]


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


async def test_publish_job_embeds_inline_poll_on_card():
    await _set_channel()
    outs = [{"label": "Yes", "market_id": "0xMKT", "side": "yes", "price": 0.4},
            {"label": "No", "market_id": "0xMKT", "side": "no", "price": 0.6}]
    item_id = await _seed_ready(url_hash="poll1", outcomes=outs)
    bot = _Bot()
    await news_jobs.publish_job(SimpleNamespace(bot=bot))
    # ONE message — the poll is inline on the card, not a separate poll send
    assert [c[0] for c in bot.calls] == ["text"]
    kb = bot.calls[0][1]["reply_markup"]
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data]
    assert cbs == [f"nv:{item_id}:0", f"nv:{item_id}:1"]  # inline vote buttons on the card


async def test_publish_job_poll_can_be_disabled():
    await _set_channel()
    outs = [{"label": "Yes", "market_id": "0xMKT", "side": "yes", "price": 0.4},
            {"label": "No", "market_id": "0xMKT", "side": "no", "price": 0.6}]
    await _seed_ready(url_hash="poll2", outcomes=outs)
    async with async_session_scope() as s:
        await appconfig.set_(s, news_jobs.NEWS_POLL_KEY, "0")
    bot = _Bot()
    await news_jobs.publish_job(SimpleNamespace(bot=bot))
    assert [c[0] for c in bot.calls] == ["text"]
    kb = bot.calls[0][1]["reply_markup"]
    # bet buttons remain (URL), but no inline poll vote buttons
    assert not any(b.callback_data for row in kb.inline_keyboard for b in row)


async def test_publish_job_no_poll_without_outcomes():
    # the default _seed_ready has a market question but NO outcomes → no vote buttons
    await _set_channel()
    await _seed_ready(url_hash="poll3")
    bot = _Bot()
    await news_jobs.publish_job(SimpleNamespace(bot=bot))
    assert [c[0] for c in bot.calls] == ["text"]
    kb = bot.calls[0][1]["reply_markup"]
    assert not any(b.callback_data for row in kb.inline_keyboard for b in row)


async def test_post_item_with_poll_is_single_message():
    """A poll-enabled post is ONE message: the bet buttons plus the inline vote
    buttons share the card's keyboard (no separate poll send)."""
    bot = _Bot()
    outs = [{"label": "Yes", "market_id": "0xM", "side": "yes", "price": 0.6},
            {"label": "No", "market_id": "0xM", "side": "no", "price": 0.4}]
    it = _item(id=5, cta_market_id="0xM", cta_market_question="Q?", cta_outcomes=outs)
    mid = await publisher.post_item_to_channel(bot, it, chat_id=-100, lang="en",
                                               bot_username="TestBot", with_poll=True)
    assert mid == 202
    assert [c[0] for c in bot.calls] == ["text"]  # single message
    kb = bot.calls[0][1]["reply_markup"]
    assert [b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data] == ["nv:5:0", "nv:5:1"]


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
