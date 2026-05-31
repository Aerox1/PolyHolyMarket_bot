"""Shared helpers for all bot handlers.

Conventions:
* ``application.bot_data["account_manager"]`` holds the AccountManager.
* The middleware (see ``bot.middleware``) runs first on every update and caches
  ``context.user_data["lang"]`` and ``context.user_data["db_user_id"]`` (the
  internal users.id, NOT the Telegram id). Handlers pass ``db_user_id`` to the
  AccountManager / repositories.
"""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from core.config import settings
from core.i18n import normalize_lang, t
from polymarket.account_manager import AccountManager


def manager(context: ContextTypes.DEFAULT_TYPE) -> AccountManager:
    return context.application.bot_data["account_manager"]


def lang_of(context: ContextTypes.DEFAULT_TYPE) -> str:
    return normalize_lang(context.user_data.get("lang", settings.default_language))


def db_user_id(context: ContextTypes.DEFAULT_TYPE) -> int | None:
    """Internal users.id cached by the middleware (None if not yet loaded)."""
    return context.user_data.get("db_user_id")


def tr(context: ContextTypes.DEFAULT_TYPE, key: str, **variables) -> str:
    """Translate a key in the current user's language."""
    return t(key, lang_of(context), **variables)


async def reply(update: Update, context: ContextTypes.DEFAULT_TYPE, key: str, **variables) -> None:
    """Reply to the effective message with a translated, Markdown-formatted string."""
    msg = update.effective_message
    if msg is not None:
        await msg.reply_text(tr(context, key, **variables), parse_mode="Markdown")
