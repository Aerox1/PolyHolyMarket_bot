"""Per-update preprocessing: load/create the user, cache language + internal id,
enforce the optional beta allowlist and the suspended/banned status gate.

Registered as a ``TypeHandler(Update, preprocess)`` in group -1 so it runs
before any command/conversation handler. Raising ``ApplicationHandlerStop``
prevents further handlers from running for this update.
"""

from __future__ import annotations

import logging
import time

from sqlalchemy import func
from telegram import Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

from core.config import settings
from core.i18n import t
from db.engine import async_session_scope
from db.models import UserStatus
from db.repositories import users as users_repo
from bot.ratelimit import RateLimiter

logger = logging.getLogger(__name__)

_rate_limiter = RateLimiter(max_events=25, window_seconds=10.0)


async def preprocess(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg = update.effective_user
    if tg is None or tg.is_bot:
        return

    # Optional allowlist (private beta). Empty = open to everyone.
    allowed = settings.allowed_user_ids
    if allowed and tg.id not in allowed:
        raise ApplicationHandlerStop

    # Per-user rate limit (abuse / flood protection).
    if not _rate_limiter.allow(tg.id):
        if update.effective_message is not None:
            lang = context.user_data.get("lang", settings.default_language)
            await update.effective_message.reply_text(t("bot.error.rate_limited", lang))
        raise ApplicationHandlerStop

    # Throttle the per-user DB round-trip: load/create + last_seen + status refresh
    # at most once per ``middleware_sync_seconds``. Between syncs we reuse the cached
    # db_user_id/lang/status, so the common case (rapid taps/messages) does ZERO DB
    # writes. Trade-off: ban/suspend enforcement + last_seen lag up to that window.
    now = time.monotonic()
    cached_uid = context.user_data.get("db_user_id")
    last_sync = context.user_data.get("_db_sync_at", 0.0)
    if cached_uid is not None and (now - last_sync) < settings.middleware_sync_seconds:
        status = context.user_data.get("_status", UserStatus.ACTIVE.value)
    else:
        async with async_session_scope() as session:
            user = await users_repo.get_or_create_user(
                session,
                telegram_id=tg.id,
                username=tg.username,
                first_name=tg.first_name,
                default_language=settings.default_language,
            )
            user.last_seen_at = func.now()
            context.user_data["db_user_id"] = user.id
            context.user_data["lang"] = user.language
            context.user_data["_status"] = user.status
            context.user_data["_db_sync_at"] = now
            status = user.status

    if status in (UserStatus.SUSPENDED.value, UserStatus.BANNED.value):
        key = "bot.error.suspended" if status == UserStatus.SUSPENDED.value else "bot.error.banned"
        if update.effective_message is not None:
            await update.effective_message.reply_text(t(key, context.user_data["lang"]))
        raise ApplicationHandlerStop
