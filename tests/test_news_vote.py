"""Inline news-poll voting: the repo (dedup + tally) and the callback handler that
records a vote and re-renders the card's keyboard with the live share. Telegram is
mocked."""

from types import SimpleNamespace

from sqlalchemy import func, select

from bot.handlers import news as news_handler
from db.engine import async_session_scope
from db.models import NewsPollVote
from db.repositories import news_items as items_repo
from db.repositories import news_votes

OUTS = [{"label": "Yes", "market_id": "0xM", "side": "yes", "price": 0.6},
        {"label": "No", "market_id": "0xM", "side": "no", "price": 0.4}]


async def _seed_item(outcomes=OUTS, url_hash="vote1"):
    async with async_session_scope() as s:
        it = await items_repo.create(s, url="https://n/" + url_hash, url_hash=url_hash, title_orig="t")
        it.cta_market_id = "0xM"
        it.cta_market_question = "Will X?"
        it.cta_outcomes = outcomes
        return it.id


# ── repository ────────────────────────────────────────────────────────────────

async def test_cast_vote_dedup_and_switch():
    item_id = await _seed_item()
    async with async_session_scope() as s:
        await news_votes.cast_vote(s, item_id=item_id, tg_user_id=111, outcome_index=0)
        await news_votes.cast_vote(s, item_id=item_id, tg_user_id=222, outcome_index=1)
        await news_votes.cast_vote(s, item_id=item_id, tg_user_id=111, outcome_index=0)  # re-tap → no-op
    async with async_session_scope() as s:
        assert await news_votes.tallies(s, item_id) == {0: 1, 1: 1}
    # user 111 switches Yes → No (one row per user, moved, not duplicated)
    async with async_session_scope() as s:
        await news_votes.cast_vote(s, item_id=item_id, tg_user_id=111, outcome_index=1)
    async with async_session_scope() as s:
        assert await news_votes.tallies(s, item_id) == {1: 2}
        n = await s.scalar(select(func.count()).select_from(NewsPollVote)
                           .where(NewsPollVote.tg_user_id == 111))
        assert n == 1


# ── callback handler ──────────────────────────────────────────────────────────

class _Query:
    def __init__(self, data):
        self.data = data
        self.answered = "unset"
        self.markup = "unset"

    async def answer(self, text=None):
        self.answered = text if text is not None else ""

    async def edit_message_reply_markup(self, reply_markup=None):
        self.markup = reply_markup


class _Update:
    def __init__(self, data, uid):
        self.callback_query = _Query(data)
        self.effective_user = SimpleNamespace(id=uid)


def _ctx():
    return SimpleNamespace(bot=SimpleNamespace(username="TestBot"), user_data={"lang": "en"})


async def test_on_news_vote_records_and_rerenders():
    item_id = await _seed_item(url_hash="vote_h1")
    upd = _Update(f"nv:{item_id}:0", uid=999)
    await news_handler.on_news_vote(upd, _ctx())
    async with async_session_scope() as s:
        assert await news_votes.tallies(s, item_id) == {0: 1}
    assert "Yes" in upd.callback_query.answered  # toast names the chosen outcome
    kb = upd.callback_query.markup
    votes = [b for row in kb.inline_keyboard for b in row if b.callback_data]
    assert [b.callback_data for b in votes] == [f"nv:{item_id}:0", f"nv:{item_id}:1"]
    assert votes[0].text == "🗳 Yes · 100%"  # live tally re-rendered onto the card


async def test_on_news_vote_ignores_out_of_range_index():
    item_id = await _seed_item(url_hash="vote_h2")
    upd = _Update(f"nv:{item_id}:9", uid=1)  # no such outcome
    await news_handler.on_news_vote(upd, _ctx())
    assert upd.callback_query.markup == "unset"  # nothing edited
    assert upd.callback_query.answered == ""      # spinner dismissed, no toast
    async with async_session_scope() as s:
        assert await news_votes.tallies(s, item_id) == {}
