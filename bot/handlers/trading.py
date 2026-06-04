"""Trading commands — build a confirmation intent, then hand off to confirm.py.

  /buy  <token_id> <price> <size>     limit buy
  /sell <token_id> <price> <size>     limit sell
  /marketbuy  <token_id> <usd>        market buy (USD)
  /marketsell <token_id> <shares>     market sell (shares)
  /close <token_id>                   market-sell the full position
  /cancel <order_id>                  cancel one order
  /cancelall                          cancel all open orders (always confirms)

These handlers never place orders directly — confirm.request() owns execution,
audit and DB logging.
"""

from __future__ import annotations

import asyncio
import logging
import math

from telegram import InlineKeyboardButton, Update
from telegram.ext import Application, CommandHandler, ContextTypes

from bot.handlers import common, confirm, positions_ui
from polymarket.credentials import NoAccountConnected, TradingUnavailable

logger = logging.getLogger(__name__)

# Sane bounds for a market BUY's USD amount — same range the Mini App / news CTAs
# enforce, so a fat-fingered command can't place a wildly oversized real order.
_MIN_BET_USD = 0.5
_MAX_BET_USD = 1000.0


def _short(token: str) -> str:  # thin alias — single implementation lives in common.short
    return common.short(token)


def _browse_kb(context: ContextTypes.DEFAULT_TYPE):
    """[🔥 Browse markets][🏠 Dashboard] — point raw-command errors at the funnel."""
    return common.with_nav(context, [[InlineKeyboardButton(
        common.tr(context, "bot.tile.trending"), callback_data="menu:trending")]])


def _floats(args: list[str], n: int) -> list[float] | None:
    if len(args) < n:
        return None
    try:
        return [float(x) for x in args[-n:]]
    except ValueError:
        return None


async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _limit(update, context, "buy")


async def sell(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _limit(update, context, "sell")


async def _limit(update: Update, context: ContextTypes.DEFAULT_TYPE, side: str) -> None:
    args = context.args or []
    if len(args) < 3:
        await common.reply(update, context, f"bot.trade.{side}_usage", reply_markup=_browse_kb(context))
        return
    token = args[0]
    nums = _floats(args, 2)
    if nums is None:
        await common.reply(update, context, "bot.trade.bad_number", reply_markup=_browse_kb(context))
        return
    price, size = nums
    if not (0 < price < 1):  # Polymarket prices are probabilities in (0,1)
        await common.reply(update, context, "bot.trade.bad_price")
        return
    if size <= 0:
        await common.reply(update, context, "bot.trade.bad_size")
        return
    intent = confirm.make_intent("limit", side=side, token_id=token, price=price, size=size)
    await confirm.request(update, context, intent, f"bot.confirm.{side}",
                          size=size, price=price, token=_short(token))


async def marketbuy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _market(update, context, "buy", "marketbuy")


async def marketsell(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # No args → show the /manage list so the user can sell with one tap instead of
    # being told to paste a token id.
    if not (context.args or []):
        await positions_ui.manage(update, context)
        return
    await _market(update, context, "sell", "marketsell")


async def _market(update: Update, context: ContextTypes.DEFAULT_TYPE, side: str, usage: str) -> None:
    args = context.args or []
    if len(args) < 2:
        await common.reply(update, context, f"bot.trade.{usage}_usage", reply_markup=_browse_kb(context))
        return
    token = args[0]
    nums = _floats(args, 1)
    if nums is None:
        await common.reply(update, context, "bot.trade.bad_number", reply_markup=_browse_kb(context))
        return
    amount = nums[0]
    if not math.isfinite(amount) or amount <= 0:
        await common.reply(update, context, "bot.trade.bad_amount")
        return
    # A market BUY's amount is USD — clamp to a sane range so a mistyped command
    # can't submit a wildly oversized real order. (A market SELL's amount is a
    # SHARE count, not USD, so the USD cap doesn't apply.)
    if side == "buy" and not (_MIN_BET_USD <= amount <= _MAX_BET_USD):
        await common.reply(update, context, "bot.trade.bad_amount")
        return
    intent = confirm.make_intent("market", side=side, token_id=token, amount=amount)
    key = "bot.confirm.marketbuy" if side == "buy" else "bot.confirm.marketsell"
    await confirm.request(update, context, intent, key, amount=amount, token=_short(token))


async def close(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if not args:  # no token → the /manage list (one-tap close)
        await positions_ui.manage(update, context)
        return
    token = args[0]
    user_id = common.db_user_id(context)
    try:
        pm = await common.manager(context).get_readonly_client(user_id)
        positions = await asyncio.to_thread(pm.get_positions)
    except NoAccountConnected:
        await common.reply(update, context, "bot.error.no_account")
        return
    except Exception as exc:  # noqa: BLE001
        logger.warning("close: positions fetch failed: %s", type(exc).__name__)
        await common.reply(update, context, "bot.error.generic")
        return

    row = _position_row(positions, token)
    size = _to_float((row or {}).get("size"))
    if size <= 0:
        await common.reply(update, context, "bot.inquiry.no_positions")
        return
    title = (row.get("title") or row.get("outcome") or token) if row else token
    est = _to_float((row or {}).get("currentValue"))
    intent = confirm.make_intent("close", side="sell", token_id=token, size=size, title=title)
    await confirm.request(update, context, intent, "bot.confirm.close",
                          title=common.md_safe(title, 60), shares=f"{size:g}", est=f"{est:,.2f}")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if not args:
        await common.reply(update, context, "bot.trade.cancel_usage")
        return
    intent = confirm.make_intent("cancel", order_id=args[0])
    await confirm.request(update, context, intent, "bot.confirm.cancel", order_id=args[0])


async def cancelall(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    intent = confirm.make_intent("cancel_all")
    await confirm.request(update, context, intent, "bot.confirm.cancel_all")


def _position_row(positions, token: str) -> dict | None:
    rows = positions
    if isinstance(positions, dict):
        rows = positions.get("data") or positions.get("positions") or []
    if not isinstance(rows, list):
        return None
    for row in rows:
        if isinstance(row, dict):
            tid = row.get("asset") or row.get("token_id") or row.get("tokenId")
            if tid == token:
                return row
    return None


def _to_float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def register(application: Application) -> None:
    application.add_handler(CommandHandler("buy", buy))
    application.add_handler(CommandHandler("sell", sell))
    application.add_handler(CommandHandler("marketbuy", marketbuy))
    application.add_handler(CommandHandler("marketsell", marketsell))
    application.add_handler(CommandHandler("close", close))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CommandHandler("cancelall", cancelall))
