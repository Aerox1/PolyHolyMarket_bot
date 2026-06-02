"""Append-only audit log for security-relevant events.

Records to the ``audit_log`` table. The ``detail`` payload must NEVER contain
secrets (no private keys, no API secrets) — only metadata like wallet address
(public), event type, order intent, and reason classes.

Works with both sync (dashboard/worker) and async (bot) sessions.
"""

from __future__ import annotations

import enum
import logging

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from db.models import AuditLog

logger = logging.getLogger(__name__)


class AuditEvent(str, enum.Enum):
    # ── account lifecycle ──
    ACCOUNT_CONNECT_STARTED = "ACCOUNT_CONNECT_STARTED"
    ACCOUNT_CONNECTED = "ACCOUNT_CONNECTED"
    ACCOUNT_CONNECT_FAILED = "ACCOUNT_CONNECT_FAILED"
    ACCOUNT_DISCONNECTED = "ACCOUNT_DISCONNECTED"
    KEY_MESSAGE_DELETED = "KEY_MESSAGE_DELETED"
    KEY_DECRYPTED = "KEY_DECRYPTED"
    # ── trading ──
    ORDER_SUBMIT = "ORDER_SUBMIT"
    ORDER_RESULT = "ORDER_RESULT"
    ORDER_ERROR = "ORDER_ERROR"
    CANCEL_SUBMIT = "CANCEL_SUBMIT"
    CANCEL_RESULT = "CANCEL_RESULT"
    # ── admin ──
    ADMIN_LOGIN = "ADMIN_LOGIN"
    ADMIN_LOGIN_FAIL = "ADMIN_LOGIN_FAIL"
    USER_SUSPENDED = "USER_SUSPENDED"
    USER_BANNED = "USER_BANNED"
    USER_ACTIVATED = "USER_ACTIVATED"
    BROADCAST_SENT = "BROADCAST_SENT"
    GEMINI_BUDGET_SET = "GEMINI_BUDGET_SET"
    # ── news ──
    NEWS_ITEM_APPROVED = "NEWS_ITEM_APPROVED"
    NEWS_ITEM_REJECTED = "NEWS_ITEM_REJECTED"
    NEWS_ITEM_EDITED = "NEWS_ITEM_EDITED"
    NEWS_SOURCE_ADDED = "NEWS_SOURCE_ADDED"
    NEWS_SOURCE_REMOVED = "NEWS_SOURCE_REMOVED"
    NEWS_SETTINGS_SET = "NEWS_SETTINGS_SET"


def _row(
    event: AuditEvent | str,
    *,
    actor_type: str,
    actor_id: int | None,
    user_id: int | None,
    account_id: int | None,
    detail: dict | None,
    ip: str | None,
) -> AuditLog:
    return AuditLog(
        event=event.value if isinstance(event, AuditEvent) else str(event),
        actor_type=actor_type,
        actor_id=actor_id,
        user_id=user_id,
        account_id=account_id,
        detail=detail or {},
        ip=ip,
    )


def record(
    session: Session,
    event: AuditEvent | str,
    *,
    actor_type: str = "user",
    actor_id: int | None = None,
    user_id: int | None = None,
    account_id: int | None = None,
    detail: dict | None = None,
    ip: str | None = None,
) -> None:
    """Synchronous audit write (dashboard/worker). Caller commits."""
    session.add(_row(event, actor_type=actor_type, actor_id=actor_id, user_id=user_id,
                     account_id=account_id, detail=detail, ip=ip))


async def record_async(
    session: AsyncSession,
    event: AuditEvent | str,
    *,
    actor_type: str = "user",
    actor_id: int | None = None,
    user_id: int | None = None,
    account_id: int | None = None,
    detail: dict | None = None,
    ip: str | None = None,
) -> None:
    """Async audit write (bot). Caller commits."""
    session.add(_row(event, actor_type=actor_type, actor_id=actor_id, user_id=user_id,
                     account_id=account_id, detail=detail, ip=ip))
