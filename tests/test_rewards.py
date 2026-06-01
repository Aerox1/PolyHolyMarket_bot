"""Points + multi-layer referral economy: attribution, conditional unlock,
descending propagation, and the no-propagate-through-pending-edge rule."""

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from db.models import Base, Referral, User, UserStats
from db.repositories import rewards as rw


@pytest.fixture
async def sf():
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _user(s, tid, username=None):
    u = User(telegram_id=tid, username=username, language="en")
    s.add(u)
    await s.flush()
    return u


async def test_referral_code_unique_and_lookup(sf):
    async with sf() as s:
        a = await _user(s, 1, "alice")
        b = await _user(s, 2, "bob")
        ca = await rw.ensure_referral_code(s, a)
        cb = await rw.ensure_referral_code(s, b)
        assert ca and cb and ca != cb
        assert (await rw.get_by_referral_code(s, ca)).id == a.id


async def test_attribute_rejects_self_and_double(sf):
    async with sf() as s:
        a = await _user(s, 1, "alice")
        b = await _user(s, 2, "bob")
        ca = await rw.ensure_referral_code(s, a)
        assert await rw.attribute_referral(s, a, ca) is False         # self
        assert await rw.attribute_referral(s, b, ca) is True          # b referred by a
        assert b.referred_by == a.id
        # second attribution is ignored
        c = await _user(s, 3, "carol"); cc = await rw.ensure_referral_code(s, c)
        assert await rw.attribute_referral(s, b, cc) is False
        assert b.referred_by == a.id


async def test_bet_awards_points(sf):
    async with sf() as s:
        u = await _user(s, 1)
        pts = await rw.reward_for_bet(s, u.id, amount_usd=20.0)
        assert pts == rw.POINTS_BET_BASE + 20
        # balance = bet points + a once-per-day streak bonus (5 × a 1-day streak)
        assert await rw.balance(s, u.id) == pts + rw.DAILY_STREAK_BONUS
        # a second bet the same day does NOT re-award the streak bonus
        await rw.reward_for_bet(s, u.id, amount_usd=0.0)
        assert await rw.balance(s, u.id) == pts + rw.DAILY_STREAK_BONUS + rw.POINTS_BET_BASE


async def test_conditional_unlock_pays_both_sides(sf):
    async with sf() as s:
        a = await _user(s, 1, "alice"); b = await _user(s, 2, "bob")
        ca = await rw.ensure_referral_code(s, a)
        await rw.attribute_referral(s, b, ca)
        # below threshold → stays pending, no signup bonus
        s.add(UserStats(user_id=b.id, total_bets=rw.REFERRAL_UNLOCK_BETS - 1)); await s.flush()
        await rw._maybe_unlock(s, b.id)
        edge = await s.scalar(__import__("sqlalchemy").select(Referral).where(Referral.invitee_id == b.id))
        assert edge.status == "pending" and await rw.balance(s, a.id) == 0
        # cross threshold → unlock + two-sided signup
        (await s.get(UserStats, b.id)).total_bets = rw.REFERRAL_UNLOCK_BETS
        await s.flush()
        await rw._maybe_unlock(s, b.id)
        assert edge.status == "unlocked"
        assert await rw.balance(s, a.id) == rw.SIGNUP_BONUS
        assert await rw.balance(s, b.id) == rw.SIGNUP_BONUS


async def test_multilayer_propagation(sf):
    async with sf() as s:
        a = await _user(s, 1); b = await _user(s, 2); c = await _user(s, 3)
        b.referred_by = a.id; c.referred_by = b.id
        s.add(Referral(inviter_id=a.id, invitee_id=b.id, status="unlocked"))
        s.add(Referral(inviter_id=b.id, invitee_id=c.id, status="unlocked"))
        await s.flush()
        base = await rw.reward_for_bet(s, c.id, amount_usd=0.0)       # base = POINTS_BET_BASE
        assert base == rw.POINTS_BET_BASE
        # B earns L1, A earns L2
        assert await rw.balance(s, b.id) == int(base * rw.REFERRAL_LAYER_RATES[0])
        assert await rw.balance(s, a.id) == int(base * rw.REFERRAL_LAYER_RATES[1])


async def test_propagation_stops_at_pending_edge(sf):
    async with sf() as s:
        a = await _user(s, 1); b = await _user(s, 2); c = await _user(s, 3)
        b.referred_by = a.id; c.referred_by = b.id
        s.add(Referral(inviter_id=a.id, invitee_id=b.id, status="pending"))   # B not unlocked
        s.add(Referral(inviter_id=b.id, invitee_id=c.id, status="unlocked"))
        await s.flush()
        base = await rw.reward_for_bet(s, c.id, amount_usd=0.0)
        assert await rw.balance(s, b.id) == int(base * rw.REFERRAL_LAYER_RATES[0])  # L1 ok
        assert await rw.balance(s, a.id) == 0                                       # chain stops at B's pending edge
