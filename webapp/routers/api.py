"""Mini App JSON API: categories, markets, account status, and real bets.

Auth: every endpoint requires a valid Telegram ``initData`` (via current_user).
Betting places a REAL market order through the user's connected wallet — the
server re-fetches the market and resolves the outcome token itself (never trusts
a client-supplied token), enforces an amount cap, then signs via AccountManager.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from core import audit
from core.audit import AuditEvent
from db.models import User
from db.repositories import accounts as accounts_repo
from db.repositories import bets as bets_repo
from db.repositories import categories as categories_repo
from db.repositories import orders as orders_repo
from db.repositories import stats as stats_repo
from polymarket import markets
from polymarket.credentials import NoAccountConnected, TradingUnavailable
from webapp.deps import current_user, get_db, manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

_MAX_BET_USD = 1000.0
_MIN_BET_USD = 0.5


# ── account ───────────────────────────────────────────────────────────────────

@router.get("/me")
async def me(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)) -> dict:
    acc = await accounts_repo.resolve_account(db, user.id)
    return {
        "telegram_id": user.telegram_id,
        "language": user.language,
        "connected": acc is not None,
        "wallet": acc.wallet_address if acc else None,
        "stats": await stats_repo.get_stats(db, user.id),
    }


def _parse_usdc(raw) -> float:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return v / 1e6 if v > 1_000_000 else v


@router.get("/portfolio")
async def portfolio(request: Request, user: User = Depends(current_user)) -> dict:
    mgr = manager(request)
    try:
        ro = await mgr.get_readonly_client(user.id)
    except NoAccountConnected:
        raise HTTPException(status_code=409, detail="no_account")
    raw = await asyncio.to_thread(ro.get_positions)
    rows = raw if isinstance(raw, list) else (raw.get("data") or raw.get("positions") or [])
    positions = []
    for p in rows[:30] if isinstance(rows, list) else []:
        if not isinstance(p, dict):
            continue
        positions.append({
            "title": p.get("title") or p.get("market"),
            "outcome": p.get("outcome"),
            "size": _num(p.get("size")),
            "value": _num(p.get("currentValue") if p.get("currentValue") is not None else p.get("curValue")),
            "pnl": _num(p.get("cashPnl") if p.get("cashPnl") is not None else p.get("pnl")),
        })
    # Balance needs L2 (trading client) — best-effort.
    balance = None
    try:
        pm = await mgr.get_trading_client(user.id)
        bal = await asyncio.to_thread(pm.get_balance)
        balance = _parse_usdc(bal.get("balance")) if isinstance(bal, dict) else None
    except Exception as exc:  # noqa: BLE001 — balance is best-effort (incl. TradingUnavailable)
        logger.info("portfolio balance unavailable: %s", type(exc).__name__)
    return {"balance": balance, "positions": positions}


@router.get("/leaderboard")
async def leaderboard(metric: str = "bets", user: User = Depends(current_user),
                      db: AsyncSession = Depends(get_db)) -> dict:
    metric = metric if metric in stats_repo.METRICS else "bets"
    rows = await stats_repo.leaderboard(db, metric=metric, limit=20)
    return {"metric": metric, "rows": rows, "me": await stats_repo.get_stats(db, user.id)}


def _num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# ── categories & markets ──────────────────────────────────────────────────────

@router.get("/categories")
async def list_categories(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)) -> list[dict]:
    cats = await categories_repo.list_visible(db)
    return [
        {
            "id": c.id,
            "title": c.title,
            "slug": c.slug,
            "volume": float(c.volume or 0),
            "image_url": c.image_path,            # null → frontend uses a gradient placeholder
            "image_status": c.image_status,
        }
        for c in cats
    ]


@router.get("/categories/{category_id}/markets")
async def category_markets(category_id: int, user: User = Depends(current_user),
                           db: AsyncSession = Depends(get_db)) -> dict:
    cat = await categories_repo.get(db, category_id)
    if cat is None or cat.hidden:
        raise HTTPException(status_code=404, detail="category not found")
    mkts = await asyncio.to_thread(markets.category_markets, cat.tag_slug or cat.slug, 40)
    return {"category": {"id": cat.id, "title": cat.title}, "markets": mkts}


@router.get("/markets/{market_id}")
async def market_detail(market_id: str, user: User = Depends(current_user)) -> dict:
    m = await asyncio.to_thread(markets.get_market, market_id)
    if m is None:
        raise HTTPException(status_code=404, detail="market not found")
    return m


# ── betting (real order) ──────────────────────────────────────────────────────

@router.post("/bet")
async def place_bet(
    request: Request,
    payload: dict = Body(...),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    market_id = str(payload.get("market_id") or "")
    outcome = str(payload.get("outcome") or "").lower()
    try:
        amount = float(payload.get("amount_usd"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="invalid amount")
    if outcome not in ("yes", "no"):
        raise HTTPException(status_code=400, detail="outcome must be yes or no")
    if not (_MIN_BET_USD <= amount <= _MAX_BET_USD):
        raise HTTPException(status_code=400, detail=f"amount must be between ${_MIN_BET_USD} and ${_MAX_BET_USD}")

    # Re-fetch the market server-side; never trust a client-supplied token.
    m = await asyncio.to_thread(markets.get_market, market_id)
    if m is None:
        raise HTTPException(status_code=404, detail="market not found")
    token = m["yes_token"] if outcome == "yes" else m["no_token"]

    try:
        pm = await manager(request).get_trading_client(user.id)
    except NoAccountConnected:
        raise HTTPException(status_code=409, detail="no_account")
    except TradingUnavailable:
        raise HTTPException(status_code=409, detail="trading_unavailable")

    account_id = await manager(request).default_account_id(user.id)
    await audit.record_async(db, AuditEvent.ORDER_SUBMIT, actor_type="user", user_id=user.id,
                             account_id=account_id,
                             detail={"src": "miniapp", "market": market_id, "outcome": outcome, "amount": amount})
    try:
        result = await asyncio.to_thread(pm.place_market_order, token, amount, "buy")
    except Exception as exc:  # noqa: BLE001 — CLOB errors carry no key material
        logger.warning("miniapp bet failed: %s", type(exc).__name__)
        await audit.record_async(db, AuditEvent.ORDER_ERROR, actor_type="user", user_id=user.id,
                                 account_id=account_id, detail={"error": type(exc).__name__})
        raise HTTPException(status_code=502, detail="order_failed")

    ok = not (isinstance(result, dict) and (result.get("success") is False or result.get("error") or result.get("errorMsg")))
    order_id = result.get("orderID") or result.get("orderId") or result.get("id") if isinstance(result, dict) else None
    await audit.record_async(db, AuditEvent.ORDER_RESULT, actor_type="user", user_id=user.id,
                             account_id=account_id, detail={"ok": ok, "order_id": order_id})
    if account_id is not None:
        await orders_repo.log_order(db, account_id=account_id, token_id=token, side="BUY",
                                    order_type="MARKET", size=amount, price=None,
                                    status=("open" if ok else "rejected"), clob_order_id=order_id,
                                    title=m.get("question"), error=None if ok else "rejected")
    if not ok:
        raise HTTPException(status_code=502, detail="order_rejected")
    # Gamification: count the bet toward streak + totals, and record a settleable bet.
    try:
        await stats_repo.record_bet(db, user.id, amount)
        await bets_repo.create_bet(
            db, user_id=user.id, account_id=account_id, market_id=market_id, token_id=token,
            question=m.get("question"), outcome=outcome,
            amount_usd=amount, entry_price=(m.get("yes_price") if outcome == "yes" else m.get("no_price")),
            source="miniapp", clob_order_id=order_id,
        )
    except Exception as exc:  # noqa: BLE001 — stats/bet recording must never block a trade result
        logger.warning("record_bet/create_bet failed: %s", type(exc).__name__)
    return {"ok": True, "order_id": order_id, "outcome": outcome, "amount": amount,
            "question": m.get("question")}
