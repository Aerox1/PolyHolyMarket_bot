"""Bet repository: record settleable bets and settle them when markets resolve.

Payout model (fixed-odds at entry): a winning bet's shares = amount / entry_price
each redeem for $1, so payout = amount/entry_price and pnl = payout - amount. A
loss pays 0 (pnl = -amount). A void refunds the stake (pnl = 0).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Bet, BetStatus


async def create_bet(
    session: AsyncSession,
    *,
    user_id: int,
    account_id: int | None,
    market_id: str,
    token_id: str,
    question: str | None,
    outcome: str,
    amount_usd: float,
    entry_price: float | None,
    source: str = "miniapp",
    clob_order_id: str | None = None,
) -> Bet:
    shares = (amount_usd / entry_price) if (entry_price and entry_price > 0) else None
    bet = Bet(
        user_id=user_id, account_id=account_id, market_id=market_id, token_id=token_id,
        question=question, outcome=outcome.upper(), amount_usd=amount_usd,
        entry_price=entry_price, shares=shares, source=source, clob_order_id=clob_order_id,
        status=BetStatus.OPEN.value,
    )
    session.add(bet)
    await session.flush()
    return bet


async def open_bets(session: AsyncSession, limit: int = 500) -> list[Bet]:
    return list(
        await session.scalars(
            select(Bet).where(Bet.status == BetStatus.OPEN.value).order_by(Bet.created_at).limit(limit)
        )
    )


async def open_market_ids(session: AsyncSession) -> list[str]:
    rows = await session.scalars(
        select(Bet.market_id).where(Bet.status == BetStatus.OPEN.value).distinct()
    )
    return list(rows)


def settle_bet_values(bet: Bet, *, winning_token: str | None, void: bool) -> dict:
    """Pure computation of a bet's settlement outcome (no DB writes)."""
    amount = float(bet.amount_usd or 0)
    p = float(bet.entry_price) if bet.entry_price is not None else None
    if void or winning_token is None:
        return {"status": BetStatus.VOID.value, "won": None, "payout": amount, "pnl": 0.0, "brier": None}
    won = str(bet.token_id) == str(winning_token)
    if won:
        payout = (amount / p) if (p and p > 0) else amount
        pnl = payout - amount
        brier = (p - 1.0) ** 2 if p is not None else None
        return {"status": BetStatus.WON.value, "won": True, "payout": payout, "pnl": pnl, "brier": brier}
    brier = (p - 0.0) ** 2 if p is not None else None
    return {"status": BetStatus.LOST.value, "won": False, "payout": 0.0, "pnl": -amount, "brier": brier}


def apply_settlement(bet: Bet, values: dict) -> None:
    bet.status = values["status"]
    bet.payout_usd = values["payout"]
    bet.pnl_usd = values["pnl"]
    bet.brier = values["brier"]
    bet.settled_at = datetime.now(timezone.utc)
