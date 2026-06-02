"""Per-user news delivery: who to deliver to, candidate selection (relevance +
dedup), and the delivered ledger."""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Bet, BetStatus, NewsDelivered, NewsItem, User, UserNewsPrefs, UserStatus


async def users_for(session: AsyncSession, mode: str) -> list[tuple[int, int, str]]:
    """Active users opted into ``mode``. Returns (user_id, telegram_id, language)
    tuples — plain values so the caller doesn't hold ORM objects across sends."""
    rows = await session.execute(
        select(User.id, User.telegram_id, User.language)
        .join(UserNewsPrefs, UserNewsPrefs.user_id == User.id)
        .where(UserNewsPrefs.delivery == mode, User.status == UserStatus.ACTIVE.value)
    )
    return [(r[0], r[1], r[2]) for r in rows.all()]


async def user_market_ids(session: AsyncSession, user_id: int) -> set[str]:
    """Distinct markets the user CURRENTLY holds (open bets) — the position-
    relevance signal. Settled bets are excluded so resolved markets stop
    generating 'relevant' news."""
    return set(await session.scalars(
        select(Bet.market_id.distinct()).where(
            Bet.user_id == user_id, Bet.status == BetStatus.OPEN.value)
    ))


async def candidates_for(
    session: AsyncSession, user_id: int, *, followed_ids: set[int], market_ids: set[str],
    only_relevant: bool, limit: int,
) -> list[NewsItem]:
    """Published ('sent') items not yet delivered to this user, newest first.

    Relevance = followed topic OR a market the user holds. When ``only_relevant``
    (always true for realtime), an item must match; otherwise any sent item is a
    digest candidate."""
    delivered = select(NewsDelivered.news_item_id).where(NewsDelivered.user_id == user_id)
    stmt = select(NewsItem).where(NewsItem.status == "sent", NewsItem.id.not_in(delivered))

    conds = []
    if followed_ids:
        conds.append(NewsItem.category_id.in_(followed_ids))
    if market_ids:
        conds.append(NewsItem.cta_market_id.in_(market_ids))
    if only_relevant:
        if not conds:
            return []  # wants only-relevant but follows nothing and holds nothing
        stmt = stmt.where(or_(*conds))

    stmt = stmt.order_by(NewsItem.published_at.desc(), NewsItem.score.desc()).limit(limit)
    return list(await session.scalars(stmt))


def mark_delivered(session: AsyncSession, user_id: int, item_id: int, channel: str) -> None:
    session.add(NewsDelivered(user_id=user_id, news_item_id=item_id, channel=channel))
