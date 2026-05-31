"""Public market-discovery commands: /trending and /categories.

These need no connected account — they read public Polymarket data. Blocking
calls run in a worker thread.
"""

from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from bot.handlers import common
from polymarket import markets

logger = logging.getLogger(__name__)


def _pct(p) -> str:
    try:
        return f"{round(float(p) * 100)}%"
    except (TypeError, ValueError):
        return "—"


def _vol(v) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "$0"
    if v >= 1_000_000:
        return f"${v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"${v / 1_000:.0f}K"
    return f"${v:.0f}"


async def trending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    try:
        mkts = await asyncio.to_thread(markets.trending_markets, 12)
    except Exception as exc:  # noqa: BLE001
        logger.warning("trending failed: %s", type(exc).__name__)
        await common.reply(update, context, "bot.error.generic")
        return
    if not mkts:
        await common.reply(update, context, "bot.discover.none")
        return
    lines = [common.tr(context, "bot.discover.trending_header")]
    for i, m in enumerate(mkts, 1):
        q = (m.get("question") or "")[:70]
        lines.append(
            common.tr(context, "bot.discover.market_line", n=i, q=q,
                      yes=_pct(m.get("yes_price")), no=_pct(m.get("no_price")), vol=_vol(m.get("volume")))
        )
        lines.append(f"`{m.get('id')}`")
    lines.append(common.tr(context, "bot.discover.trade_hint"))
    await msg.reply_text("\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True)


async def categories(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    try:
        cats = await asyncio.to_thread(markets.top_categories, 15)
    except Exception as exc:  # noqa: BLE001
        logger.warning("categories failed: %s", type(exc).__name__)
        await common.reply(update, context, "bot.error.generic")
        return
    if not cats:
        await common.reply(update, context, "bot.discover.none")
        return
    lines = [common.tr(context, "bot.discover.categories_header")]
    for i, c in enumerate(cats, 1):
        lines.append(common.tr(context, "bot.discover.category_line", n=i,
                                title=c.get("title"), vol=_vol(c.get("volume"))))
    await msg.reply_text("\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True)


def register(application: Application) -> None:
    application.add_handler(CommandHandler("trending", trending))
    application.add_handler(CommandHandler("categories", categories))
