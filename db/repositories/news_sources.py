"""News source repository (async). Sources are admin-curated RSS/HTML feeds the
crawler polls; the dashboard CRUDs them (Phase 3)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import NewsSource


async def enabled(session: AsyncSession, limit: int = 200) -> list[NewsSource]:
    return list(await session.scalars(
        select(NewsSource).where(NewsSource.enabled.is_(True)).order_by(NewsSource.id.asc()).limit(limit)
    ))


async def get(session: AsyncSession, source_id: int) -> NewsSource | None:
    return await session.get(NewsSource, source_id)


async def mark_checked(session: AsyncSession, source_id: int, status: str) -> None:
    src = await session.get(NewsSource, source_id)
    if src is None:
        return
    src.last_checked_at = datetime.now(timezone.utc)
    src.last_status = (status or "")[:64]
