"""Settlement money-path: resolution parsing, win/lose/void payout math, and the
stats fold. These guard real-money P&L + accuracy accounting."""

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from db.models import Base, Bet, User
from db.repositories import bets as bets_repo
from db.repositories import stats as stats_repo
from polymarket.markets import parse_resolution


# ── resolution parsing (Gamma-shaped dicts) ──────────────────────────────────

def _mkt(closed, uma, prices, tokens=("TA", "TB")):
    import json
    return {"closed": closed, "umaResolutionStatus": uma,
            "outcomePrices": json.dumps(list(prices)), "clobTokenIds": json.dumps(list(tokens))}


def test_resolution_yes_won():
    r = parse_resolution(_mkt(True, "resolved", ["1", "0"]))
    assert r == {"resolved": True, "winning_token": "TA", "void": False}


def test_resolution_no_won():
    r = parse_resolution(_mkt(True, "resolved", ["0", "1"]))
    assert r["resolved"] and r["winning_token"] == "TB" and not r["void"]


def test_resolution_open_not_closed():
    assert parse_resolution(_mkt(False, "", ["0.6", "0.4"]))["resolved"] is False


def test_resolution_closed_but_not_uma_resolved():
    assert parse_resolution(_mkt(True, "proposed", ["0.6", "0.4"]))["resolved"] is False


def test_resolution_void_no_clear_winner():
    r = parse_resolution(_mkt(True, "resolved", ["0.5", "0.5"]))
    assert r["resolved"] and r["winning_token"] is None and r["void"] is True


# ── payout math ──────────────────────────────────────────────────────────────

def _bet(token="TA", amount=10.0, entry=0.5):
    b = Bet(user_id=1, market_id="0xM", token_id=token, outcome="YES", amount_usd=amount, entry_price=entry)
    return b


def test_settle_win_payout_and_brier():
    v = bets_repo.settle_bet_values(_bet(entry=0.5), winning_token="TA", void=False)
    assert v["status"] == "WON" and v["payout"] == 20.0 and v["pnl"] == 10.0
    assert abs(v["brier"] - 0.25) < 1e-9          # (0.5 - 1)^2


def test_settle_loss():
    v = bets_repo.settle_bet_values(_bet(token="TA", entry=0.5), winning_token="TB", void=False)
    assert v["status"] == "LOST" and v["payout"] == 0.0 and v["pnl"] == -10.0
    assert abs(v["brier"] - 0.25) < 1e-9          # (0.5 - 0)^2


def test_settle_longshot_win_pays_more():
    v = bets_repo.settle_bet_values(_bet(entry=0.1, amount=10), winning_token="TA", void=False)
    assert v["payout"] == pytest.approx(100.0) and v["pnl"] == pytest.approx(90.0)


def test_settle_void_refunds():
    v = bets_repo.settle_bet_values(_bet(amount=10), winning_token=None, void=True)
    assert v["status"] == "VOID" and v["payout"] == 10.0 and v["pnl"] == 0.0 and v["brier"] is None


# ── stats fold (async) ───────────────────────────────────────────────────────

@pytest.fixture
async def sf():
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_record_settlement_updates_stats(sf):
    async with sf() as s:
        s.add(User(telegram_id=1, username="u", language="en"))
        await s.flush()
        await stats_repo.record_settlement(s, 1, status="WON", pnl=10.0, brier=0.25)
        await stats_repo.record_settlement(s, 1, status="LOST", pnl=-5.0, brier=0.49)
        await stats_repo.record_settlement(s, 1, status="VOID", pnl=0.0, brier=None)  # not counted
        st = await stats_repo.get_stats(s, 1)
    assert st["wins"] == 1 and st["losses"] == 1 and st["settled_bets"] == 2
    assert st["realized_pnl_usd"] == 5.0
    assert st["win_rate"] == 50.0


async def test_full_settle_flow(sf):
    async with sf() as s:
        s.add(User(telegram_id=2, username="b", language="en"))
        await s.flush()
        bet = await bets_repo.create_bet(s, user_id=2, account_id=None, market_id="0xM",
                                         token_id="TA", question="Q?", outcome="yes",
                                         amount_usd=20.0, entry_price=0.5)
        assert bet.shares == 40.0 and bet.status == "OPEN"
        # market resolves: TA wins
        vals = bets_repo.settle_bet_values(bet, winning_token="TA", void=False)
        bets_repo.apply_settlement(bet, vals)
        await stats_repo.record_settlement(s, 2, status=vals["status"], pnl=vals["pnl"], brier=vals["brier"])
        await s.flush()
        assert bet.status == "WON" and float(bet.pnl_usd) == 20.0 and bet.settled_at is not None
        open_after = await bets_repo.open_bets(s)
    assert open_after == []  # settled bet is no longer OPEN (idempotent)
