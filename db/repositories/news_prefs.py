"""Per-user news preferences + topic follows (async)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Category, UserNewsPrefs, UserTopicFollow

DELIVERY_MODES = ("off", "daily", "realtime")


async def get_or_create(session: AsyncSession, user_id: int) -> UserNewsPrefs:
    prefs = await session.get(UserNewsPrefs, user_id)
    if prefs is None:
        prefs = UserNewsPrefs(user_id=user_id)
        session.add(prefs)
        await session.flush()
    return prefs


async def set_delivery(session: AsyncSession, user_id: int, mode: str) -> None:
    if mode in DELIVERY_MODES:
        (await get_or_create(session, user_id)).delivery = mode


async def set_digest_hour(session: AsyncSession, user_id: int, hour: int) -> None:
    (await get_or_create(session, user_id)).digest_hour = max(0, min(int(hour), 23))


async def toggle_relevant(session: AsyncSession, user_id: int) -> bool:
    prefs = await get_or_create(session, user_id)
    prefs.only_relevant = not prefs.only_relevant
    return prefs.only_relevant


async def mark_digest_sent(session: AsyncSession, user_id: int, when: datetime) -> None:
    (await get_or_create(session, user_id)).last_digest_at = when


async def set_quiet_hours(session: AsyncSession, user_id: int, start: int | None, end: int | None) -> None:
    """Set (or clear, when start/end is None) the realtime quiet-hours window."""
    prefs = await get_or_create(session, user_id)
    prefs.quiet_start = None if start is None else max(0, min(int(start), 23))
    prefs.quiet_end = None if end is None else max(0, min(int(end), 23))


# ── topics (Category rows with kind news/both) ────────────────────────────────

async def list_news_topics(session: AsyncSession) -> list[Category]:
    return list(await session.scalars(
        select(Category).where(Category.kind.in_(("news", "both")), Category.hidden.is_(False))
        .order_by(Category.display_order.asc(), Category.title.asc())
    ))


async def followed_ids(session: AsyncSession, user_id: int) -> set[int]:
    return set(await session.scalars(
        select(UserTopicFollow.category_id).where(UserTopicFollow.user_id == user_id)
    ))


async def toggle_follow(session: AsyncSession, user_id: int, category_id: int) -> bool:
    """Follow/unfollow a topic. Returns True if now followed, False if unfollowed."""
    existing = await session.get(UserTopicFollow, (user_id, category_id))
    if existing is not None:
        await session.delete(existing)
        return False
    session.add(UserTopicFollow(user_id=user_id, category_id=category_id))
    return True
