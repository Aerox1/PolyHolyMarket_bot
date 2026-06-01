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


# Cap the button-heavy /manage view (each position adds a 4-button row).
_MANAGE_CAP = 10


def _f(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


async def manage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = common.db_user_id(context)
    await common.typing(update, context)
    try:
        pm = await common.manager(context).get_readonly_client(user_id)
        positions = await asyncio.to_thread(pm.get_positions)
    except NoAccountConnected:
        await common.reply(update, context, "bot.error.no_account",
                           reply_markup=common.connect_keyboard(context))
        return
    except Exception as exc:  # noqa: BLE001
        logger.warning("manage: positions fetch failed: %s", type(exc).__name__)
        await common.reply(update, context, "bot.error.generic")
        return

    rows = _rows(positions)
    payloads: list[tuple[str, float, str, float]] = []
    keyboards = []
    header = common.tr(context, "bot.inquiry.positions_header").replace("*", "").replace("`", "")
    lines = [f"<b>{common.esc(header)}</b>", ""]
    total_valid = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        token = _field(row, "asset", "token_id", "tokenId")
        if not token:
            continue
        size = _f(_field(row, "size"))
        if size <= 0:
            continue
        total_valid += 1
        if len(payloads) >= _MANAGE_CAP:
            continue
        title = _field(row, "title") or _field(row, "outcome") or token[:10]
        outcome = _field(row, "outcome") or ""
        avg = _f(_field(row, "avgPrice"))
        value = _f(_field(row, "currentValue"))
        pct_pnl = _f(_field(row, "percentPnl"))
        emoji = "🟢" if pct_pnl >= 0 else "🔴"
        idx = len(payloads)
        payloads.append((token, size, title, value))
        lines.append(
            f"<b>{idx + 1}. {common.esc(title)}</b>\n"
            f"   {common.esc(outcome)} · {size:g} @ ${avg:.4f} · ${value:,.2f} ({emoji} {pct_pnl:+.1f}%)"
        )
        keyboards.append([
            InlineKeyboardButton("25%", callback_data=f"pos:{idx}:25"),
            InlineKeyboardButton("50%", callback_data=f"pos:{idx}:50"),
            InlineKeyboardButton("75%", callback_data=f"pos:{idx}:75"),
            InlineKeyboardButton(common.tr(context, "bot.trade.close_full"), callback_data=f"pos:{idx}:100"),
        ])

    if not payloads:
        await common.reply(update, context, "bot.inquiry.no_positions", reply_markup=common.with_nav(
            context, [[InlineKeyboardButton(common.tr(context, "bot.tile.trending"), callback_data="menu:trending")]]))
        return

    if total_valid > len(payloads):
        note = common.tr(context, "bot.inquiry.showing", n=len(payloads), total=total_valid).replace("_", "")
        lines += ["", f"<i>{common.esc(note)}</i>"]

    common.stash(context, "pos_tokens", payloads)
    await common.screen(update, context, text="\n".join(lines), reply_markup=common.with_nav(context, keyboards))


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
    token, size, title, value = entry
    try:
        pct = int(pct_s)
    except ValueError:
        return

    safe_title = common.md_safe(title, 60)
    if pct >= 100:
        intent = confirm.make_intent("close", side="sell", token_id=token, size=size, title=title)
        await confirm.request(update, context, intent, "bot.confirm.close",
                              title=safe_title, shares=f"{size:g}", est=f"{value:,.2f}")
    else:
        sell_size = round(size * pct / 100.0, 6)
        est = value * pct / 100.0
        intent = confirm.make_intent("market", side="sell", token_id=token, amount=sell_size, title=title)
        await confirm.request(update, context, intent, "bot.confirm.sell_pos",
                              pct=pct, title=safe_title, shares=f"{sell_size:g}", est=f"{est:,.2f}")


def register(application: Application) -> None:
    application.add_handler(CommandHandler("manage", manage))
    application.add_handler(CallbackQueryHandler(on_position_action, pattern="^pos:"))
