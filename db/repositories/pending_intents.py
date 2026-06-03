"""Pending bet intents — a bet a user meant to place from a news-channel CTA but
couldn't yet (not connected). Stored so it survives the connect conversation (which
wipes user_data) and a restart, then resumes on the amount picker after onboarding.

NEVER stores secrets. The intent is informational only — the bet is placed through
the normal confirm path after the user re-selects an amount (no auto-placement)."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import PendingIntent


def _idem_key(user_id: int, news_item_id: int | None, outcome: str) -> str:
    return hashlib.sha256(f"{user_id}:{news_item_id}:{outcome.upper()}".encode()).hexdigest()


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes (stored UTC) — normalize to tz-aware UTC."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


async def upsert_intent(
    session: AsyncSession, *, user_id: int, news_item_id: int | None, market_id: str,
    outcome: str, question: str | None = None, source: str = "news", ttl_hours: int = 24,
) -> PendingIntent:
    """Create or refresh a pending intent. Idempotent on (user, item, outcome): a
    repeat tap of the SAME outcome updates the existing row; tapping the OTHER
    outcome adds a second row (resume picks the newest = last-tap-wins)."""
    outcome = outcome.upper()
    key = _idem_key(user_id, news_item_id, outcome)
    expires = datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
    row = await session.scalar(select(PendingIntent).where(PendingIntent.idempotency_key == key))
    if row is None:
        row = PendingIntent(
            user_id=user_id, news_item_id=news_item_id, market_id=market_id, outcome=outcome,
            question=question, source=source, status="pending", idempotency_key=key,
            expires_at=expires)
        session.add(row)
    else:
        row.market_id = market_id
        row.question = question
        row.status = "pending"
        row.expires_at = expires
    await session.flush()
    return row


async def latest_pending(session: AsyncSession, user_id: int) -> PendingIntent | None:
    """Newest non-expired ``pending`` intent for a user (last-tap-wins)."""
    now = datetime.now(timezone.utc)
    rows = await session.scalars(
        select(PendingIntent)
        .where(PendingIntent.user_id == user_id, PendingIntent.status == "pending")
        .order_by(PendingIntent.id.desc())
    )
    for r in rows:
        exp = _aware(r.expires_at)
        if exp is None or exp > now:
            return r
    return None


async def mark(session: AsyncSession, intent_id: int, status: str) -> None:
    row = await session.get(PendingIntent, intent_id)
    if row is not None:
        row.status = status


async def expire_stale(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Mark past-TTL ``pending``/``resumed`` rows ``expired``. Returns the count."""
    now = now or datetime.now(timezone.utc)
    rows = list(await session.scalars(
        select(PendingIntent).where(PendingIntent.status.in_(("pending", "resumed")))
    ))
    touched = 0
    for r in rows:
        exp = _aware(r.expires_at)
        if exp is not None and exp <= now:
            r.status = "expired"
            touched += 1
    return touched
