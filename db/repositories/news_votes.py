"""Inline news-poll vote repository (async).

The channel news card carries callback vote buttons (sentiment poll). A tap calls
``cast_vote`` (one row per Telegram account per item; a re-tap switches the
outcome), then ``tallies`` recomputes the per-outcome counts so the card's keyboard
can be re-rendered with live percentages. Sentiment only — placing a real bet stays
on the card's deep-link buttons.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import NewsPollVote


async def cast_vote(session: AsyncSession, *, item_id: int, tg_user_id: int, outcome_index: int) -> None:
    """Record (or move) a user's vote on an item. Idempotent per
    ``(item_id, tg_user_id)``: a second tap on the same option is a no-op, a tap on a
    different option switches it. Caller flushes/commits via the session scope."""
    row = await session.get(NewsPollVote, (item_id, tg_user_id))
    if row is None:
        session.add(NewsPollVote(news_item_id=item_id, tg_user_id=tg_user_id, outcome_index=outcome_index))
    elif row.outcome_index != outcome_index:
        row.outcome_index = outcome_index


async def tallies(session: AsyncSession, item_id: int) -> dict[int, int]:
    """``{outcome_index: vote_count}`` for an item (omitting outcomes with no votes)."""
    rows = await session.execute(
        select(NewsPollVote.outcome_index, func.count())
        .where(NewsPollVote.news_item_id == item_id)
        .group_by(NewsPollVote.outcome_index)
    )
    return {idx: n for idx, n in rows.all()}
