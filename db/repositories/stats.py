"""Gamification stats: daily-bet streaks, lifetime totals, and leaderboards.

``record_bet`` is called on every successful bet (bot + Mini App), so streaks
and totals stay consistent across both surfaces.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import User, UserStats

METRICS = {"bets": UserStats.total_bets, "volume": UserStats.total_volume_usd}


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _yesterday() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")


async def record_bet(session: AsyncSession, user_id: int, amount_usd: float) -> UserStats:
    """Update streak + totals for a successful bet. Idempotent within a day for
    the streak (multiple bets the same day keep the streak, bump totals)."""
    stats = await session.get(UserStats, user_id)
    if stats is None:
        stats = UserStats(user_id=user_id, current_streak=0, longest_streak=0)
        session.add(stats)

    today, yest = _today(), _yesterday()
    if stats.last_active_date == today:
        pass  # already counted today's streak
    elif stats.last_active_date == yest:
        stats.current_streak += 1
    else:
        stats.current_streak = 1
    stats.longest_streak = max(stats.longest_streak or 0, stats.current_streak)
    stats.last_active_date = today
    stats.total_bets = (stats.total_bets or 0) + 1
    stats.total_volume_usd = float(stats.total_volume_usd or 0) + max(0.0, amount_usd)
    await session.flush()
    return stats


async def get_stats(session: AsyncSession, user_id: int) -> dict:
    stats = await session.get(UserStats, user_id)
    if stats is None:
        return {"current_streak": 0, "longest_streak": 0, "total_bets": 0, "total_volume_usd": 0.0, "rank_bets": None}
    # rank by total_bets (1 = most)
    higher = await session.scalar(
        select(func.count()).select_from(UserStats).where(UserStats.total_bets > stats.total_bets)
    )
    return {
        "current_streak": stats.current_streak,
        "longest_streak": stats.longest_streak,
        "total_bets": stats.total_bets,
        "total_volume_usd": float(stats.total_volume_usd or 0),
        "rank_bets": int(higher or 0) + 1,
    }


def _display_name(user: User) -> str:
    return user.username or user.first_name or f"Player {user.id}"


async def leaderboard(session: AsyncSession, metric: str = "bets", limit: int = 20) -> list[dict]:
    col = METRICS.get(metric, UserStats.total_bets)
    rows = await session.execute(
        select(UserStats, User).join(User, User.id == UserStats.user_id)
        .order_by(col.desc()).limit(limit)
    )
    out = []
    for rank, (stats, user) in enumerate(rows.all(), start=1):
        out.append({
            "rank": rank,
            "name": _display_name(user),
            "bets": stats.total_bets,
            "volume_usd": float(stats.total_volume_usd or 0),
            "streak": stats.current_streak,
        })
    return out
