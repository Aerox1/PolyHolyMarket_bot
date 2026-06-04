"""Sync data layer for the admin dashboard.

The dashboard process runs WITHOUT ``ENCRYPTION_KEY`` and these queries NEVER
select ``encrypted_private_key`` / ``encrypted_api_creds`` — account dicts carry
only public fields (wallet address, mode, status…). Live positions, when shown,
are fetched from Polymarket's PUBLIC Data API by wallet address (no key).
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from urllib.parse import urlparse

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, aliased

from core.config import settings
from db.models import (
    Account, AuditLog, Bet, Category, NewsItem, NewsSource, Order, PendingIntent,
    PointsLedger, Referral, Trade, User, UserStats, UserStatus,
)
from db.repositories import appconfig, gemini_usage
from db.repositories.rewards import REFERRAL_UNLOCK_BETS

# app_config keys for the news pipeline (admin-editable, non-secret)
NEWS_CHANNEL_ID = "news_channel_id"
NEWS_TOP_N = "news_top_n"
NEWS_AUTOSEND = "news_autosend"
NEWS_POLL = "news_poll"  # post an engagement poll under each channel card (default on)


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


# ── Mini App: categories + Gemini budget ──────────────────────────────────────

def list_categories(db: Session) -> list[Category]:
    """All categories (incl. hidden) for the admin, in display order."""
    return list(
        db.scalars(
            select(Category).order_by(
                Category.pinned.desc(), Category.display_order.asc(), Category.volume.desc()
            )
        )
    )


def curate_category(db: Session, category_id: int, action: str) -> bool:
    cat = db.get(Category, category_id)
    if cat is None:
        return False
    if action == "pin":
        cat.pinned = True
    elif action == "unpin":
        cat.pinned = False
    elif action == "hide":
        cat.hidden = True
    elif action == "unhide":
        cat.hidden = False
    elif action == "regen":
        # The key-less dashboard can't call Gemini; reset status so the webapp
        # regenerates this image on its next refresh cycle.
        cat.image_status = "none"
        cat.image_path = None
    else:
        return False
    return True


def get_category(db: Session, category_id: int) -> Category | None:
    return db.get(Category, category_id)


def update_category(db: Session, category_id: int, *, title: str | None = None,
                    prompt_override: str | None = None, regenerate: bool = False) -> bool:
    cat = db.get(Category, category_id)
    if cat is None:
        return False
    if title is not None and title.strip():
        cat.title = title.strip()
    if prompt_override is not None:
        cat.prompt_override = prompt_override.strip() or None
    if regenerate:
        cat.image_status = "none"
        cat.image_path = None
    return True


def save_category_image(db: Session, category_id: int, data: bytes) -> bool:
    """Save an admin-uploaded image as the category's card (no Gemini needed)."""
    from core.gemini import cards_dir

    cat = db.get(Category, category_id)
    if cat is None:
        return False
    (cards_dir() / f"{cat.slug}.png").write_bytes(data)
    cat.image_path = f"/cards/{cat.slug}.png"
    cat.image_status = "ready"
    return True


# ── Welcome banner (the /start hero image) ──────────────────────────────────────

def welcome_banner(db: Session) -> dict:
    """Current welcome-banner state for the admin Mini App page."""
    from core.gemini import DEFAULT_WELCOME_PROMPT, WELCOME_PATH_KEY, WELCOME_PROMPT_KEY, welcome_image_file

    f = welcome_image_file()
    return {
        "path": appconfig.get_sync(db, WELCOME_PATH_KEY) or (f"/cards/{f.name}" if f else None),
        "exists": f is not None,
        "prompt": appconfig.get_sync(db, WELCOME_PROMPT_KEY) or "",
        "default_prompt": DEFAULT_WELCOME_PROMPT,
    }


def set_welcome_prompt(db: Session, prompt: str, *, regenerate: bool = False) -> None:
    """Save the welcome-banner prompt. If regenerate, drop the cached image so the
    webapp (which holds the Gemini key) re-creates it on its next startup."""
    from core.gemini import WELCOME_PATH_KEY, WELCOME_PROMPT_KEY, welcome_image_file

    appconfig.set_sync(db, WELCOME_PROMPT_KEY, prompt.strip())
    if regenerate:
        f = welcome_image_file()
        if f is not None:
            f.unlink(missing_ok=True)
        appconfig.set_sync(db, WELCOME_PATH_KEY, "")


def save_welcome_image(db: Session, data: bytes) -> None:
    """Save an admin-uploaded welcome banner (no Gemini needed)."""
    from core.gemini import WELCOME_PATH_KEY, WELCOME_SLUG, cards_dir, welcome_image_file

    # Clear any prior cached file (could be a different extension) then write PNG.
    old = welcome_image_file()
    if old is not None:
        old.unlink(missing_ok=True)
    (cards_dir() / f"{WELCOME_SLUG}.png").write_bytes(data)
    appconfig.set_sync(db, WELCOME_PATH_KEY, f"/cards/{WELCOME_SLUG}.png")


def gemini_budget(db: Session) -> float:
    return appconfig.get_float_sync(db, appconfig.GEMINI_WEEKLY_BUDGET, settings.gemini_weekly_budget_usd)


def set_gemini_budget(db: Session, value: float) -> None:
    appconfig.set_sync(db, appconfig.GEMINI_WEEKLY_BUDGET, f"{value:.2f}")


def gemini_stats(db: Session) -> dict:
    return {
        "budget": gemini_budget(db),
        "spent": gemini_usage.weekly_spend_sync(db),
        "images_this_week": gemini_usage.image_count_window_sync(db),
        "configured": bool(settings.gemini_api_key),
    }


# ── Rewards & referrals ─────────────────────────────────────────────────────────

def _points(db: Session, user_id: int, reasons: tuple[str, ...] | None = None) -> int:
    stmt = select(func.coalesce(func.sum(PointsLedger.delta), 0)).where(PointsLedger.user_id == user_id)
    if reasons:
        stmt = stmt.where(PointsLedger.reason.in_(reasons))
    return int(db.scalar(stmt) or 0)


def user_rewards(db: Session, user_id: int) -> dict:
    """Per-user rewards/referral summary (mirrors rewards.referral_stats, sync)."""
    user = db.get(User, user_id)
    inviter = None
    if user and user.referred_by:
        inv = db.get(User, user.referred_by)
        if inv:
            inviter = {"id": inv.id, "username": inv.username, "telegram_id": inv.telegram_id}
    direct = int(db.scalar(select(func.count()).select_from(Referral).where(Referral.inviter_id == user_id)) or 0)
    unlocked = int(db.scalar(select(func.count()).select_from(Referral).where(
        Referral.inviter_id == user_id, Referral.status == "unlocked")) or 0)
    direct_ids = list(db.scalars(select(Referral.invitee_id).where(Referral.inviter_id == user_id)))
    indirect = 0
    if direct_ids:
        indirect = int(db.scalar(select(func.count()).select_from(Referral).where(
            Referral.inviter_id.in_(direct_ids))) or 0)
    return {
        "points": _points(db, user_id),
        "referral_code": (user.referral_code if user else None),
        "inviter": inviter,
        "direct": direct,
        "indirect": indirect,
        "unlocked": unlocked,
        "referral_points": _points(db, user_id, ("referral", "referral_signup")),
        "unlock_bets": REFERRAL_UNLOCK_BETS,
    }


def user_referees(db: Session, user_id: int, limit: int = 50) -> list[dict]:
    """The people this user referred (invitees), with unlock status + bets-to-unlock."""
    rows = db.execute(
        select(
            Referral.invitee_id, Referral.status, Referral.created_at, Referral.unlocked_at,
            User.username, User.telegram_id, func.coalesce(UserStats.total_bets, 0),
        )
        .join(User, User.id == Referral.invitee_id)
        .outerjoin(UserStats, UserStats.user_id == Referral.invitee_id)
        .where(Referral.inviter_id == user_id)
        .order_by(Referral.created_at.desc())
        .limit(limit)
    ).all()
    return [
        {"user_id": invitee_id, "username": username, "telegram_id": tg,
         "status": status, "created_at": created, "unlocked_at": unlocked, "bets": int(bets or 0)}
        for invitee_id, status, created, unlocked, username, tg, bets in rows
    ]


def referral_overview(db: Session) -> dict:
    edges = int(db.scalar(select(func.count()).select_from(Referral)) or 0)
    unlocked = int(db.scalar(select(func.count()).select_from(Referral).where(Referral.status == "unlocked")) or 0)
    return {
        "total_points": int(db.scalar(select(func.coalesce(func.sum(PointsLedger.delta), 0))) or 0),
        "edges": edges,
        "unlocked": unlocked,
        "pending": edges - unlocked,
        "with_code": int(db.scalar(select(func.count()).select_from(User).where(User.referral_code.isnot(None))) or 0),
        "unlock_bets": REFERRAL_UNLOCK_BETS,
    }


def referral_edges(db: Session) -> list[dict]:
    """Every referral edge (inviter → invitee), newest first — for CSV export.
    Public columns only (no key material)."""
    Inv, Ree = aliased(User), aliased(User)
    rows = db.execute(
        select(
            Referral.inviter_id, Inv.username, Referral.invitee_id, Ree.username,
            Referral.status, Referral.created_at, Referral.unlocked_at,
            func.coalesce(UserStats.total_bets, 0),
        )
        .join(Inv, Inv.id == Referral.inviter_id)
        .join(Ree, Ree.id == Referral.invitee_id)
        .outerjoin(UserStats, UserStats.user_id == Referral.invitee_id)
        .order_by(Referral.created_at.desc())
    ).all()
    return [
        {"inviter_id": iid, "inviter_username": iu, "invitee_id": eid, "invitee_username": eu,
         "status": st, "created_at": cr, "unlocked_at": ul, "bets": int(bets or 0)}
        for iid, iu, eid, eu, st, cr, ul, bets in rows
    ]


def top_referrers(db: Session, limit: int = 20) -> list[dict]:
    rows = db.execute(
        select(
            Referral.inviter_id,
            func.count().label("direct"),
            func.sum(case((Referral.status == "unlocked", 1), else_=0)).label("unlocked"),
        )
        .group_by(Referral.inviter_id)
        .order_by(func.count().desc())
        .limit(limit)
    ).all()
    out = []
    for inviter_id, direct, unlocked in rows:
        u = db.get(User, inviter_id)
        out.append({
            "user_id": inviter_id,
            "username": (u.username if u else None),
            "telegram_id": (u.telegram_id if u else None),
            "direct": int(direct or 0),
            "unlocked": int(unlocked or 0),
            "points": _points(db, inviter_id),
        })
    return out


# ── News pipeline (KEYLESS: never calls Gemini/Telegram; only DB flag-writes) ────

# Statuses still in flight (admin-approved, render not finished) roll up under
# "approved" in the overview tile.
_NEWS_INFLIGHT = ("approved", "translating", "rendering")


def news_overview(db: Session) -> dict:
    counts = {"backlog": 0, "approved": 0, "ready": 0, "sent": 0, "rejected": 0}
    for status, n in db.execute(select(NewsItem.status, func.count()).group_by(NewsItem.status)).all():
        key = "approved" if status in _NEWS_INFLIGHT else status
        if key in counts:
            counts[key] += int(n or 0)
    counts["sources"] = db.scalar(select(func.count()).select_from(NewsSource)) or 0
    counts["enabled_sources"] = db.scalar(
        select(func.count()).select_from(NewsSource).where(NewsSource.enabled.is_(True))) or 0
    return counts


def news_bets_overview(db: Session, *, limit: int = 50) -> dict:
    """Settleable bets driven by the news channel (Bet.source='news') + the
    deferred-intent conversion funnel. Read-only — no secrets (Bet/PendingIntent
    hold none)."""
    is_news = Bet.source == "news"
    total = int(db.scalar(select(func.count()).select_from(Bet).where(is_news)) or 0)
    volume = float(db.scalar(select(func.coalesce(func.sum(Bet.amount_usd), 0)).where(is_news)) or 0)
    bet_status = {"OPEN": 0, "WON": 0, "LOST": 0, "VOID": 0}
    for st, n in db.execute(select(Bet.status, func.count()).where(is_news).group_by(Bet.status)).all():
        if st in bet_status:
            bet_status[st] = int(n or 0)

    recent = [
        {"id": b.id, "user_id": b.user_id, "market_id": b.market_id, "question": b.question,
         "outcome": b.outcome, "amount": _f(b.amount_usd), "status": b.status, "created_at": b.created_at}
        for b in db.execute(select(Bet).where(is_news).order_by(Bet.created_at.desc()).limit(limit)).scalars().all()
    ]

    funnel = {s: 0 for s in ("pending", "resumed", "fulfilled", "expired", "cancelled")}
    for st, n in db.execute(select(PendingIntent.status, func.count()).group_by(PendingIntent.status)).all():
        if st in funnel:
            funnel[st] = int(n or 0)
    intent_total = sum(funnel.values())
    conversion = round(funnel["fulfilled"] / intent_total * 100, 1) if intent_total else 0.0
    return {"total": total, "volume": volume, "status": bet_status, "recent": recent,
            "funnel": funnel, "intent_total": intent_total, "conversion": conversion}


def _news_item_dict(it: NewsItem, *, category: str | None, source: str | None) -> dict:
    return {
        "id": it.id,
        "title": it.title_orig,
        "status": it.status,
        "score": _f(it.score),
        "category_id": it.category_id,
        "category": category,
        "source": source,
        "url": it.url,
        "lang_orig": it.lang_orig,
        "has_cta": bool(it.cta_market_id),
        "cta_market_id": it.cta_market_id,
        "cta_url": it.cta_url,
        "image_status": it.image_status,
        "hero_image_url": it.hero_image_url,
        "translations": dict(it.translations or {}),
        "fetched_at": it.fetched_at,
        "approved_at": it.approved_at,
        "published_at": it.published_at,
    }


def _name_maps(db: Session, items: list[NewsItem]) -> tuple[dict, dict]:
    cat_ids = {i.category_id for i in items if i.category_id}
    src_ids = {i.source_id for i in items if i.source_id}
    cats = {c.id: c.title for c in db.scalars(select(Category).where(Category.id.in_(cat_ids)))} if cat_ids else {}
    srcs = {s.id: s.name for s in db.scalars(select(NewsSource).where(NewsSource.id.in_(src_ids)))} if src_ids else {}
    return cats, srcs


def list_news_items(db: Session, *, status: str | None = None, category_id: int | None = None,
                    limit: int = 50, offset: int = 0) -> list[dict]:
    stmt = select(NewsItem)
    if status == "approved":  # match the overview rollup (incl. in-flight render states)
        stmt = stmt.where(NewsItem.status.in_(_NEWS_INFLIGHT))
    elif status:
        stmt = stmt.where(NewsItem.status == status)
    if category_id:
        stmt = stmt.where(NewsItem.category_id == category_id)
    stmt = stmt.order_by(NewsItem.fetched_at.desc(), NewsItem.id.desc()).limit(limit).offset(offset)
    items = list(db.scalars(stmt))
    cats, srcs = _name_maps(db, items)
    return [_news_item_dict(i, category=cats.get(i.category_id), source=srcs.get(i.source_id)) for i in items]


def count_news_items(db: Session, *, status: str | None = None, category_id: int | None = None) -> int:
    stmt = select(func.count()).select_from(NewsItem)
    if status == "approved":
        stmt = stmt.where(NewsItem.status.in_(_NEWS_INFLIGHT))
    elif status:
        stmt = stmt.where(NewsItem.status == status)
    if category_id:
        stmt = stmt.where(NewsItem.category_id == category_id)
    return db.scalar(stmt) or 0


def news_item_detail(db: Session, item_id: int) -> dict | None:
    it = db.get(NewsItem, item_id)
    if it is None:
        return None
    cats, srcs = _name_maps(db, [it])
    d = _news_item_dict(it, category=cats.get(it.category_id), source=srcs.get(it.source_id))
    d["body_orig"] = it.body_orig
    return d


def curate_news_item(db: Session, item_id: int, action: str) -> bool:
    """Flag-write admin actions on a news item. No external calls — the keyed bot
    worker acts on the resulting status. Mirrors curate_category."""
    it = db.get(NewsItem, item_id)
    if it is None:
        return False
    if action == "approve":
        it.status = "approved"
        it.approved_at = datetime.now(timezone.utc)
        it.excluded_from_autopublish = False
    elif action == "reject":
        it.status = "rejected"
    elif action == "unapprove":
        it.status = "backlog"
        it.approved_at = None
    elif action == "rerender":
        # re-run translate + CTA on next render tick
        it.status = "approved"
    elif action == "regen_image":
        # re-queue for rendering too — render_job only picks up RENDERABLE
        # statuses, so without this the cleared image would never be rebuilt.
        it.image_status = "none"
        it.rendered_image_path = None
        it.status = "approved"
    else:
        return False
    return True


def update_news_translations(db: Session, item_id: int, translations: dict) -> bool:
    it = db.get(NewsItem, item_id)
    if it is None:
        return False
    it.translations = translations  # full replace (MutableDict)
    return True


def list_news_sources(db: Session) -> list[NewsSource]:
    return list(db.scalars(select(NewsSource).order_by(NewsSource.id.asc())))


def create_news_source(db: Session, *, name: str, url: str, category_id: int | None, kind: str) -> NewsSource | None:
    url = (url or "").strip()
    if not url or urlparse(url).scheme not in ("http", "https"):
        return None  # require an absolute http(s) URL (reject javascript:/data:/file:)
    url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()
    if db.scalar(select(NewsSource.id).where(NewsSource.url_hash == url_hash)) is not None:
        return None  # duplicate URL
    if kind not in ("auto", "rss", "html"):
        kind = "auto"
    if category_id is not None and db.get(Category, category_id) is None:
        category_id = None  # drop a stale/bogus FK rather than 500 on flush
    src = NewsSource(name=(name or url)[:255], url=url[:2048], url_hash=url_hash,
                     category_id=category_id, kind=kind, enabled=True)
    db.add(src)
    db.flush()
    return src


def curate_news_source(db: Session, source_id: int, action: str) -> bool:
    src = db.get(NewsSource, source_id)
    if src is None:
        return False
    if action == "toggle":
        src.enabled = not src.enabled
    elif action == "test":
        src.last_status = "pending"  # the crawl worker probes it on its next tick
    elif action == "delete":
        db.delete(src)
    else:
        return False
    return True


def news_integration_status(db: Session) -> dict:
    channel_id = appconfig.get_sync(db, NEWS_CHANNEL_ID, "") or ""
    return {
        "gemini": bool(settings.gemini_api_key),
        "telegram": bool(settings.telegram_bot_token),
        "channel": bool(channel_id),
        "pipeline_enabled": bool(settings.news_pipeline_enabled),
    }


def news_settings(db: Session) -> dict:
    s = news_integration_status(db)
    s.update({
        "channel_id": appconfig.get_sync(db, NEWS_CHANNEL_ID, "") or "",
        "channel_lang": settings.news_channel_lang,
        "top_n": int(appconfig.get_float_sync(db, NEWS_TOP_N, 5)),
        "autosend": appconfig.get_sync(db, NEWS_AUTOSEND, "0") == "1",
        "poll": appconfig.get_sync(db, NEWS_POLL, "1") == "1",  # default on
    })
    return s


def set_news_settings(db: Session, *, channel_id: str, top_n: int, autosend: bool, poll: bool = True) -> None:
    appconfig.set_sync(db, NEWS_CHANNEL_ID, (channel_id or "").strip())
    appconfig.set_sync(db, NEWS_TOP_N, str(max(1, min(int(top_n), 20))))
    appconfig.set_sync(db, NEWS_AUTOSEND, "1" if autosend else "0")
    appconfig.set_sync(db, NEWS_POLL, "1" if poll else "0")
