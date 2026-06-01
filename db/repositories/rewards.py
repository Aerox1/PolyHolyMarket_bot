"""Points & referral economy (virtual points — no real-money payout).

Design:
* Points live in an append-only ledger (balance = SUM(delta)); no ad-hoc writes.
* Referral is multi-layer & DESCENDING (Trojan-style) but gated by a CONDITIONAL
  UNLOCK: an invitee earns their inviter nothing until the invitee completes real
  activity (>= REFERRAL_UNLOCK_BETS bets). Unlock pays a two-sided signup bonus.
* Propagation up the chain stops at the first edge that isn't unlocked, so
  inactive intermediates can't farm deeper layers.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import PointsLedger, Referral, User, UserStats

# ── tunables (points are free to mint; kept modest + descending) ──────────────
REFERRAL_LAYER_RATES = [0.10, 0.05, 0.03, 0.02, 0.01]  # L1..L5
POINTS_BET_BASE = 10
POINTS_PER_USD = 1.0
WIN_BONUS = 25
DAILY_STREAK_BONUS = 5            # × current streak (capped)
DAILY_STREAK_CAP = 7
SIGNUP_BONUS = 500                # two-sided, on unlock
REFERRAL_UNLOCK_BETS = 3


# ── referral codes / attribution ──────────────────────────────────────────────

def _slug(user: User) -> str:
    if user.username:
        s = "".join(ch for ch in user.username.lower() if ch.isalnum())[:24]
        if s:
            return s
    return f"u{user.id}{secrets.token_hex(2)}"


async def ensure_referral_code(session: AsyncSession, user: User) -> str:
    if user.referral_code:
        return user.referral_code
    base = _slug(user)
    code = base
    # ensure uniqueness
    while await session.scalar(select(User.id).where(User.referral_code == code)):
        code = f"{base}{secrets.token_hex(2)}"
    user.referral_code = code
    await session.flush()
    return code


async def get_by_referral_code(session: AsyncSession, code: str) -> User | None:
    code = (code or "").strip().lower()
    if not code:
        return None
    return await session.scalar(select(User).where(func.lower(User.referral_code) == code))


async def attribute_referral(session: AsyncSession, invitee: User, code: str) -> bool:
    """Record a pending referral edge if the invitee is unreferred and code is a
    valid OTHER user. No reward yet (conditional unlock)."""
    if invitee.referred_by:
        return False
    inviter = await get_by_referral_code(session, code)
    if inviter is None or inviter.id == invitee.id:
        return False
    invitee.referred_by = inviter.id
    if await session.scalar(select(Referral.id).where(Referral.invitee_id == invitee.id)) is None:
        session.add(Referral(inviter_id=inviter.id, invitee_id=invitee.id, status="pending"))
    await session.flush()
    return True


# ── points ledger ───────────────────────────────────────────────────────────

async def award(session: AsyncSession, user_id: int, delta: int, reason: str, ref: str | None = None) -> None:
    if delta == 0:
        return
    session.add(PointsLedger(user_id=user_id, delta=int(delta), reason=reason, ref=ref))


async def balance(session: AsyncSession, user_id: int) -> int:
    total = await session.scalar(
        select(func.coalesce(func.sum(PointsLedger.delta), 0)).where(PointsLedger.user_id == user_id)
    )
    return int(total or 0)


# ── earning ─────────────────────────────────────────────────────────────────

async def reward_for_bet(session: AsyncSession, user_id: int, amount_usd: float) -> int:
    """Award activity points for a bet, a once-per-day streak bonus, propagate up
    the referral chain, and unlock the user's own referral past the threshold."""
    pts = POINTS_BET_BASE + int(POINTS_PER_USD * max(0.0, amount_usd))
    await award(session, user_id, pts, "bet")
    await _maybe_award_daily_streak(session, user_id)
    await _maybe_unlock(session, user_id)
    await _propagate(session, user_id, pts)
    return pts


async def _maybe_award_daily_streak(session: AsyncSession, user_id: int) -> None:
    """Award the streak bonus at most once per UTC day."""
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    already = await session.scalar(
        select(PointsLedger.id).where(
            PointsLedger.user_id == user_id, PointsLedger.reason == "streak",
            PointsLedger.created_at >= start,
        ).limit(1)
    )
    if already:
        return
    stats = await session.get(UserStats, user_id)
    await reward_for_streak(session, user_id, stats.current_streak if stats else 1)


async def reward_for_win(session: AsyncSession, user_id: int) -> None:
    await award(session, user_id, WIN_BONUS, "win")
    await _propagate(session, user_id, WIN_BONUS)


async def reward_for_streak(session: AsyncSession, user_id: int, streak: int) -> None:
    bonus = DAILY_STREAK_BONUS * min(max(streak, 0), DAILY_STREAK_CAP)
    await award(session, user_id, bonus, "streak")


async def _maybe_unlock(session: AsyncSession, user_id: int) -> None:
    ref = await session.scalar(
        select(Referral).where(Referral.invitee_id == user_id, Referral.status == "pending")
    )
    if ref is None:
        return
    stats = await session.get(UserStats, user_id)
    if stats is None or (stats.total_bets or 0) < REFERRAL_UNLOCK_BETS:
        return
    ref.status = "unlocked"
    ref.unlocked_at = datetime.now(timezone.utc)
    await award(session, ref.inviter_id, SIGNUP_BONUS, "referral_signup", ref=str(user_id))
    await award(session, user_id, SIGNUP_BONUS, "referral_welcome", ref=str(ref.inviter_id))


async def _propagate(session: AsyncSession, earner_id: int, base_pts: int) -> None:
    """Walk up the referral chain; each layer's inviter earns rate*base IF the
    edge from the current node is unlocked. Stop at the first non-unlocked edge."""
    current = earner_id
    for rate in REFERRAL_LAYER_RATES:
        node = await session.get(User, current)
        if node is None or not node.referred_by:
            return
        edge = await session.scalar(select(Referral).where(Referral.invitee_id == current))
        if edge is None or edge.status != "unlocked":
            return
        layer_pts = int(base_pts * rate)
        if layer_pts > 0:
            await award(session, node.referred_by, layer_pts, "referral", ref=str(current))
        current = node.referred_by


# ── stats for the Rewards screen ──────────────────────────────────────────────

async def referral_stats(session: AsyncSession, user: User) -> dict:
    direct = await session.scalar(
        select(func.count()).select_from(Referral).where(Referral.inviter_id == user.id)
    ) or 0
    unlocked = await session.scalar(
        select(func.count()).select_from(Referral).where(Referral.inviter_id == user.id, Referral.status == "unlocked")
    ) or 0
    # indirect = referrals whose inviter was referred by this user (one level deep proxy)
    direct_ids = list(await session.scalars(select(Referral.invitee_id).where(Referral.inviter_id == user.id)))
    indirect = 0
    if direct_ids:
        indirect = await session.scalar(
            select(func.count()).select_from(Referral).where(Referral.inviter_id.in_(direct_ids))
        ) or 0
    referral_pts = await session.scalar(
        select(func.coalesce(func.sum(PointsLedger.delta), 0)).where(
            PointsLedger.user_id == user.id,
            PointsLedger.reason.in_(("referral", "referral_signup")),
        )
    ) or 0
    return {
        "direct": int(direct),
        "unlocked": int(unlocked),
        "indirect": int(indirect),
        "referral_points": int(referral_pts),
        "balance": await balance(session, user.id),
    }
