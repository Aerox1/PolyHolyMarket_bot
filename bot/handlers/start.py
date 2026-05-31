"""Onboarding entry point: /start, language picker, and the main menu.

Callback ownership (see project rules):
    * ``^lang:``               — language selection.
    * ``^menu:create$``        — "create account" instructions.
    * ``^menu:help$``          — help text.
``menu:connect`` is intentionally NOT handled here — it is owned by
``connect.py`` as a conversation entry point.
"""

from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from bot.handlers import common
from core.config import settings
from core.i18n import LANG_FLAGS, LANG_NAMES, SUPPORTED
from db.engine import async_session_scope
from db.repositories import users as users_repo

logger = logging.getLogger(__name__)


# ── Keyboards ─────────────────────────────────────────────────────────────────


def language_keyboard() -> InlineKeyboardMarkup:
    """Inline keyboard with one button per supported language (flag + name)."""
    rows = [
        [
            InlineKeyboardButton(
                f"{LANG_FLAGS.get(code, '')} {LANG_NAMES.get(code, code)}".strip(),
                callback_data=f"lang:{code}",
            )
        ]
        for code in SUPPORTED
    ]
    return InlineKeyboardMarkup(rows)


def main_menu_keyboard(lang: str) -> InlineKeyboardMarkup:
    """The main menu inline keyboard, localised to ``lang``."""
    from core.i18n import t

    rows = [
        [InlineKeyboardButton(t("bot.menu.connect", lang), callback_data="menu:connect")],
        [InlineKeyboardButton(t("bot.menu.create", lang), callback_data="menu:create")],
        [InlineKeyboardButton(t("bot.menu.help", lang), callback_data="menu:help")],
    ]
    return InlineKeyboardMarkup(rows)


# ── Internal helpers ────────────────────────────────────────────────────────


async def _send_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the main-menu prompt + keyboard to the effective chat."""
    msg = update.effective_message
    if msg is None:
        return
    await msg.reply_text(
        common.tr(context, "bot.start.menu_prompt"),
        reply_markup=main_menu_keyboard(common.lang_of(context)),
        parse_mode="Markdown",
    )


def _help_text_key() -> str:
    return "bot.start.help_text"


# ── Handlers ──────────────────────────────────────────────────────────────────


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start — greet the user and present the language picker."""
    tg = update.effective_user
    msg = update.effective_message
    if msg is None:
        return
    name = (tg.first_name if tg else None) or ""
    try:
        await msg.reply_text(
            common.tr(context, "bot.start.welcome", name=name),
            parse_mode="Markdown",
        )
        await msg.reply_text(
            common.tr(context, "bot.start.choose_language"),
            reply_markup=language_keyboard(),
            parse_mode="Markdown",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("start failed: %s", type(exc).__name__)
        await common.reply(update, context, "bot.error.generic")


async def on_language_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``^lang:`` — persist the chosen language, then show the main menu."""
    query = update.callback_query
    if query is None:
        return
    try:
        await query.answer()
        code = (query.data or "").split(":", 1)[1] if ":" in (query.data or "") else ""
        if code not in SUPPORTED:
            return

        tg = update.effective_user
        if tg is not None:
            async with async_session_scope() as session:
                await users_repo.set_language(session, tg.id, code)
        context.user_data["lang"] = code

        await query.edit_message_text(
            common.tr(context, "bot.start.language_set"),
            parse_mode="Markdown",
        )
        await _send_main_menu(update, context)
    except Exception as exc:  # noqa: BLE001
        logger.warning("on_language_choice failed: %s", type(exc).__name__)
        await common.reply(update, context, "bot.error.generic")


async def on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``^menu:(create|help)$`` — create-account instructions or help text."""
    query = update.callback_query
    if query is None:
        return
    try:
        await query.answer()
        action = (query.data or "").split(":", 1)[1] if ":" in (query.data or "") else ""
        msg = update.effective_message
        if msg is None:
            return
        if action == "create":
            await msg.reply_text(
                common.tr(context, "bot.create.instructions", url=settings.polymarket_signup_url),
                parse_mode="Markdown",
                disable_web_page_preview=False,
            )
        elif action == "help":
            await msg.reply_text(
                common.tr(context, _help_text_key()),
                parse_mode="Markdown",
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("on_menu failed: %s", type(exc).__name__)
        await common.reply(update, context, "bot.error.generic")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/help — same content as the menu:help button."""
    try:
        await common.reply(update, context, _help_text_key())
    except Exception as exc:  # noqa: BLE001
        logger.warning("help_command failed: %s", type(exc).__name__)
        await common.reply(update, context, "bot.error.generic")


async def language_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/language — re-open the language picker."""
    msg = update.effective_message
    if msg is None:
        return
    try:
        await msg.reply_text(
            common.tr(context, "bot.settings.language_prompt"),
            reply_markup=language_keyboard(),
            parse_mode="Markdown",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("language_command failed: %s", type(exc).__name__)
        await common.reply(update, context, "bot.error.generic")


# ── Registration ──────────────────────────────────────────────────────────────


def register(application: Application) -> None:
    """Add this module's handlers to the PTB application."""
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("language", language_command))
    application.add_handler(CallbackQueryHandler(on_language_choice, pattern="^lang:"))
    application.add_handler(CallbackQueryHandler(on_menu, pattern="^menu:(create|help)$"))
