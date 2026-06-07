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
from db.models import User, UserStatus
from db.repositories import users as users_repo
from bot import access_gate
from bot.ratelimit import RateLimiter

logger = logging.getLogger(__name__)

_rate_limiter = RateLimiter(max_events=25, window_seconds=10.0)
# Wrong-access-code attempts per user (brute-force guard on the invite gate).
_access_limiter = RateLimiter(max_events=5, window_seconds=60.0)


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
            # getattr default True: never lock out on a partial/legacy user object.
            context.user_data["_access_granted"] = getattr(user, "access_granted", True)
            context.user_data["_db_sync_at"] = now
            status = user.status

    if status in (UserStatus.SUSPENDED.value, UserStatus.BANNED.value):
        key = "bot.error.suspended" if status == UserStatus.SUSPENDED.value else "bot.error.banned"
        if update.effective_message is not None:
            await update.effective_message.reply_text(t(key, context.user_data["lang"]))
        raise ApplicationHandlerStop

    # ── access gate (invite code for new users) ──
    # Granted users (the common case) short-circuit with zero overhead; locked users
    # are blocked from every handler except code entry (handled inside the enforcer).
    if not context.user_data.get("_access_granted") and not await _enforce_access_gate(update, context, tg):
        raise ApplicationHandlerStop


async def _enforce_access_gate(update: Update, context: ContextTypes.DEFAULT_TYPE, tg) -> bool:
    """Return True if the update may proceed (gate off / user already granted / just
    unlocked via a /start invite link). Otherwise handle the locked user — validate a
    submitted code (rate-limited) or show the prompt — and return False so the caller
    stops the update."""
    uid = context.user_data.get("db_user_id")
    lang = context.user_data.get("lang", settings.default_language)
    msg = update.effective_message
    async with async_session_scope() as session:
        if not await access_gate.gate_enabled(session):
            context.user_data["_access_granted"] = True
            return True
        user = await session.get(User, uid) if uid is not None else None
        if user is None:
            return True  # can't evaluate the gate without the row — never lock anyone out
        if user.access_granted:
            context.user_data["_access_granted"] = True
            return True

        # LOCKED: the only thing that gets through is a valid code.
        code = access_gate.code_from_update(update)
        if code:
            if not _access_limiter.allow(tg.id):
                if msg is not None:
                    await msg.reply_text(t("bot.access.rate_limited", lang))
                return False
            if await access_gate.try_grant(session, user, code):
                context.user_data["_access_granted"] = True
                # a /start r-<code> invite link: let it fall through so the dashboard opens
                m_text = (update.message.text or "") if getattr(update, "message", None) else ""
                if m_text.lstrip().lower().startswith("/start"):
                    return True
                if msg is not None:
                    await msg.reply_text(t("bot.access.granted", lang))
                return False
            if msg is not None:
                await msg.reply_text(t("bot.access.invalid", lang))
            return False

        # No code offered (a command / button / blank) → show the prompt.
        if update.callback_query is not None:
            try:
                await update.callback_query.answer()
            except Exception:  # noqa: BLE001 — clearing the spinner is best-effort
                pass
        if msg is not None:
            await msg.reply_text(t("bot.access.prompt", lang))
        return False
