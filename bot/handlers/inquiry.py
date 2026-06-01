"""Inquiry handlers — read-only monitoring + public market-data commands.

Per-user, multi-language port of Polygen's ``bot/handlers/inquiry.py``. Every
command resolves the caller's internal user id and an appropriate client
(read-only for Data/Gamma/public CLOB, signing for balance/orders) via the
shared :mod:`bot.handlers.common` helpers. All blocking Polymarket calls are
pushed to a worker thread with :func:`asyncio.to_thread`. User-facing strings
come exclusively from the i18n catalog through :func:`common.tr` /
:func:`common.reply`.

Public API: only ``register(application)`` — ``main.py`` needs nothing else.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from bot.handlers import common, confirm
from polymarket.credentials import NoAccountConnected, TradingUnavailable

logger = logging.getLogger(__name__)

# How many rows to show in any list-style response before truncating.
_MAX_ROWS = 15
# How many order-book levels to show per side.
_MAX_BOOK_LEVELS = 5


# ── small formatting helpers (local) ─────────────────────────────────────────

def _send(update: Update, text: str):
    """Send an already-assembled Markdown string to the effective message."""
    msg = update.effective_message
    return msg.reply_text(text, parse_mode="Markdown")


def _as_float(value, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _fmt_money(value, decimals: int = 2) -> str:
    return f"{_as_float(value):,.{decimals}f}"


def _fmt_price(value, decimals: int = 4) -> str:
    return f"{_as_float(value):.{decimals}f}"


def _pnl_emoji(value) -> str:
    return "🟢" if _as_float(value) >= 0 else "🔴"


def _fmt_ts(ts) -> str:
    """Format a (possibly second- or millisecond-precision) unix timestamp."""
    n = _as_float(ts)
    if n <= 0:
        return "?"
    if n > 1e12:  # milliseconds
        n /= 1000.0
    try:
        return datetime.fromtimestamp(n, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return "?"


def _shorten(value, head: int = 16) -> str:
    s = str(value or "?")
    return f"{s[:head]}…" if len(s) > head else s


def _as_rows(data) -> list[dict]:
    """Data-API endpoints sometimes wrap rows under ``data``; normalise to a list."""
    if isinstance(data, dict):
        for key in ("data", "positions", "trades", "activity", "history"):
            inner = data.get(key)
            if isinstance(inner, list):
                return inner
        return []
    return data if isinstance(data, list) else []


# ── navigation + presentation helpers (Step 3) ───────────────────────────────

# Cross-link row shown under every monitoring screen so users hop without typing.
_XLINKS = [("📊", "portfolio"), ("📈", "positions"), ("📋", "orders"), ("🧾", "trades"), ("🕑", "activity")]


def _nav_rows(context: ContextTypes.DEFAULT_TYPE, current: str) -> list:
    """The cross-link row + [↻ Refresh current][🏠 Dashboard] row, as a list of rows."""
    xrow = [InlineKeyboardButton(emoji, callback_data=f"inq:{c}") for emoji, c in _XLINKS]
    bottom = [InlineKeyboardButton(common.tr(context, "bot.tile.refresh"), callback_data=f"inq:{current}"),
              common.dashboard_button(context)]
    return [xrow, bottom]


def _nav(context: ContextTypes.DEFAULT_TYPE, current: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(_nav_rows(context, current))


def _connect_kb(context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    return common.connect_keyboard(context)


def _empty_kb(context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(common.tr(context, "bot.tile.trending"), callback_data="menu:trending"),
        common.dashboard_button(context),
    ]])


def _hhead(context: ContextTypes.DEFAULT_TYPE, key: str) -> str:
    """Render a (trusted) Markdown header from the catalog as an HTML <b> heading."""
    raw = common.tr(context, key).replace("*", "").replace("`", "")
    return f"<b>{common.esc(raw)}</b>"


_ACT_EMOJI = {"TRADE": "🔁", "BUY": "🟢", "SELL": "🔴", "REDEEM": "🪙", "REWARD": "🎁",
              "SPLIT": "✂️", "MERGE": "🔗", "CONVERSION": "🔄", "CONVERT": "🔄"}


def _act_emoji(atype) -> str:
    return _ACT_EMOJI.get(str(atype).upper(), "•")


def _rel_ts(ts) -> str:
    """Relative timestamp ('3h ago'), falling back to an absolute date."""
    n = _as_float(ts)
    if n <= 0:
        return "?"
    if n > 1e12:  # milliseconds
        n /= 1000.0
    try:
        dt = datetime.fromtimestamp(n, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return "?"
    secs = (datetime.now(timezone.utc) - dt).total_seconds()
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    if secs < 7 * 86400:
        return f"{int(secs // 86400)}d ago"
    return dt.strftime("%Y-%m-%d")


async def _no_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await common.reply(update, context, "bot.error.no_account", reply_markup=_connect_kb(context))


# ── /portfolio ───────────────────────────────────────────────────────────────

async def portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = common.db_user_id(context)
    await common.typing(update, context)
    try:
        pm = await common.manager(context).get_readonly_client(user_id)
        value = await asyncio.to_thread(pm.get_portfolio_value)
        positions = await asyncio.to_thread(pm.get_positions, _MAX_ROWS, 0)
    except NoAccountConnected:
        await _no_account(update, context)
        return
    except TradingUnavailable:
        await common.reply(update, context, "bot.error.trading_unavailable")
        return
    except Exception as exc:  # noqa: BLE001
        logger.warning("portfolio failed: %s", type(exc).__name__)
        await common.reply(update, context, "bot.error.generic")
        return

    rows = _as_rows(positions)
    # Positions value: prefer an explicit field, else sum currentValue across rows.
    positions_value = _as_float(
        value.get("positionsValue")
        or value.get("positions_value")
        or value.get("positions")
        if isinstance(value, dict) else 0.0
    )
    if positions_value == 0.0 and rows:
        positions_value = sum(_as_float(p.get("currentValue")) for p in rows)

    cash = _as_float(
        value.get("cash") or value.get("balance") or value.get("usdc")
        if isinstance(value, dict) else 0.0
    )

    total = _as_float(value.get("value") or value.get("total") if isinstance(value, dict) else 0.0)
    if total == 0.0:
        total = cash + positions_value

    pnl = _as_float(value.get("pnl") or value.get("cashPnl") if isinstance(value, dict) else 0.0)
    if pnl == 0.0 and rows:
        pnl = sum(_as_float(p.get("cashPnl")) for p in rows)

    pnl_pct = _as_float(value.get("pnlPercent") or value.get("percentPnl") if isinstance(value, dict) else 0.0)

    text = common.tr(
        context, "bot.inquiry.portfolio",
        cash=_fmt_money(cash),
        positions_value=_fmt_money(positions_value),
        total=_fmt_money(total),
        pnl=f"{_pnl_emoji(pnl)} ${_fmt_money(pnl)}",
        pnl_pct=f"{pnl_pct:+.1f}",
    )
    await common.screen(update, context, text=text, parse_mode="Markdown",
                        reply_markup=_nav(context, "portfolio"))


# ── /positions ───────────────────────────────────────────────────────────────

async def positions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = common.db_user_id(context)
    await common.typing(update, context)
    try:
        pm = await common.manager(context).get_readonly_client(user_id)
        data = await asyncio.to_thread(pm.get_positions, _MAX_ROWS, 0)
    except NoAccountConnected:
        await _no_account(update, context)
        return
    except TradingUnavailable:
        await common.reply(update, context, "bot.error.trading_unavailable")
        return
    except Exception as exc:  # noqa: BLE001
        logger.warning("positions failed: %s", type(exc).__name__)
        await common.reply(update, context, "bot.error.generic")
        return

    rows = _as_rows(data)
    if not rows:
        await common.reply(update, context, "bot.inquiry.no_positions", reply_markup=_empty_kb(context))
        return

    value_label = common.esc(common.tr(context, "bot.inquiry.position_value"))
    lines = [_hhead(context, "bot.inquiry.positions_header"), ""]
    for p in rows[:_MAX_ROWS]:
        title = common.esc(p.get("title") or p.get("question") or "?")
        outcome = common.esc(p.get("outcome", ""))
        size = _fmt_money(p.get("size"))
        avg = _fmt_price(p.get("avgPrice"))
        cur_value = _fmt_money(p.get("currentValue"))
        cash_pnl = p.get("cashPnl", 0)
        pct_pnl = _as_float(p.get("percentPnl"))
        lines.append(
            f"• <b>{title}</b>\n"
            f"  {outcome} | {size} @ ${avg}\n"
            f"  {value_label}: ${cur_value} | "
            f"{_pnl_emoji(cash_pnl)} ${_fmt_money(cash_pnl)} ({pct_pnl:+.1f}%)"
        )
    await common.screen(update, context, text="\n".join(lines), reply_markup=_nav(context, "positions"))


# ── /balance ─────────────────────────────────────────────────────────────────

def _parse_atomic_usdc(raw) -> float:
    """Atomic USDC has 6 decimals; divide when the magnitude looks atomic."""
    n = _as_float(raw)
    return n / 1e6 if n > 1e6 else n


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = common.db_user_id(context)
    await common.typing(update, context)
    try:
        pm = await common.manager(context).get_trading_client(user_id)
        data = await asyncio.to_thread(pm.get_balance)
    except NoAccountConnected:
        await _no_account(update, context)
        return
    except TradingUnavailable:
        await common.reply(update, context, "bot.error.trading_unavailable")
        return
    except Exception as exc:  # noqa: BLE001
        logger.warning("balance failed: %s", type(exc).__name__)
        await common.reply(update, context, "bot.error.generic")
        return

    raw = data.get("balance", data) if isinstance(data, dict) else data
    text = common.tr(context, "bot.inquiry.balance", balance=_fmt_money(_parse_atomic_usdc(raw)))
    await common.screen(update, context, text=text, parse_mode="Markdown",
                        reply_markup=_nav(context, "balance"))


# ── /orders ──────────────────────────────────────────────────────────────────

async def orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = common.db_user_id(context)
    await common.typing(update, context)
    try:
        pm = await common.manager(context).get_trading_client(user_id)
        data = await asyncio.to_thread(pm.get_open_orders)
    except NoAccountConnected:
        await _no_account(update, context)
        return
    except TradingUnavailable:
        await common.reply(update, context, "bot.error.trading_unavailable")
        return
    except Exception as exc:  # noqa: BLE001
        logger.warning("orders failed: %s", type(exc).__name__)
        await common.reply(update, context, "bot.error.generic")
        return

    rows = data if isinstance(data, list) else _as_rows(data)
    if not rows:
        await common.reply(update, context, "bot.inquiry.no_orders", reply_markup=_empty_kb(context))
        return

    order_ids: list[str] = []
    lines = [_hhead(context, "bot.inquiry.orders_header"), ""]
    for o in rows[:_MAX_ROWS]:
        oid = str(o.get("id", "?"))
        side = str(o.get("side", "?")).upper()
        price = _fmt_price(o.get("price"))
        size = _fmt_money(o.get("original_size") or o.get("size"))
        token = o.get("asset_id") or o.get("market") or "?"
        emoji = "🟩" if side == "BUY" else "🟥"
        n = len(order_ids) + 1
        order_ids.append(oid)
        lines.append(
            f"{emoji} <b>#{n}</b> <code>{common.esc(_shorten(oid, 12))}</code>\n"
            f"  {common.esc(side)} | ${price} × {size}\n"
            f"  <code>{common.esc(_shorten(token))}</code>"
        )
    if len(rows) > _MAX_ROWS:
        note = common.tr(context, "bot.inquiry.showing", n=_MAX_ROWS, total=len(rows)).replace("_", "")
        lines += ["", f"<i>{common.esc(note)}</i>"]

    # Per-order ✖ Cancel buttons (id stashed by index) + 🗑 Cancel all, then the nav rows.
    common.stash(context, "open_orders", order_ids)
    cancel_btns = [InlineKeyboardButton(common.tr(context, "bot.trade.cancel_one", n=i + 1),
                                        callback_data=f"ocancel:{i}") for i in range(len(order_ids))]
    btn_rows = [cancel_btns[j:j + 3] for j in range(0, len(cancel_btns), 3)]
    btn_rows.append([InlineKeyboardButton(common.tr(context, "bot.trade.cancel_all_btn"), callback_data="ocancelall")])
    await common.screen(update, context, text="\n".join(lines),
                        reply_markup=InlineKeyboardMarkup(btn_rows + _nav_rows(context, "orders")))


# ── /trades ──────────────────────────────────────────────────────────────────

async def trades(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = common.db_user_id(context)
    await common.typing(update, context)
    try:
        pm = await common.manager(context).get_readonly_client(user_id)
        data = await asyncio.to_thread(pm.get_trades, _MAX_ROWS, 0)
    except NoAccountConnected:
        await _no_account(update, context)
        return
    except TradingUnavailable:
        await common.reply(update, context, "bot.error.trading_unavailable")
        return
    except Exception as exc:  # noqa: BLE001
        logger.warning("trades failed: %s", type(exc).__name__)
        await common.reply(update, context, "bot.error.generic")
        return

    rows = _as_rows(data)
    if not rows:
        await common.reply(update, context, "bot.inquiry.no_trades", reply_markup=_empty_kb(context))
        return

    lines = [_hhead(context, "bot.inquiry.trades_header"), ""]
    for tr_row in rows[:_MAX_ROWS]:
        title = common.esc(tr_row.get("title") or tr_row.get("question") or "?")
        side = str(tr_row.get("side", "?")).upper()
        outcome = common.esc(tr_row.get("outcome", ""))
        price = _as_float(tr_row.get("price"))
        size = _as_float(tr_row.get("size"))
        emoji = "🟢" if side == "BUY" else "🔴"
        when = _rel_ts(tr_row.get("timestamp") or tr_row.get("matchTime"))
        lines.append(
            f"{emoji} <b>{common.esc(side)}</b> {outcome} — {title}\n"
            f"  ${price:.4f} × {size:,.2f} = ${price * size:,.2f}\n"
            f"  {when}"
        )
    await common.screen(update, context, text="\n".join(lines), reply_markup=_nav(context, "trades"))


# ── /activity ────────────────────────────────────────────────────────────────

async def activity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = common.db_user_id(context)
    await common.typing(update, context)
    try:
        pm = await common.manager(context).get_readonly_client(user_id)
        data = await asyncio.to_thread(pm.get_activity, _MAX_ROWS)
    except NoAccountConnected:
        await _no_account(update, context)
        return
    except TradingUnavailable:
        await common.reply(update, context, "bot.error.trading_unavailable")
        return
    except Exception as exc:  # noqa: BLE001
        logger.warning("activity failed: %s", type(exc).__name__)
        await common.reply(update, context, "bot.error.generic")
        return

    rows = _as_rows(data)
    if not rows:
        await common.reply(update, context, "bot.inquiry.no_activity", reply_markup=_empty_kb(context))
        return

    lines = [_hhead(context, "bot.inquiry.activity_header"), ""]
    for a in rows[:_MAX_ROWS]:
        atype = a.get("type", "?")
        title = common.esc(a.get("title") or a.get("question") or "")
        amount = _fmt_money(a.get("usdcSize") or a.get("amount"))
        when = _rel_ts(a.get("timestamp") or a.get("createdAt"))
        lines.append(f"{_act_emoji(atype)} <b>{common.esc(str(atype))}</b> — {title}\n  ${amount} | {when}")
    await common.screen(update, context, text="\n".join(lines), reply_markup=_nav(context, "activity"))


# ── /search <query> ──────────────────────────────────────────────────────────

async def search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await common.reply(update, context, "bot.market.search_usage")
        return
    query = " ".join(context.args)
    user_id = common.db_user_id(context)
    await common.typing(update, context)
    try:
        pm = await common.manager(context).get_readonly_client(user_id)
        results = await asyncio.to_thread(pm.search_markets, query, 10)
    except NoAccountConnected:
        await _no_account(update, context)
        return
    except TradingUnavailable:
        await common.reply(update, context, "bot.error.trading_unavailable")
        return
    except Exception as exc:  # noqa: BLE001
        logger.warning("search failed: %s", type(exc).__name__)
        await common.reply(update, context, "bot.error.generic")
        return

    rows = results if isinstance(results, list) else _as_rows(results)
    if not rows:
        await common.reply(update, context, "bot.market.no_results", query=query)
        return

    lines = [common.tr(context, "bot.market.results_header", query=query), ""]
    for m in rows[:_MAX_ROWS]:
        title = m.get("question") or m.get("title") or "?"
        cid = m.get("conditionId") or m.get("condition_id") or "?"
        lines.append(f"• *{title}*\n  `{cid}`")
    await _send(update, "\n".join(lines))


# ── /market <condition_id> ───────────────────────────────────────────────────

async def market(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await common.reply(update, context, "bot.market.market_usage")
        return
    condition_id = context.args[0]
    user_id = common.db_user_id(context)
    await common.typing(update, context)
    try:
        pm = await common.manager(context).get_readonly_client(user_id)
        data = await asyncio.to_thread(pm.get_market, condition_id)
    except NoAccountConnected:
        await _no_account(update, context)
        return
    except TradingUnavailable:
        await common.reply(update, context, "bot.error.trading_unavailable")
        return
    except Exception as exc:  # noqa: BLE001
        logger.warning("market failed: %s", type(exc).__name__)
        await common.reply(update, context, "bot.error.generic")
        return

    if not data:
        await common.reply(update, context, "bot.market.no_results", query=condition_id)
        return

    question = data.get("question", "?")
    outcomes = data.get("outcomes", "?")
    prices = data.get("outcomePrices", "?")
    volume = _fmt_money(data.get("volumeNum") or data.get("volume"))
    liquidity = _fmt_money(data.get("liquidityNum") or data.get("liquidity"))
    end_date = data.get("endDate") or data.get("end_date_iso") or "?"
    tokens = data.get("clobTokenIds", "")

    await _send(
        update,
        common.tr(
            context, "bot.market.detail",
            question=question,
            outcomes=outcomes,
            prices=prices,
            volume=volume,
            liquidity=liquidity,
            end_date=end_date,
            tokens=tokens,
        ),
    )


# ── /price <token_id> ────────────────────────────────────────────────────────

async def price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await common.reply(update, context, "bot.market.price_usage")
        return
    token_id = context.args[0]
    user_id = common.db_user_id(context)
    await common.typing(update, context)
    try:
        pm = await common.manager(context).get_readonly_client(user_id)
        buy = await asyncio.to_thread(pm.get_price, token_id, "buy")
        sell = await asyncio.to_thread(pm.get_price, token_id, "sell")
        mid = await asyncio.to_thread(pm.get_midpoint, token_id)
        spread = await asyncio.to_thread(pm.get_spread, token_id)
    except NoAccountConnected:
        await _no_account(update, context)
        return
    except TradingUnavailable:
        await common.reply(update, context, "bot.error.trading_unavailable")
        return
    except Exception as exc:  # noqa: BLE001
        logger.warning("price failed: %s", type(exc).__name__)
        await common.reply(update, context, "bot.error.generic")
        return

    await _send(
        update,
        common.tr(
            context, "bot.market.price_detail",
            token=_shorten(token_id, 20),
            bid=_fmt_price(buy.get("price") if isinstance(buy, dict) else buy),
            ask=_fmt_price(sell.get("price") if isinstance(sell, dict) else sell),
            mid=_fmt_price(mid.get("mid") if isinstance(mid, dict) else mid),
            spread=_fmt_price(spread.get("spread") if isinstance(spread, dict) else spread),
        ),
    )


# ── /book <token_id> ─────────────────────────────────────────────────────────

async def book(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await common.reply(update, context, "bot.market.book_usage")
        return
    token_id = context.args[0]
    user_id = common.db_user_id(context)
    await common.typing(update, context)
    try:
        pm = await common.manager(context).get_readonly_client(user_id)
        data = await asyncio.to_thread(pm.get_orderbook, token_id)
    except NoAccountConnected:
        await _no_account(update, context)
        return
    except TradingUnavailable:
        await common.reply(update, context, "bot.error.trading_unavailable")
        return
    except Exception as exc:  # noqa: BLE001
        logger.warning("book failed: %s", type(exc).__name__)
        await common.reply(update, context, "bot.error.generic")
        return

    bids = (data.get("bids", []) if isinstance(data, dict) else [])[:_MAX_BOOK_LEVELS]
    asks = (data.get("asks", []) if isinstance(data, dict) else [])[:_MAX_BOOK_LEVELS]
    if not bids and not asks:
        await common.reply(update, context, "bot.market.no_book", token=_shorten(token_id, 20))
        return

    lines = [common.tr(context, "bot.market.book_header", token=_shorten(token_id, 20)), ""]
    lines.append(common.tr(context, "bot.market.book_asks"))
    for a in reversed(asks):
        lines.append(f"  ${_fmt_price(a.get('price'))} — {_fmt_money(a.get('size'))}")
    lines.append("———————")
    lines.append(common.tr(context, "bot.market.book_bids"))
    for b in bids:
        lines.append(f"  ${_fmt_price(b.get('price'))} — {_fmt_money(b.get('size'))}")
    await _send(update, "\n".join(lines))


# ── refresh / cross-link callbacks (inq:<command>) ───────────────────────────

async def on_inq(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Re-run a monitoring command from a [↻ Refresh] / cross-link button."""
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    action = (query.data or "").split(":", 1)[1] if ":" in (query.data or "") else ""
    fn = {"portfolio": portfolio, "positions": positions, "balance": balance,
          "orders": orders, "trades": trades, "activity": activity}.get(action)
    if fn is not None:
        await fn(update, context)


async def on_order_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel one order (✖ #n) or all (🗑) from the /orders screen, via confirm.py."""
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    data = query.data or ""
    if data == "ocancelall":
        await confirm.request(update, context, confirm.make_intent("cancel_all"), "bot.confirm.cancel_all")
        return
    idx = data.split(":", 1)[1] if ":" in data else ""
    oid = common.from_stash(context, "open_orders", idx)
    if not oid:
        await common.reply(update, context, "bot.confirm.expired")
        return
    intent = confirm.make_intent("cancel", order_id=str(oid))
    await confirm.request(update, context, intent, "bot.confirm.cancel", order_id=common.short(str(oid), 12))


# ── registration ─────────────────────────────────────────────────────────────

def register(application: Application) -> None:
    """Add all read-only inquiry + market-data command handlers."""
    application.add_handler(CallbackQueryHandler(
        on_inq, pattern="^inq:(portfolio|positions|balance|orders|trades|activity)$"))
    application.add_handler(CallbackQueryHandler(on_order_cancel, pattern="^ocancel:"))
    application.add_handler(CallbackQueryHandler(on_order_cancel, pattern="^ocancelall$"))
    application.add_handler(CommandHandler("portfolio", portfolio))
    application.add_handler(CommandHandler("positions", positions))
    application.add_handler(CommandHandler("balance", balance))
    application.add_handler(CommandHandler("orders", orders))
    application.add_handler(CommandHandler("trades", trades))
    application.add_handler(CommandHandler("activity", activity))
    application.add_handler(CommandHandler("search", search))
    application.add_handler(CommandHandler("market", market))
    application.add_handler(CommandHandler("price", price))
    application.add_handler(CommandHandler("book", book))
