"""Shared order-confirmation + execution for trading.py and positions_ui.py.

Every order goes through here. A pending *intent* (no secrets — just token id,
side, size/price/amount) is stashed in ``user_data`` under a short random id and
shown with inline ✅/❌ buttons. On confirm we resolve the user's trading client,
place the order in a worker thread, log it to the DB, and write audit rows.

If the user's ``confirm_trades`` preference is off, non-destructive orders skip
the button step and execute immediately. ``cancel_all`` ALWAYS confirms.

Owns callbacks ``^ord_ok:`` and ``^ord_no:``.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

from bot.handlers import common
from core import audit
from core.audit import AuditEvent
from db.engine import async_session_scope
from db.repositories import orders as orders_repo
from db.repositories import users as users_repo
from polymarket.credentials import NoAccountConnected, TradingUnavailable

logger = logging.getLogger(__name__)

_TTL_SECONDS = 120


# ── intent helpers ────────────────────────────────────────────────────────────

def make_intent(kind: str, **fields) -> dict:
    """Build a confirmation intent. kind ∈ limit|market|close|cancel|cancel_all."""
    intent = {"kind": kind, **fields}
    intent["ts"] = time.time()
    return intent


def _confirm_keyboard(context: ContextTypes.DEFAULT_TYPE, intent_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(common.tr(context, "bot.confirm.yes"), callback_data=f"ord_ok:{intent_id}"),
            InlineKeyboardButton(common.tr(context, "bot.confirm.no"), callback_data=f"ord_no:{intent_id}"),
        ]]
    )


async def _wants_confirmation(user_id: int) -> bool:
    try:
        async with async_session_scope() as session:
            settings_row = await users_repo.get_settings(session, user_id)
            return bool(settings_row.confirm_trades) if settings_row else True
    except Exception:  # noqa: BLE001 — default to safe (confirm) on any error
        return True


async def request(update: Update, context: ContextTypes.DEFAULT_TYPE, intent: dict,
                  confirm_key: str, **text_vars) -> None:
    """Ask for confirmation (or execute immediately if the user opted out)."""
    user_id = common.db_user_id(context)
    if user_id is None:
        await common.reply(update, context, "bot.error.no_account")
        return

    intent["ts"] = time.time()
    always_confirm = intent.get("kind") == "cancel_all"
    if not always_confirm and not await _wants_confirmation(user_id):
        await _execute(update, context, intent)
        return

    intent_id = secrets.token_hex(4)
    context.user_data.setdefault("pending_orders", {})[intent_id] = intent
    await update.effective_message.reply_text(
        common.tr(context, confirm_key, **text_vars),
        parse_mode="Markdown",
        reply_markup=_confirm_keyboard(context, intent_id),
    )


# ── callbacks ───────────────────────────────────────────────────────────────

async def on_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    intent_id = (query.data or "").split(":", 1)[1] if ":" in (query.data or "") else ""
    intent = (context.user_data.get("pending_orders") or {}).pop(intent_id, None)
    if not intent or (time.time() - intent.get("ts", 0)) > _TTL_SECONDS:
        await query.edit_message_text(common.tr(context, "bot.confirm.expired"))
        return
    await _execute(update, context, intent, query=query)


async def on_decline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    intent_id = (query.data or "").split(":", 1)[1] if ":" in (query.data or "") else ""
    (context.user_data.get("pending_orders") or {}).pop(intent_id, None)
    await query.edit_message_text(common.tr(context, "bot.confirm.aborted"))


# ── execution ─────────────────────────────────────────────────────────────────

def _result_ok(result) -> bool:
    if not isinstance(result, dict):
        return True
    if result.get("success") is False:
        return False
    if result.get("error") or result.get("errorMsg"):
        return False
    return True


def _result_order_id(result) -> str | None:
    if isinstance(result, dict):
        return result.get("orderID") or result.get("orderId") or result.get("id")
    return None


async def _execute(update: Update, context: ContextTypes.DEFAULT_TYPE, intent: dict, query=None) -> None:
    user_id = common.db_user_id(context)

    async def respond(key: str, **kw) -> None:
        text = common.tr(context, key, **kw)
        if query is not None:
            await query.edit_message_text(text, parse_mode="Markdown")
        elif update.effective_message is not None:
            await update.effective_message.reply_text(text, parse_mode="Markdown")

    try:
        pm = await common.manager(context).get_trading_client(user_id)
    except NoAccountConnected:
        await respond("bot.error.no_account")
        return
    except TradingUnavailable:
        await respond("bot.error.trading_unavailable")
        return
    except Exception as exc:  # noqa: BLE001
        logger.warning("execute: client build failed: %s", type(exc).__name__)
        await respond("bot.error.generic")
        return

    account_id = await common.manager(context).default_account_id(user_id)
    kind = intent.get("kind")
    side = intent.get("side", "")
    token = intent.get("token_id", "")

    await respond("bot.trade.placing")
    submit_event = (
        AuditEvent.CANCEL_SUBMIT if kind in ("cancel", "cancel_all") else AuditEvent.ORDER_SUBMIT
    )
    await _audit(submit_event, user_id, account_id, _safe_detail(intent))

    try:
        if kind == "limit":
            result = await asyncio.to_thread(pm.place_limit_order, token, intent["price"], intent["size"], side)
        elif kind == "market":
            result = await asyncio.to_thread(pm.place_market_order, token, intent["amount"], side)
        elif kind == "close":  # market SELL of the full share size
            result = await asyncio.to_thread(pm.place_market_order, token, intent["size"], "sell")
        elif kind == "cancel":
            result = await asyncio.to_thread(pm.cancel_order, intent["order_id"])
        elif kind == "cancel_all":
            result = await asyncio.to_thread(pm.cancel_all_orders)
        else:
            await respond("bot.error.generic")
            return
    except Exception as exc:  # noqa: BLE001 — CLOB errors carry no key material
        logger.warning("execute %s failed: %s", kind, type(exc).__name__)
        await _audit(AuditEvent.ORDER_ERROR, user_id, account_id, {"kind": kind, "error": type(exc).__name__})
        if kind in ("limit", "market", "close") and account_id is not None:
            await _log_order(account_id, intent, status="rejected", error=type(exc).__name__)
        await respond("bot.order.failed")
        return

    # ── interpret result ──
    if kind in ("cancel", "cancel_all"):
        cancel_ok = _result_ok(result)
        await _audit(AuditEvent.CANCEL_RESULT, user_id, account_id, {"kind": kind, "ok": cancel_ok})
        if not cancel_ok:
            await respond("bot.order.failed")
        elif kind == "cancel":
            await respond("bot.order.cancelled", order_id=intent.get("order_id", ""))
        else:
            count = len(result.get("canceled", [])) if isinstance(result, dict) else 0
            await respond("bot.order.cancelled_all", count=count)
        return

    ok = _result_ok(result)
    order_id = _result_order_id(result)
    await _audit(AuditEvent.ORDER_RESULT, user_id, account_id,
                 {"kind": kind, "ok": ok, "order_id": order_id})
    if account_id is not None:
        await _log_order(account_id, intent, status=("open" if ok else "rejected"),
                         clob_order_id=order_id, error=None if ok else "rejected")

    if ok and user_id is not None:
        await _record_activity(user_id, intent)

    if not ok:
        await respond("bot.order.failed")
    elif kind == "close":
        await respond("bot.order.closed", token=_short(token))
    else:
        status = result.get("status", "") if isinstance(result, dict) else ""
        await respond("bot.order.placed", order_id=order_id or "—", status=status or "submitted")


# ── small helpers ───────────────────────────────────────────────────────────

def _short(token: str) -> str:
    return f"{token[:8]}…" if token and len(token) > 10 else token


def _safe_detail(intent: dict) -> dict:
    """Audit detail with no secrets (intents never carry secrets anyway)."""
    return {k: v for k, v in intent.items() if k != "ts"}


async def _audit(event: AuditEvent, user_id: int | None, account_id: int | None, detail: dict) -> None:
    try:
        async with async_session_scope() as session:
            await audit.record_async(session, event, actor_type="user",
                                     user_id=user_id, account_id=account_id, detail=detail)
    except Exception as exc:  # noqa: BLE001 — auditing must never block a trade
        logger.warning("audit %s failed: %s", event, type(exc).__name__)


def _notional_usd(intent: dict) -> float:
    """Best-effort USD notional for the volume leaderboard."""
    kind = intent.get("kind")
    if kind == "market":
        return float(intent.get("amount") or 0)
    if kind == "limit":
        return float(intent.get("price") or 0) * float(intent.get("size") or 0)
    return 0.0  # close/sell by shares — counts as a bet, no USD notional


async def _record_activity(user_id: int, intent: dict) -> None:
    """Count a successful order toward the user's streak + totals."""
    try:
        from db.repositories import stats as stats_repo
        async with async_session_scope() as session:
            await stats_repo.record_bet(session, user_id, _notional_usd(intent))
    except Exception as exc:  # noqa: BLE001 — gamification must never block a trade
        logger.warning("record_activity failed: %s", type(exc).__name__)


async def _log_order(account_id: int, intent: dict, *, status: str,
                     clob_order_id: str | None = None, error: str | None = None) -> None:
    try:
        async with async_session_scope() as session:
            await orders_repo.log_order(
                session,
                account_id=account_id,
                token_id=intent.get("token_id", ""),
                side=("sell" if intent.get("kind") == "close" else intent.get("side", "")),
                order_type=("MARKET" if intent.get("kind") in ("market", "close") else "LIMIT"),
                size=float(intent.get("size") or intent.get("amount") or 0),
                price=intent.get("price"),
                status=status,
                clob_order_id=clob_order_id,
                error=error,
            )
    except Exception as exc:  # noqa: BLE001 — logging must never block a trade
        logger.warning("order logging failed: %s", type(exc).__name__)


def register(application: Application) -> None:
    application.add_handler(CallbackQueryHandler(on_confirm, pattern="^ord_ok:"))
    application.add_handler(CallbackQueryHandler(on_decline, pattern="^ord_no:"))
