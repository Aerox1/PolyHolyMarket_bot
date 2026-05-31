"""Order & trade logging (async).

Records every order placed through the bot and its resulting fill, for the
user's history and the admin dashboard. Best-effort: a logging failure must
never block or reverse a real trade, so callers wrap these in try/except.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Order, Trade

logger = logging.getLogger(__name__)


async def log_order(
    session: AsyncSession,
    *,
    account_id: int,
    token_id: str,
    side: str,
    order_type: str,
    size: float,
    price: float | None,
    status: str,
    clob_order_id: str | None = None,
    title: str | None = None,
    error: str | None = None,
) -> Order:
    order = Order(
        account_id=account_id,
        clob_order_id=clob_order_id,
        token_id=token_id,
        title=title,
        side=side.upper(),
        order_type=order_type.upper(),
        price=price,
        size=size,
        status=status,
        error=error,
    )
    session.add(order)
    await session.flush()
    return order


async def log_trade(
    session: AsyncSession,
    *,
    account_id: int,
    token_id: str,
    side: str,
    price: float,
    size: float,
    cost: float,
    fee: float = 0.0,
    pnl: float | None = None,
    order_id: int | None = None,
    title: str | None = None,
    outcome: str | None = None,
    fill_method: str | None = None,
    is_demo: bool = False,
) -> Trade:
    trade = Trade(
        account_id=account_id,
        order_id=order_id,
        token_id=token_id,
        title=title,
        outcome=outcome,
        side=side.upper(),
        price=price,
        size=size,
        cost=cost,
        fee=fee,
        pnl=pnl,
        fill_method=fill_method,
        is_demo=is_demo,
    )
    session.add(trade)
    await session.flush()
    return trade


async def recent_orders(session: AsyncSession, account_id: int, limit: int = 20) -> list[Order]:
    return list(
        await session.scalars(
            select(Order).where(Order.account_id == account_id).order_by(Order.created_at.desc()).limit(limit)
        )
    )


async def recent_trades(session: AsyncSession, account_id: int, limit: int = 20) -> list[Trade]:
    return list(
        await session.scalars(
            select(Trade).where(Trade.account_id == account_id).order_by(Trade.executed_at.desc()).limit(limit)
        )
    )
