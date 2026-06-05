"""Gemini spend ledger — the weekly budget is computed from this table.

Uses a rolling 7-day window. Async (Gemini client/webapp) + sync (dashboard).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from db.models import GeminiUsage


def _window_start() -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=7)


async def weekly_spend(session: AsyncSession) -> float:
    total = await session.scalar(
        select(func.coalesce(func.sum(GeminiUsage.cost_usd), 0)).where(GeminiUsage.ts >= _window_start())
    )
    return float(total or 0)


async def weekly_image_spend(session: AsyncSession) -> float:
    """Rolling-7d spend on paid IMAGE generation only — gates the image budget."""
    total = await session.scalar(
        select(func.coalesce(func.sum(GeminiUsage.cost_usd), 0))
        .where(GeminiUsage.ts >= _window_start(), GeminiUsage.kind == "image")
    )
    return float(total or 0)


async def weekly_text_spend(session: AsyncSession) -> float:
    """Rolling-7d spend on news TEXT (every non-image kind) — gates the SEPARATE
    text budget, never the image budget. Claude text is subscription (notional cost)."""
    total = await session.scalar(
        select(func.coalesce(func.sum(GeminiUsage.cost_usd), 0))
        .where(GeminiUsage.ts >= _window_start(), GeminiUsage.kind != "image")
    )
    return float(total or 0)


async def record(session: AsyncSession, *, category_id: int | None, cost_usd: float,
                 model: str | None, ok: bool, kind: str = "image") -> None:
    session.add(GeminiUsage(category_id=category_id, cost_usd=cost_usd, model=model, ok=ok, kind=kind))


def weekly_spend_sync(session: Session) -> float:
    total = session.scalar(
        select(func.coalesce(func.sum(GeminiUsage.cost_usd), 0)).where(GeminiUsage.ts >= _window_start())
    )
    return float(total or 0)


def image_count_window_sync(session: Session) -> int:
    return session.scalar(
        select(func.count()).select_from(GeminiUsage).where(GeminiUsage.ts >= _window_start(), GeminiUsage.ok.is_(True))
    ) or 0
