"""Per-update preprocessing: load/create the user, cache language + internal id,
enforce the optional beta allowlist and the suspended/banned status gate.

Registered as a ``TypeHandler(Update, preprocess)`` in group -1 so it runs
before any command/conversation handler. Raising ``ApplicationHandlerStop``
prevents further handlers from running for this update.
"""

from __future__ import annotations

import logging

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
        status = user.status

    if status in (UserStatus.SUSPENDED.value, UserStatus.BANNED.value):
        key = "bot.error.suspended" if status == UserStatus.SUSPENDED.value else "bot.error.banned"
        if update.effective_message is not None:
            await update.effective_message.reply_text(t(key, context.user_data["lang"]))
        raise ApplicationHandlerStop
