"""Button-driven position management: /manage lists positions, each with
[Sell 50%] [Close] buttons that route through confirm.request().

Token ids are too long for callback_data (64-byte limit), so we stash an index→
(token, size) map in user_data and put only the index + percentage in the
callback. Owns callbacks ``^pos:``.
"""

from __future__ import annotations

import asyncio
import logging

from telegram import InlineKeyboardButton, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from bot.handlers import common, confirm
from polymarket.credentials import NoAccountConnected

logger = logging.getLogger(__name__)


def _rows(positions):
    rows = positions
    if isinstance(positions, dict):
        rows = positions.get("data") or positions.get("positions") or []
    return rows if isinstance(rows, list) else []


def _field(row: dict, *names):
    for n in names:
        if row.get(n) not in (None, ""):
            return row.get(n)
    return None


async def manage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = common.db_user_id(context)
    try:
        pm = await common.manager(context).get_readonly_client(user_id)
        positions = await asyncio.to_thread(pm.get_positions)
    except NoAccountConnected:
        await common.reply(update, context, "bot.error.no_account")
        return
    except Exception as exc:  # noqa: BLE001
        logger.warning("manage: positions fetch failed: %s", type(exc).__name__)
        await common.reply(update, context, "bot.error.generic")
        return

    rows = _rows(positions)
    payloads: list[tuple[str, float]] = []
    keyboards = []
    lines = [common.tr(context, "bot.inquiry.positions_header")]
    for row in rows[:15]:
        if not isinstance(row, dict):
            continue
        token = _field(row, "asset", "token_id", "tokenId")
        if not token:
            continue
        try:
            size = float(_field(row, "size") or 0)
        except (TypeError, ValueError):
            size = 0.0
        if size <= 0:
            continue
        title = _field(row, "title") or _field(row, "outcome") or token[:10]
        idx = len(payloads)
        payloads.append((token, size))
        lines.append(f"{idx + 1}. {title} — {size:g}")
        keyboards.append([
            InlineKeyboardButton(common.tr(context, "bot.trade.sell_pct", pct=50), callback_data=f"pos:{idx}:50"),
            InlineKeyboardButton(common.tr(context, "bot.trade.close_full"), callback_data=f"pos:{idx}:100"),
        ])

    if not payloads:
        await common.reply(update, context, "bot.inquiry.no_positions")
        return

    common.stash(context, "pos_tokens", payloads)
    await update.effective_message.reply_text(
        "\n".join(lines), parse_mode="Markdown", reply_markup=common.with_nav(context, keyboards)
    )


async def on_position_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split(":")
    if len(parts) != 3:
        return
    _, idx, pct_s = parts
    entry = common.from_stash(context, "pos_tokens", idx)
    if entry is None:
        await query.message.reply_text(common.tr(context, "bot.confirm.expired"))
        return
    token, size = entry
    try:
        pct = int(pct_s)
    except ValueError:
        return

    short = common.short(token)
    if pct >= 100:
        intent = confirm.make_intent("close", side="sell", token_id=token, size=size)
        await confirm.request(update, context, intent, "bot.confirm.close", token=short)
    else:
        sell_size = round(size * pct / 100.0, 6)
        intent = confirm.make_intent("market", side="sell", token_id=token, amount=sell_size)
        await confirm.request(update, context, intent, "bot.confirm.marketsell", amount=sell_size, token=short)


def register(application: Application) -> None:
    application.add_handler(CommandHandler("manage", manage))
    application.add_handler(CallbackQueryHandler(on_position_action, pattern="^pos:"))
