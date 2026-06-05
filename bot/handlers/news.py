"""/news — per-user news delivery preferences (mode, digest hour, relevance,
followed topics). Mirrors the rewards/settings screen pattern; all state lives in
``user_news_prefs`` + ``user_topic_follows`` and is consumed by the delivery jobs.
"""

from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from bot.handlers import common
from db.engine import async_session_scope
from db.repositories import news_prefs

logger = logging.getLogger(__name__)

_MODE_KEYS = {"off": "bot.news.mode_off", "daily": "bot.news.mode_daily", "realtime": "bot.news.mode_realtime"}


def _mode_row(context, current: str) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(
        common.tr(context, key) + (" ✓" if mode == current else ""),
        callback_data=f"news:mode:{mode}") for mode, key in _MODE_KEYS.items()]


def _quiet_str(context, prefs) -> str:
    if prefs.quiet_start is None or prefs.quiet_end is None:
        return common.tr(context, "bot.news.off")
    return f"{prefs.quiet_start:02d}:00–{prefs.quiet_end:02d}:00"


async def _settings_text(context, prefs, followed_n: int) -> str:
    onoff = "bot.news.on" if prefs.only_relevant else "bot.news.off"
    return (
        f"<b>{common.esc(common.tr(context, 'bot.news.settings_title'))}</b>\n\n"
        f"{common.esc(common.tr(context, 'bot.news.mode'))}: "
        f"<b>{common.esc(common.tr(context, _MODE_KEYS[prefs.delivery]))}</b>\n"
        f"{common.esc(common.tr(context, 'bot.news.digest_hour'))}: <b>{prefs.digest_hour:02d}:00</b>\n"
        f"{common.esc(common.tr(context, 'bot.news.quiet'))}: <b>{common.esc(_quiet_str(context, prefs))}</b>\n"
        f"{common.esc(common.tr(context, 'bot.news.only_topics'))}: "
        f"<b>{common.esc(common.tr(context, onoff))}</b>\n"
        f"{common.esc(common.tr(context, 'bot.news.following'))}: <b>{followed_n}</b>"
    )


async def show_settings_screen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = common.db_user_id(context)
    if uid is None:
        await common.reply(update, context, "bot.error.generic")
        return
    async with async_session_scope() as s:
        prefs = await news_prefs.get_or_create(s, uid)
        followed_n = len(await news_prefs.followed_ids(s, uid))
        delivery, digest_hour = prefs.delivery, prefs.digest_hour
        quiet_label = _quiet_str(context, prefs)
        text = await _settings_text(context, prefs, followed_n)
    rows = [
        _mode_row(context, delivery),
        [InlineKeyboardButton(common.tr(context, "bot.news.digest_hour") + f": {digest_hour:02d}:00",
                              callback_data="news:hour")],
        [InlineKeyboardButton(common.tr(context, "bot.news.quiet") + f": {quiet_label}", callback_data="news:quiet")],
        [InlineKeyboardButton(common.tr(context, "bot.news.only_topics_btn"), callback_data="news:relevant")],
        [InlineKeyboardButton(common.tr(context, "bot.news.topics") + f" ({followed_n})", callback_data="news:topics")],
    ]
    await common.screen(update, context, text=text, reply_markup=common.with_nav(context, rows))


async def _show_hours(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cells = [InlineKeyboardButton(f"{h:02d}", callback_data=f"news:sethour:{h}") for h in range(24)]
    rows = [cells[i:i + 6] for i in range(0, 24, 6)]
    rows.append([InlineKeyboardButton(common.tr(context, "bot.nav.back"), callback_data="news:back")])
    await common.screen(update, context, text=common.esc(common.tr(context, "bot.news.pick_hour")),
                        reply_markup=common.with_nav(context, rows))


async def _show_topics(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = common.db_user_id(context)
    if uid is None:
        await common.reply(update, context, "bot.error.generic")
        return
    async with async_session_scope() as s:
        topics = await news_prefs.list_news_topics(s)
        followed = await news_prefs.followed_ids(s, uid)
    if not topics:
        await common.screen(update, context, text=common.esc(common.tr(context, "bot.news.no_topics")),
                            reply_markup=common.with_nav(context,
                                [[InlineKeyboardButton(common.tr(context, "bot.nav.back"), callback_data="news:back")]]))
        return
    rows = [[InlineKeyboardButton(("✅ " if c.id in followed else "▫️ ") + c.title,
                                  callback_data=f"news:topic:{c.id}")] for c in topics]
    rows.append([InlineKeyboardButton(common.tr(context, "bot.nav.back"), callback_data="news:back")])
    await common.screen(update, context, text=f"<b>{common.esc(common.tr(context, 'bot.news.topics_title'))}</b>",
                        reply_markup=common.with_nav(context, rows))


async def news_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await show_settings_screen(update, context)
    except Exception as exc:  # noqa: BLE001
        logger.warning("news_command failed: %s", type(exc).__name__)
        await common.reply(update, context, "bot.error.generic")


async def on_news(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    data = query.data or ""
    uid = common.db_user_id(context)
    try:
        await query.answer()
        if uid is None:
            return
        parts = data.split(":")
        verb = parts[1] if len(parts) > 1 else ""
        if verb == "mode":
            async with async_session_scope() as s:
                await news_prefs.set_delivery(s, uid, parts[2])
            await show_settings_screen(update, context)
        elif verb == "relevant":
            async with async_session_scope() as s:
                await news_prefs.toggle_relevant(s, uid)
            await show_settings_screen(update, context)
        elif verb == "quiet":
            # toggle between Off and a sensible overnight window (22:00–07:00)
            async with async_session_scope() as s:
                prefs = await news_prefs.get_or_create(s, uid)
                if prefs.quiet_start is None:
                    await news_prefs.set_quiet_hours(s, uid, 22, 7)
                else:
                    await news_prefs.set_quiet_hours(s, uid, None, None)
            await show_settings_screen(update, context)
        elif verb == "hour":
            await _show_hours(update, context)
        elif verb == "sethour":
            async with async_session_scope() as s:
                await news_prefs.set_digest_hour(s, uid, int(parts[2]))
            await show_settings_screen(update, context)
        elif verb == "topics":
            await _show_topics(update, context)
        elif verb == "topic":
            async with async_session_scope() as s:
                await news_prefs.toggle_follow(s, uid, int(parts[2]))
            await _show_topics(update, context)
        elif verb == "back":
            await show_settings_screen(update, context)
    except Exception as exc:  # noqa: BLE001
        logger.warning("on_news(%s) failed: %s", data, type(exc).__name__)
        await common.reply(update, context, "bot.error.generic")


def register(application: Application) -> None:
    application.add_handler(CommandHandler("news", news_command))
    application.add_handler(CallbackQueryHandler(on_news, pattern=r"^news:"))
