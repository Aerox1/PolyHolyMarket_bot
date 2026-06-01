"""Shared helpers for all bot handlers.

Conventions:
* ``application.bot_data["account_manager"]`` holds the AccountManager.
* The middleware (see ``bot.middleware``) runs first on every update and caches
  ``context.user_data["lang"]`` and ``context.user_data["db_user_id"]`` (the
  internal users.id, NOT the Telegram id). Handlers pass ``db_user_id`` to the
  AccountManager / repositories.
"""

from __future__ import annotations

import html

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ChatAction
from telegram.error import BadRequest
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


async def reply(update: Update, context: ContextTypes.DEFAULT_TYPE, key: str, *,
                reply_markup=None, disable_preview: bool = False, **variables) -> None:
    """Reply to the effective message with a translated Markdown string + optional keyboard."""
    msg = update.effective_message
    if msg is not None:
        await msg.reply_text(tr(context, key, **variables), parse_mode="Markdown",
                             reply_markup=reply_markup, disable_web_page_preview=disable_preview)


async def typing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show a 'typing…' chat action so the user gets instant feedback before a
    blocking network call. Feedback-only — never raises into the handler."""
    chat = update.effective_chat
    if chat is not None:
        try:
            await chat.send_action(ChatAction.TYPING)
        except Exception:  # noqa: BLE001
            pass


async def edit_or_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, key: str | None = None, *,
                        text: str | None = None, reply_markup=None, disable_preview: bool = False,
                        **variables) -> None:
    """Edit the originating message in place when invoked from a callback (handling
    the photo-caption case + the benign 'message is not modified' error); otherwise
    send a fresh message. Centralizes the edit-vs-reply pattern used across handlers."""
    body = text if text is not None else tr(context, key, **variables)
    query = update.callback_query
    if query is not None:
        m = query.message
        try:
            if isinstance(m, Message) and m.photo:
                await m.edit_caption(caption=body, reply_markup=reply_markup, parse_mode="Markdown")
            else:
                await query.edit_message_text(body, reply_markup=reply_markup, parse_mode="Markdown",
                                              disable_web_page_preview=disable_preview)
            return
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                return
            # otherwise fall through to a fresh send (stale/uneditable message)
    msg = update.effective_message
    if msg is not None:
        await msg.reply_text(body, parse_mode="Markdown", reply_markup=reply_markup,
                             disable_web_page_preview=disable_preview)


def esc(value) -> str:
    """HTML-escape a dynamic value for parse_mode='HTML' messages. Use this on any
    market title / outcome / user text — legacy Markdown silently breaks on * _ ` [."""
    return html.escape("" if value is None else str(value), quote=False)


_MD_STRIP = str.maketrans({c: None for c in "*_`[]"})


def md_safe(value, limit: int | None = None) -> str:
    """Make dynamic text safe to drop into a legacy-Markdown string by removing the
    chars Telegram's legacy Markdown can't escape. Use for titles in confirm prompts."""
    s = ("" if value is None else str(value)).translate(_MD_STRIP).strip()
    return s[:limit] if limit else s


async def screen(update: Update, context: ContextTypes.DEFAULT_TYPE, *, text: str,
                 reply_markup=None, parse_mode: str = "HTML", disable_preview: bool = True) -> None:
    """Render a text 'screen'. From a callback on a TEXT message, edits it in place
    (one evolving screen); from a command, a stale callback, or a PHOTO message
    (e.g. the banner dashboard) sends a fresh message rather than caption-editing it."""
    query = update.callback_query
    msg = query.message if query is not None else None
    if query is not None and isinstance(msg, Message) and not msg.photo:
        try:
            await query.edit_message_text(text, parse_mode=parse_mode, reply_markup=reply_markup,
                                          disable_web_page_preview=disable_preview)
            return
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                return
            # otherwise fall through to a fresh send
    if update.effective_message is not None:
        await update.effective_message.reply_text(text, parse_mode=parse_mode, reply_markup=reply_markup,
                                                   disable_web_page_preview=disable_preview)


# ── inline-keyboard helpers ─────────────────────────────────────────────────────

def dashboard_button(context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardButton:
    return InlineKeyboardButton(tr(context, "bot.nav.home"), callback_data="menu:home")


def back_button(context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardButton:
    return InlineKeyboardButton(tr(context, "bot.nav.back"), callback_data="menu:home")


def with_nav(context: ContextTypes.DEFAULT_TYPE, rows=None) -> InlineKeyboardMarkup:
    """Append a [🏠 Dashboard] navigation row to the given keyboard rows so no
    screen is a dead-end."""
    out = [list(r) for r in (rows or [])]
    out.append([dashboard_button(context)])
    return InlineKeyboardMarkup(out)


def connect_keyboard(context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    """[🔗 Connect][🏠 Dashboard] — shown on the 'no account connected' state."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(tr(context, "bot.menu.connect"), callback_data="menu:connect"),
        dashboard_button(context),
    ]])


def short(value: str | None, head: int = 8, tail: int = 0) -> str:
    """Shorten a long id/address. One implementation for every handler:
    ``short(tok)`` → 'abcd1234…'; ``short(addr, 6, 4)`` → '0x1234…cdef'."""
    if not value or len(value) <= head + tail + 1:
        return value or ""
    return f"{value[:head]}…{value[-tail:]}" if tail else f"{value[:head]}…"


# ── callback_data index stash (works around Telegram's 64-byte callback limit) ──

def stash(context: ContextTypes.DEFAULT_TYPE, key: str, payloads: list) -> list[str]:
    """Store payloads under ``user_data[key]`` as an index→payload map; return the
    string indices to embed in callback_data."""
    m = {str(i): p for i, p in enumerate(payloads)}
    context.user_data[key] = m
    return list(m.keys())


def from_stash(context: ContextTypes.DEFAULT_TYPE, key: str, idx) -> object | None:
    """Resolve a stashed payload by index, or None if missing/expired."""
    return (context.user_data.get(key) or {}).get(str(idx))
