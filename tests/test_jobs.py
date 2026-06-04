"""Background-job money path: the broadcast consumer and the settlement engine.

Both jobs open their OWN async sessions via async_session_scope, so we seed and
assert through that same scope (DB pattern (a)). market_resolution + the settle
helpers reached via asyncio.to_thread are monkeypatched ON the bot.jobs module.
No network, no real Telegram — context.bot.send_message is an async recorder."""

from types import SimpleNamespace

import pytest
from telegram.error import Forbidden, TelegramError

import bot.jobs as jobs
from db.engine import async_session_scope
from db.models import Bet, BetStatus, Command
from db.repositories import bets as bets_repo
from db.repositories import stats as stats_repo
from db.repositories import users as users_repo


# ── fakes ──────────────────────────────────────────────────────────────────────

class _Bot:
    """Records send_message calls; optionally raises on send."""

    def __init__(self, raise_exc=None):
        self.sent = []          # list of kwargs dicts
        self._raise = raise_exc

    async def send_message(self, **kwargs):
        if self._raise is not None:
            raise self._raise
        self.sent.append(kwargs)


def _ctx(bot=None):
    return SimpleNamespace(bot=bot or _Bot())


async def _seed_user(tg_id=900, lang="en"):
    async with async_session_scope() as s:
        u = await users_repo.get_or_create_user(
            s, telegram_id=tg_id, username="u", first_name="U", default_language=lang)
        return u.id, u.telegram_id


async def _make_command(*, user_id, message="hi", action="BROADCAST", status="pending"):
    """No commands_repo.create — build the row directly."""
    async with async_session_scope() as s:
        cmd = Command(user_id=user_id, action=action,
                      payload={"message": message} if message is not None else {},
                      status=status)
        s.add(cmd)
        await s.flush()
        return cmd.id


async def _command_status(cmd_id):
    async with async_session_scope() as s:
        cmd = await s.get(Command, cmd_id)
        return cmd.status


# ── broadcast_job ───────────────────────────────────────────────────────────────

async def test_broadcast_delivers_and_marks_done():
    uid, tg = await _seed_user(tg_id=901)
    cid = await _make_command(user_id=uid, message="hello world")
    bot = _Bot()
    await jobs.broadcast_job(_ctx(bot))
    # the text was delivered to the user's telegram_id, command flips to done
    assert bot.sent and bot.sent[0]["chat_id"] == tg and bot.sent[0]["text"] == "hello world"
    assert await _command_status(cid) == "done"


async def test_broadcast_empty_message_marks_error_no_send():
    uid, _ = await _seed_user(tg_id=902)
    cid = await _make_command(user_id=uid, message="")   # empty body → guard
    bot = _Bot()
    await jobs.broadcast_job(_ctx(bot))
    assert bot.sent == []                                  # nothing sent
    assert await _command_status(cid) == "error"


async def test_broadcast_missing_payload_key_marks_error():
    # payload with no "message" key at all → message defaults to "" → guard
    uid, _ = await _seed_user(tg_id=903)
    cid = await _make_command(user_id=uid, message=None)   # payload == {}
    bot = _Bot()
    await jobs.broadcast_job(_ctx(bot))
    assert bot.sent == []
    assert await _command_status(cid) == "error"


async def test_broadcast_no_telegram_id_marks_error(monkeypatch):
    # telegram_id_for returns None (e.g. user vanished) → guard, no send.
    # A Command FK requires a real user row, so we seed one and force the lookup
    # to None to exercise the "telegram_id is None" branch.
    uid, _ = await _seed_user(tg_id=907)
    cid = await _make_command(user_id=uid, message="hi")

    async def none_lookup(session, user_id):
        return None

    monkeypatch.setattr(jobs.commands_repo, "telegram_id_for", none_lookup)
    bot = _Bot()
    await jobs.broadcast_job(_ctx(bot))
    assert bot.sent == []
    assert await _command_status(cid) == "error"


async def test_broadcast_forbidden_marks_error():
    # user blocked the bot → Forbidden → marked error, not raised
    uid, _ = await _seed_user(tg_id=904)
    cid = await _make_command(user_id=uid, message="hi")
    bot = _Bot(raise_exc=Forbidden("blocked"))
    await jobs.broadcast_job(_ctx(bot))
    assert await _command_status(cid) == "error"


async def test_broadcast_generic_telegram_error_marks_error():
    # any other TelegramError is logged + marked error, never re-raised
    uid, _ = await _seed_user(tg_id=905)
    cid = await _make_command(user_id=uid, message="hi")
    bot = _Bot(raise_exc=TelegramError("boom"))
    await jobs.broadcast_job(_ctx(bot))   # must NOT raise
    assert await _command_status(cid) == "error"


async def test_broadcast_ignores_non_broadcast_and_non_pending():
    # only pending BROADCAST rows are consumed; others are untouched
    uid, _ = await _seed_user(tg_id=906)
    other_action = await _make_command(user_id=uid, message="x", action="SYNC")
    already_done = await _make_command(user_id=uid, message="y", status="done")
    bot = _Bot()
    await jobs.broadcast_job(_ctx(bot))
    assert bot.sent == []
    assert await _command_status(other_action) == "pending"   # wrong action, left alone
    assert await _command_status(already_done) == "done"      # not pending, left alone


# ── settlement_job ───────────────────────────────────────────────────────────────

async def _seed_open_bet(*, user_id, market_id, token_id, outcome="YES",
                         amount=10.0, entry=0.5, question="Q?"):
    async with async_session_scope() as s:
        bet = await bets_repo.create_bet(
            s, user_id=user_id, account_id=None, market_id=market_id, token_id=token_id,
            question=question, outcome=outcome, amount_usd=amount, entry_price=entry)
        return bet.id


async def _bet_status(bet_id):
    async with async_session_scope() as s:
        bet = await s.get(Bet, bet_id)
        return bet.status


async def test_settlement_no_open_markets_early_return(monkeypatch):
    # no open bets → open_market_ids empty → early return, market_resolution never called
    called = {"n": 0}

    def fake_res(mid):
        called["n"] += 1
        return {"resolved": True, "winning_token": None, "void": False}

    monkeypatch.setattr(jobs.markets, "market_resolution", fake_res)
    bot = _Bot()
    await jobs.settlement_job(_ctx(bot))   # must not raise
    assert called["n"] == 0 and bot.sent == []


async def test_settlement_settles_winner_and_loser_and_notifies(monkeypatch):
    uid, tg = await _seed_user(tg_id=910)
    # one market, two OPEN bets: TA is the winning token, TB loses
    win_id = await _seed_open_bet(user_id=uid, market_id="0xM", token_id="TA",
                                  outcome="YES", amount=10.0, entry=0.5)
    lose_id = await _seed_open_bet(user_id=uid, market_id="0xM", token_id="TB",
                                   outcome="NO", amount=10.0, entry=0.5)

    def fake_res(mid):
        assert mid == "0xM"
        return {"resolved": True, "winning_token": "TA", "void": False}

    monkeypatch.setattr(jobs.markets, "market_resolution", fake_res)
    bot = _Bot()
    await jobs.settlement_job(_ctx(bot))

    # both bets settled out of OPEN
    assert await _bet_status(win_id) == BetStatus.WON.value
    assert await _bet_status(lose_id) == BetStatus.LOST.value
    # stats recorded: one win, one loss, both settled
    async with async_session_scope() as s:
        st = await stats_repo.get_stats(s, uid)
    assert st["wins"] == 1 and st["losses"] == 1 and st["settled_bets"] == 2
    # a notification was queued + sent per settled bet (WON + LOST → 2)
    assert len(bot.sent) == 2
    chats = {m["chat_id"] for m in bot.sent}
    assert chats == {tg}
    assert all(m.get("parse_mode") == "Markdown" for m in bot.sent)


async def test_settlement_skips_unresolved_market(monkeypatch):
    # market not resolved yet → bet stays OPEN, no notification
    uid, _ = await _seed_user(tg_id=911)
    bid = await _seed_open_bet(user_id=uid, market_id="0xOPEN", token_id="TA")
    monkeypatch.setattr(jobs.markets, "market_resolution",
                        lambda mid: {"resolved": False, "winning_token": None, "void": False})
    bot = _Bot()
    await jobs.settlement_job(_ctx(bot))
    assert await _bet_status(bid) == BetStatus.OPEN.value
    assert bot.sent == []


async def test_settlement_per_bet_isolation_one_bad_bet(monkeypatch):
    # Make settle_bet_values raise for ONE specific bet. That bet must stay OPEN
    # while the rest settle, and no exception escapes the job.
    uid, tg = await _seed_user(tg_id=912)
    bad_id = await _seed_open_bet(user_id=uid, market_id="0xM", token_id="TA",
                                  outcome="YES", amount=10.0, entry=0.5)
    good_id = await _seed_open_bet(user_id=uid, market_id="0xM", token_id="TA",
                                   outcome="YES", amount=20.0, entry=0.5)

    monkeypatch.setattr(jobs.markets, "market_resolution",
                        lambda mid: {"resolved": True, "winning_token": "TA", "void": False})

    real_settle = jobs.bets_repo.settle_bet_values

    def flaky(bet, *, winning_token, void):
        if bet.id == bad_id:
            raise RuntimeError("malformed bet")
        return real_settle(bet, winning_token=winning_token, void=void)

    monkeypatch.setattr(jobs.bets_repo, "settle_bet_values", flaky)
    bot = _Bot()
    await jobs.settlement_job(_ctx(bot))   # must NOT raise

    assert await _bet_status(bad_id) == BetStatus.OPEN.value    # left OPEN for retry
    assert await _bet_status(good_id) == BetStatus.WON.value    # the rest still settle
    # only the good bet produced a stat + a notification
    async with async_session_scope() as s:
        st = await stats_repo.get_stats(s, uid)
    assert st["wins"] == 1 and st["settled_bets"] == 1
    assert len(bot.sent) == 1


async def test_settlement_void_refunds_and_notifies(monkeypatch):
    # resolved but no clear winner → VOID: bet flips to VOID, notification sent,
    # but VOID does not count toward wins/losses.
    uid, _ = await _seed_user(tg_id=913)
    bid = await _seed_open_bet(user_id=uid, market_id="0xV", token_id="TA",
                               outcome="YES", amount=15.0, entry=0.4)
    monkeypatch.setattr(jobs.markets, "market_resolution",
                        lambda mid: {"resolved": True, "winning_token": None, "void": True})
    bot = _Bot()
    await jobs.settlement_job(_ctx(bot))
    assert await _bet_status(bid) == BetStatus.VOID.value
    async with async_session_scope() as s:
        st = await stats_repo.get_stats(s, uid)
    assert st["wins"] == 0 and st["losses"] == 0 and st["settled_bets"] == 0
    assert len(bot.sent) == 1   # still notified about the refund


async def test_settlement_notify_swallows_send_failure(monkeypatch):
    # a Forbidden/TelegramError while notifying must be swallowed (bet stays settled)
    uid, _ = await _seed_user(tg_id=914)
    bid = await _seed_open_bet(user_id=uid, market_id="0xM", token_id="TA",
                               outcome="YES", amount=10.0, entry=0.5)
    monkeypatch.setattr(jobs.markets, "market_resolution",
                        lambda mid: {"resolved": True, "winning_token": "TA", "void": False})
    bot = _Bot(raise_exc=Forbidden("blocked"))
    await jobs.settlement_job(_ctx(bot))   # must NOT raise despite send failing
    assert await _bet_status(bid) == BetStatus.WON.value   # settlement committed regardless


# ── _settle_message ──────────────────────────────────────────────────────────────

def _msg_bet(outcome="YES", question="Will it rain tomorrow in the city?", amount=10.0):
    # a detached Bet is fine — _settle_message only reads attributes, no DB.
    return Bet(user_id=1, market_id="0xM", token_id="TA", outcome=outcome,
               question=question, amount_usd=amount, entry_price=0.5)


def test_settle_message_won_contains_amounts():
    bet = _msg_bet(outcome="YES")
    vals = {"status": "WON", "payout": 20.0, "pnl": 10.0}
    out = jobs._settle_message(bet, vals, "en")
    assert "20.00" in out and "10.00" in out and "YES" in out


def test_settle_message_lost_contains_stake():
    bet = _msg_bet(outcome="NO", amount=25.0)
    out = jobs._settle_message(bet, {"status": "LOST"}, "en")
    assert "25.00" in out and "NO" in out


def test_settle_message_void_contains_stake():
    bet = _msg_bet(amount=15.0)
    out = jobs._settle_message(bet, {"status": "VOID"}, "en")
    assert "15.00" in out


def test_settle_message_truncates_long_question():
    long_q = "X" * 200
    out = jobs._settle_message(_msg_bet(question=long_q),
                               {"status": "LOST"}, "en")
    # question is sliced to 60 chars before interpolation
    assert ("X" * 60) in out and ("X" * 61) not in out


# ── register_jobs ────────────────────────────────────────────────────────────────

def test_register_jobs_no_queue_returns_quietly():
    # application.job_queue is None → warn + return, never raise
    app = SimpleNamespace(job_queue=None)
    jobs.register_jobs(app)   # no exception


def test_register_jobs_schedules_broadcast_and_settlement():
    class _JQ:
        def __init__(self):
            self.calls = []

        def run_repeating(self, callback, interval, first, name):
            self.calls.append(SimpleNamespace(callback=callback, interval=interval,
                                              first=first, name=name))

    jq = _JQ()
    jobs.register_jobs(SimpleNamespace(job_queue=jq))
    names = {c.name for c in jq.calls}
    assert names == {"broadcast", "settlement"}
    by_name = {c.name: c for c in jq.calls}
    # intervals/callbacks wired correctly
    assert by_name["broadcast"].callback is jobs.broadcast_job
    assert by_name["broadcast"].interval == jobs.BROADCAST_INTERVAL_SECONDS
    assert by_name["settlement"].callback is jobs.settlement_job
    assert by_name["settlement"].interval == jobs.SETTLEMENT_INTERVAL_SECONDS
