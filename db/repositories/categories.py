"""Category repository (async — webapp + sync job).

Categories are Polymarket tags surfaced as swipeable cards. Ordering: pinned
first, then admin display_order, then volume desc. Hidden categories are never
shown in the Mini App.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Category


async def list_visible(session: AsyncSession, limit: int = 50) -> list[Category]:
    stmt = (
        select(Category)
        .where(Category.hidden.is_(False))
        .order_by(Category.pinned.desc(), Category.display_order.asc(), Category.volume.desc())
        .limit(limit)
    )
    return list(await session.scalars(stmt))


async def get(session: AsyncSession, category_id: int) -> Category | None:
    return await session.get(Category, category_id)


async def get_by_slug(session: AsyncSession, slug: str) -> Category | None:
    return await session.scalar(select(Category).where(Category.slug == slug))


async def upsert_from_tag(
    session: AsyncSession, *, slug: str, title: str, tag_id: str | None, tag_slug: str | None, volume: float
) -> Category:
    cat = await get_by_slug(session, slug)
    if cat is None:
        cat = Category(slug=slug, title=title, tag_id=tag_id, tag_slug=tag_slug, volume=volume)
        session.add(cat)
        await session.flush()
    else:
        cat.title = title
        cat.tag_id = tag_id
        cat.tag_slug = tag_slug
        cat.volume = volume
    return cat


async def set_image(session: AsyncSession, category_id: int, *, path: str | None, status: str,
                    prompt: str | None = None) -> None:
    cat = await session.get(Category, category_id)
    if cat is None:
        return
    cat.image_status = status
    if path is not None:
        cat.image_path = path
    if prompt is not None:
        cat.image_prompt = prompt
    if status == "ready":
        cat.image_generated_at = datetime.now(timezone.utc)


async def needing_images(session: AsyncSession, limit: int = 20) -> list[Category]:
    """Visible categories without a ready image (for the generation job)."""
    stmt = (
        select(Category)
        .where(Category.hidden.is_(False), Category.image_status.in_(("none", "failed")))
        .order_by(Category.pinned.desc(), Category.volume.desc())
        .limit(limit)
    )
    return list(await session.scalars(stmt))
