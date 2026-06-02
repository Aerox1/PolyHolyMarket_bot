"""News item repository (async). The crawler creates backlog items (deduped by
url_hash); the render job drains approved items."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import NewsItem

# statuses the render job processes (admin-approved → through transient states)
RENDERABLE = ("approved", "translating", "rendering")


async def exists_by_url_hash(session: AsyncSession, url_hash: str) -> bool:
    return (await session.scalar(select(NewsItem.id).where(NewsItem.url_hash == url_hash))) is not None


async def exists_by_dedup_hash(session: AsyncSession, dedup_hash: str | None) -> bool:
    """True if a story with the same normalized-title hash already exists
    (cross-source repost dedup). None/empty never matches."""
    if not dedup_hash:
        return False
    return (await session.scalar(select(NewsItem.id).where(NewsItem.dedup_hash == dedup_hash))) is not None


async def create(
    session: AsyncSession, *, url: str, url_hash: str, title_orig: str,
    body_orig: str | None = None, lang_orig: str | None = None, hero_image_url: str | None = None,
    source_id: int | None = None, category_id: int | None = None, dedup_hash: str | None = None,
    score: float = 0.0,
) -> NewsItem:
    item = NewsItem(
        url=url, url_hash=url_hash, title_orig=title_orig, body_orig=body_orig,
        lang_orig=lang_orig, hero_image_url=hero_image_url, source_id=source_id,
        category_id=category_id, dedup_hash=dedup_hash, score=score,
    )
    session.add(item)
    await session.flush()
    return item


async def needing_render(session: AsyncSession, limit: int = 20) -> list[NewsItem]:
    return list(await session.scalars(
        select(NewsItem).where(NewsItem.status.in_(RENDERABLE))
        .order_by(NewsItem.score.desc(), NewsItem.id.asc()).limit(limit)
    ))


async def ready_to_publish(session: AsyncSession, limit: int = 20) -> list[NewsItem]:
    return list(await session.scalars(
        select(NewsItem).where(NewsItem.status == "ready")
        .order_by(NewsItem.score.desc(), NewsItem.id.asc()).limit(limit)
    ))
