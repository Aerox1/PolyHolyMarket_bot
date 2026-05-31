"""Gamification: daily-streak transitions + leaderboard ordering."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from db.models import Base, User, UserStats
from db.repositories import stats as stats_repo


def _date(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d")


@pytest.fixture
async def sf():
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _user(session, tid: int) -> int:
    u = User(telegram_id=tid, username=f"u{tid}", language="en")
    session.add(u)
    await session.flush()
    return u.id


async def test_first_bet_starts_streak(sf):
    async with sf() as s:
        uid = await _user(s, 1)
        st = await stats_repo.record_bet(s, uid, 5.0)
        assert st.current_streak == 1 and st.total_bets == 1 and float(st.total_volume_usd) == 5.0


async def test_same_day_keeps_streak_bumps_totals(sf):
    async with sf() as s:
        uid = await _user(s, 2)
        await stats_repo.record_bet(s, uid, 5.0)
        st = await stats_repo.record_bet(s, uid, 20.0)
        assert st.current_streak == 1 and st.total_bets == 2 and float(st.total_volume_usd) == 25.0


async def test_consecutive_day_increments_streak(sf):
    async with sf() as s:
        uid = await _user(s, 3)
        st = await stats_repo.record_bet(s, uid, 1.0)
        st.last_active_date = _date(1)      # pretend last bet was yesterday
        st.current_streak = 3
        await s.flush()
        st2 = await stats_repo.record_bet(s, uid, 1.0)
        assert st2.current_streak == 4


async def test_gap_resets_streak_but_keeps_longest(sf):
    async with sf() as s:
        uid = await _user(s, 4)
        st = await stats_repo.record_bet(s, uid, 1.0)
        st.last_active_date = _date(5)      # 5-day gap
        st.current_streak = 9
        st.longest_streak = 9
        await s.flush()
        st2 = await stats_repo.record_bet(s, uid, 1.0)
        assert st2.current_streak == 1 and st2.longest_streak == 9


async def test_leaderboard_ranks_by_bets(sf):
    async with sf() as s:
        a = await _user(s, 10)
        b = await _user(s, 11)
        for _ in range(3):
            await stats_repo.record_bet(s, a, 1.0)
        await stats_repo.record_bet(s, b, 1.0)
        board = await stats_repo.leaderboard(s, metric="bets", limit=10)
        assert board[0]["bets"] == 3 and board[0]["rank"] == 1
        assert board[1]["bets"] == 1
