"""Sync data layer for the admin dashboard.

The dashboard process runs WITHOUT ``ENCRYPTION_KEY`` and these queries NEVER
select ``encrypted_private_key`` / ``encrypted_api_creds`` — account dicts carry
only public fields (wallet address, mode, status…). Live positions, when shown,
are fetched from Polymarket's PUBLIC Data API by wallet address (no key).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import Account, AuditLog, Order, Trade, User, UserStatus


def _today_start() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _account_public(acc: Account) -> dict:
    """Account dict with ONLY non-secret fields."""
    return {
        "id": acc.id,
        "label": acc.label,
        "wallet_address": acc.wallet_address,
        "signature_type": acc.signature_type,
        "funder_address": acc.funder_address,
        "mode": acc.mode,
        "status": acc.status,
        "last_synced_at": acc.last_synced_at,
        "created_at": acc.created_at,
    }


# ── metrics ───────────────────────────────────────────────────────────────────

def metrics_summary(db: Session) -> dict:
    today = _today_start()
    total_users = db.scalar(select(func.count()).select_from(User)) or 0
    active_users = db.scalar(
        select(func.count()).select_from(User).where(User.status == UserStatus.ACTIVE.value)
    ) or 0
    suspended = db.scalar(
        select(func.count()).select_from(User).where(User.status == UserStatus.SUSPENDED.value)
    ) or 0
    banned = db.scalar(
        select(func.count()).select_from(User).where(User.status == UserStatus.BANNED.value)
    ) or 0
    total_accounts = db.scalar(select(func.count()).select_from(Account)) or 0
    trades_today = db.scalar(
        select(func.count()).select_from(Trade).where(Trade.executed_at >= today)
    ) or 0
    new_users_today = db.scalar(
        select(func.count()).select_from(User).where(User.created_at >= today)
    ) or 0
    return {
        "total_users": total_users,
        "active_users": active_users,
        "suspended_users": suspended,
        "banned_users": banned,
        "total_accounts": total_accounts,
        "trades_today": trades_today,
        "new_users_today": new_users_today,
    }


# ── users ─────────────────────────────────────────────────────────────────────

def count_users(db: Session, *, status: str | None = None, q: str | None = None) -> int:
    stmt = select(func.count()).select_from(User)
    stmt = _apply_user_filters(stmt, status, q)
    return db.scalar(stmt) or 0


def list_users(db: Session, *, status: str | None = None, q: str | None = None,
               limit: int = 50, offset: int = 0) -> list[dict]:
    acct_count = (
        select(Account.user_id, func.count().label("n")).group_by(Account.user_id).subquery()
    )
    stmt = (
        select(User, func.coalesce(acct_count.c.n, 0))
        .outerjoin(acct_count, acct_count.c.user_id == User.id)
        .order_by(User.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    stmt = _apply_user_filters(stmt, status, q)
    rows = db.execute(stmt).all()
    return [
        {
            "id": u.id,
            "telegram_id": u.telegram_id,
            "username": u.username,
            "language": u.language,
            "status": u.status,
            "is_admin": u.is_admin,
            "account_count": int(n or 0),
            "last_seen_at": u.last_seen_at,
            "created_at": u.created_at,
        }
        for (u, n) in rows
    ]


def _apply_user_filters(stmt, status: str | None, q: str | None):
    if status:
        stmt = stmt.where(User.status == status)
    if q:
        like = f"%{q}%"
        if q.lstrip("-").isdigit():
            stmt = stmt.where(User.telegram_id == int(q))
        else:
            stmt = stmt.where(User.username.ilike(like))
    return stmt


def get_user(db: Session, user_id: int) -> User | None:
    return db.get(User, user_id)


def user_detail(db: Session, user_id: int) -> dict | None:
    user = db.get(User, user_id)
    if user is None:
        return None
    accounts = list(db.scalars(select(Account).where(Account.user_id == user_id)))
    account_ids = [a.id for a in accounts]
    orders = trades = []
    if account_ids:
        orders = list(
            db.scalars(
                select(Order).where(Order.account_id.in_(account_ids))
                .order_by(Order.created_at.desc()).limit(25)
            )
        )
        trades = list(
            db.scalars(
                select(Trade).where(Trade.account_id.in_(account_ids))
                .order_by(Trade.executed_at.desc()).limit(25)
            )
        )
    return {
        "user": {
            "id": user.id,
            "telegram_id": user.telegram_id,
            "username": user.username,
            "first_name": user.first_name,
            "language": user.language,
            "status": user.status,
            "is_admin": user.is_admin,
            "created_at": user.created_at,
            "last_seen_at": user.last_seen_at,
        },
        "accounts": [_account_public(a) for a in accounts],
        "orders": [_order_dict(o) for o in orders],
        "trades": [_trade_dict(t) for t in trades],
    }


def set_user_status(db: Session, user_id: int, status: str) -> bool:
    if status not in (UserStatus.ACTIVE.value, UserStatus.SUSPENDED.value, UserStatus.BANNED.value):
        return False
    user = db.get(User, user_id)
    if user is None:
        return False
    user.status = status
    return True


def _order_dict(o: Order) -> dict:
    return {
        "id": o.id, "clob_order_id": o.clob_order_id, "token_id": o.token_id,
        "side": o.side, "order_type": o.order_type, "price": _f(o.price),
        "size": _f(o.size), "status": o.status, "created_at": o.created_at,
    }


def _trade_dict(t: Trade) -> dict:
    return {
        "id": t.id, "token_id": t.token_id, "side": t.side, "price": _f(t.price),
        "size": _f(t.size), "cost": _f(t.cost), "pnl": _f(t.pnl),
        "is_demo": t.is_demo, "executed_at": t.executed_at,
    }


def _f(v):
    return float(v) if v is not None else None


# ── audit ─────────────────────────────────────────────────────────────────────

def list_audit(db: Session, *, event: str | None = None, user_id: int | None = None,
               limit: int = 100, offset: int = 0) -> list[dict]:
    stmt = select(AuditLog).order_by(AuditLog.ts.desc()).limit(limit).offset(offset)
    if event:
        stmt = stmt.where(AuditLog.event == event)
    if user_id:
        stmt = stmt.where(AuditLog.user_id == user_id)
    return [
        {
            "id": a.id, "ts": a.ts, "actor_type": a.actor_type, "actor_id": a.actor_id,
            "user_id": a.user_id, "account_id": a.account_id, "event": a.event,
            "detail": a.detail, "ip": a.ip,
        }
        for a in db.scalars(stmt)
    ]


# ── wallet addresses for live position lookups (public, no secrets) ───────────

def wallet_addresses_for_user(db: Session, user_id: int) -> list[str]:
    return list(db.scalars(select(Account.wallet_address).where(Account.user_id == user_id)))
