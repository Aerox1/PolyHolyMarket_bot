"""Runtime key/value config (e.g. the live Gemini weekly budget).

Provides async (webapp/worker) and sync (dashboard) accessors over the same
``app_config`` table so admins can change settings without a redeploy.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from db.models import AppConfig

GEMINI_WEEKLY_BUDGET = "gemini_weekly_budget_usd"


# ── async (webapp / worker) ───────────────────────────────────────────────────

async def get(session: AsyncSession, key: str, default: str | None = None) -> str | None:
    row = await session.get(AppConfig, key)
    return row.value if row else default


async def get_float(session: AsyncSession, key: str, default: float) -> float:
    raw = await get(session, key)
    try:
        return float(raw) if raw is not None else default
    except (TypeError, ValueError):
        return default


async def set_(session: AsyncSession, key: str, value: str) -> None:
    row = await session.get(AppConfig, key)
    if row is None:
        session.add(AppConfig(key=key, value=value))
    else:
        row.value = value


# ── sync (dashboard) ──────────────────────────────────────────────────────────

def get_sync(session: Session, key: str, default: str | None = None) -> str | None:
    row = session.get(AppConfig, key)
    return row.value if row else default


def get_float_sync(session: Session, key: str, default: float) -> float:
    raw = get_sync(session, key)
    try:
        return float(raw) if raw is not None else default
    except (TypeError, ValueError):
        return default


def set_sync(session: Session, key: str, value: str) -> None:
    row = session.get(AppConfig, key)
    if row is None:
        session.add(AppConfig(key=key, value=value))
    else:
        row.value = value
