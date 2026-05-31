"""Cross-process command queue (async) — the bot consumes what the dashboard
enqueues (e.g. broadcasts). Replaces Polygen's commands/ directory."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Command, User


async def pending(session: AsyncSession, action: str | None = None, limit: int = 100) -> list[Command]:
    stmt = select(Command).where(Command.status == "pending").order_by(Command.requested_at).limit(limit)
    if action:
        stmt = stmt.where(Command.action == action)
    return list(await session.scalars(stmt))


async def telegram_id_for(session: AsyncSession, user_id: int) -> int | None:
    user = await session.get(User, user_id)
    return user.telegram_id if user else None


async def mark(session: AsyncSession, command_id: int, status: str) -> None:
    cmd = await session.get(Command, command_id)
    if cmd:
        cmd.status = status
        cmd.processed_at = datetime.now(timezone.utc)
