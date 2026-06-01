"""SQLAlchemy 2.x models — the shared spine for bot, dashboard and worker.

Design notes
------------
* Money/price/size columns are ``Numeric`` (never float) to avoid drift.
* Only ``Account.encrypted_private_key`` and ``Account.encrypted_api_creds`` hold
  secrets; they are ciphertext (encrypted explicitly in the repository layer via
  ``core.crypto``) — NOT auto-decrypting column types, so the dashboard process
  (which lacks ``ENCRYPTION_KEY``) physically cannot read key material.
* Types are kept portable: Postgres in prod, SQLite for unit tests
  (``Base.metadata.create_all``). No PG-only types (INET/JSONB) — ``JSON`` and
  ``String`` are used instead.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# BIGINT in Postgres (prod), INTEGER in SQLite (tests) so autoincrement works in
# both — SQLite only auto-increments INTEGER PRIMARY KEY, not BIGINT.
BigIntPK = BigInteger().with_variant(Integer, "sqlite")


class Base(DeclarativeBase):
    pass


# ── Enums (stored as strings) ────────────────────────────────────────────────

class UserStatus(str, enum.Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    BANNED = "banned"


class AccountMode(str, enum.Enum):
    DEMO = "demo"
    LIVE = "live"


class AccountStatus(str, enum.Enum):
    ACTIVE = "active"
    DISABLED = "disabled"
    ERROR = "error"


class BotLifecycle(str, enum.Enum):
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


# ── users ────────────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, index=True)
    username: Mapped[str | None] = mapped_column(String(255))
    first_name: Mapped[str | None] = mapped_column(String(255))
    language: Mapped[str] = mapped_column(String(5), default="en", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=UserStatus.ACTIVE.value, nullable=False, index=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active_account_id: Mapped[int | None] = mapped_column(BigInteger)  # soft pointer; no FK to avoid cycle
    # ── referral ──
    referral_code: Mapped[str | None] = mapped_column(String(32), unique=True, index=True)
    referred_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="SET NULL"))

    accounts: Mapped[list["Account"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    settings: Mapped["UserSettings | None"] = relationship(back_populates="user", cascade="all, delete-orphan", uselist=False)

    __table_args__ = (
        CheckConstraint("status in ('active','suspended','banned')", name="ck_user_status"),
        CheckConstraint("language in ('en','fa','ru','zh')", name="ck_user_lang"),
    )


# ── accounts ─────────────────────────────────────────────────────────────────

class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(64), default="Main", nullable=False)
    wallet_address: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    signature_type: Mapped[int] = mapped_column(SmallInteger, default=0, nullable=False)  # 0 EOA, 1 proxy, 2 safe
    funder_address: Mapped[str | None] = mapped_column(String(64))
    chain_id: Mapped[int] = mapped_column(Integer, default=137, nullable=False)

    # 🔒 ciphertext only — encrypted via core.crypto in the repository layer
    encrypted_private_key: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted_api_creds: Mapped[str | None] = mapped_column(Text)
    key_version: Mapped[int] = mapped_column(SmallInteger, default=1, nullable=False)

    mode: Mapped[str] = mapped_column(String(8), default=AccountMode.LIVE.value, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=AccountStatus.ACTIVE.value, nullable=False)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sync_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    user: Mapped["User"] = relationship(back_populates="accounts")
    positions: Mapped[list["Position"]] = relationship(back_populates="account", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("user_id", "label", name="uq_account_user_label"),
        CheckConstraint("signature_type in (0,1,2)", name="ck_account_sigtype"),
        CheckConstraint("mode in ('demo','live')", name="ck_account_mode"),
    )


# ── positions (cache) ────────────────────────────────────────────────────────

class Position(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    token_id: Mapped[str] = mapped_column(String(128), nullable=False)
    market_id: Mapped[str | None] = mapped_column(String(128))
    title: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[str | None] = mapped_column(String(64))
    size: Mapped[float] = mapped_column(Numeric(20, 6), nullable=False)
    avg_price: Mapped[float] = mapped_column(Numeric(10, 6), nullable=False)
    total_cost: Mapped[float] = mapped_column(Numeric(20, 6), nullable=False)
    cur_price: Mapped[float | None] = mapped_column(Numeric(10, 6))
    unrealized_pnl: Mapped[float | None] = mapped_column(Numeric(20, 6))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    account: Mapped["Account"] = relationship(back_populates="positions")

    __table_args__ = (UniqueConstraint("account_id", "token_id", name="uq_position_account_token"),)


# ── orders ───────────────────────────────────────────────────────────────────

class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    clob_order_id: Mapped[str | None] = mapped_column(String(128), index=True)
    token_id: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    side: Mapped[str] = mapped_column(String(4), nullable=False)  # BUY|SELL
    order_type: Mapped[str] = mapped_column(String(8), default="LIMIT", nullable=False)
    price: Mapped[float | None] = mapped_column(Numeric(10, 6))
    size: Mapped[float] = mapped_column(Numeric(20, 6), nullable=False)
    filled_size: Mapped[float] = mapped_column(Numeric(20, 6), default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # pending|open|partial|filled|cancelled|rejected
    fill_method: Mapped[str | None] = mapped_column(String(32))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_orders_account_status", "account_id", "status"),)


# ── trades ───────────────────────────────────────────────────────────────────

class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    order_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("orders.id", ondelete="SET NULL"))
    token_id: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[str | None] = mapped_column(String(64))
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    price: Mapped[float] = mapped_column(Numeric(10, 6), nullable=False)
    size: Mapped[float] = mapped_column(Numeric(20, 6), nullable=False)
    cost: Mapped[float] = mapped_column(Numeric(20, 6), nullable=False)
    fee: Mapped[float] = mapped_column(Numeric(20, 6), default=0, nullable=False)
    slippage: Mapped[float | None] = mapped_column(Numeric(20, 6))
    pnl: Mapped[float | None] = mapped_column(Numeric(20, 6))
    fill_method: Mapped[str | None] = mapped_column(String(32))
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    executed_at: Mapped[datetime] = _now()

    __table_args__ = (
        Index("ix_trades_account_time", "account_id", "executed_at"),
        Index("ix_trades_executed_at", "executed_at"),
    )


# ── bot lifecycle state (per-user) ───────────────────────────────────────────

class BotStateRow(Base):
    __tablename__ = "bot_states"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    state: Mapped[str] = mapped_column(String(8), default=BotLifecycle.RUNNING.value, nullable=False)
    changed_at: Mapped[datetime] = _now()
    changed_by: Mapped[str | None] = mapped_column(String(16))
    chat_id: Mapped[int | None] = mapped_column(BigInteger)


# ── command queue (cross-process) ────────────────────────────────────────────

class Command(Base):
    __tablename__ = "commands"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    account_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("accounts.id", ondelete="CASCADE"))
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(12), default="pending", nullable=False)
    requested_at: Mapped[datetime] = _now()
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_commands_status_time", "status", "requested_at"),)


# ── per-user settings ────────────────────────────────────────────────────────

class UserSettings(Base):
    __tablename__ = "user_settings"

    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    confirm_trades: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    notifications_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    timezone: Mapped[str] = mapped_column(String(48), default="UTC", nullable=False)
    extra: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    user: Mapped["User"] = relationship(back_populates="settings")


# ── dashboard admins (separate from Telegram users) ──────────────────────────

class Admin(Base):
    __tablename__ = "admins"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str | None] = mapped_column(String(255))
    is_superadmin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()


# ── Mini App categories (Polymarket tags as swipeable cards) ─────────────────

class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    tag_id: Mapped[str | None] = mapped_column(String(64))        # Polymarket tag id
    tag_slug: Mapped[str | None] = mapped_column(String(128))     # Polymarket tag slug
    volume: Mapped[float] = mapped_column(Numeric(20, 2), default=0, nullable=False)  # cached, for sort
    # image (Gemini) — image_path is a cached file under the webapp static dir
    image_path: Mapped[str | None] = mapped_column(String(256))
    image_status: Mapped[str] = mapped_column(String(12), default="none", nullable=False)  # none|generating|ready|failed
    image_prompt: Mapped[str | None] = mapped_column(Text)         # the prompt actually used
    prompt_override: Mapped[str | None] = mapped_column(Text)      # admin-set custom prompt
    image_generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # curation
    pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    hidden: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_categories_sort", "hidden", "pinned", "display_order"),)


class GeminiUsage(Base):
    """One row per Gemini image-generation attempt — the spend ledger the weekly
    budget is computed from."""

    __tablename__ = "gemini_usage"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = _now()
    kind: Mapped[str] = mapped_column(String(16), default="image", nullable=False)
    category_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("categories.id", ondelete="SET NULL"))
    model: Mapped[str | None] = mapped_column(String(64))
    cost_usd: Mapped[float] = mapped_column(Numeric(10, 4), default=0, nullable=False)
    ok: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    __table_args__ = (Index("ix_gemini_usage_ts", "ts"),)


class BetStatus(str, enum.Enum):
    OPEN = "OPEN"
    WON = "WON"
    LOST = "LOST"
    VOID = "VOID"


class Bet(Base):
    """A settleable prediction (primarily Mini App bets). The settlement engine
    resolves OPEN bets when their market resolves on Polymarket."""

    __tablename__ = "bets"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    account_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("accounts.id", ondelete="SET NULL"))
    market_id: Mapped[str] = mapped_column(String(128), nullable=False)   # conditionId
    token_id: Mapped[str] = mapped_column(String(128), nullable=False)    # outcome token bought
    question: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(String(8), nullable=False)       # YES|NO
    amount_usd: Mapped[float] = mapped_column(Numeric(20, 6), nullable=False)
    entry_price: Mapped[float | None] = mapped_column(Numeric(10, 6))     # implied prob of chosen outcome
    shares: Mapped[float | None] = mapped_column(Numeric(20, 6))
    status: Mapped[str] = mapped_column(String(8), default=BetStatus.OPEN.value, nullable=False, index=True)
    payout_usd: Mapped[float] = mapped_column(Numeric(20, 6), default=0, nullable=False)
    pnl_usd: Mapped[float] = mapped_column(Numeric(20, 6), default=0, nullable=False)
    brier: Mapped[float | None] = mapped_column(Numeric(10, 6))
    source: Mapped[str] = mapped_column(String(8), default="miniapp", nullable=False)
    clob_order_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = _now()
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_bets_status_market", "status", "market_id"),)


class UserStats(Base):
    """Gamification stats derived from real betting activity (daily streak +
    totals + settled results). Updated on bet placement and settlement."""

    __tablename__ = "user_stats"

    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    current_streak: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    longest_streak: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_active_date: Mapped[str | None] = mapped_column(String(10))  # 'YYYY-MM-DD' (UTC)
    total_bets: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_volume_usd: Mapped[float] = mapped_column(Numeric(20, 2), default=0, nullable=False)
    # ── settled results (from the settlement engine) ──
    wins: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    losses: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    settled_bets: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    realized_pnl_usd: Mapped[float] = mapped_column(Numeric(20, 6), default=0, nullable=False)
    brier_sum: Mapped[float] = mapped_column(Numeric(20, 6), default=0, nullable=False)  # avg = brier_sum/settled_bets
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_user_stats_bets", "total_bets"),
        Index("ix_user_stats_volume", "total_volume_usd"),
    )


class PointsLedger(Base):
    """Append-only points ledger — a user's balance is SUM(delta). No ad-hoc
    balance writes, so points can never be lost or double-counted."""

    __tablename__ = "points_ledger"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    delta: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)   # bet|win|streak|signup|referral_l1|...
    ref: Mapped[str | None] = mapped_column(String(64))               # e.g. bet id / invitee id
    created_at: Mapped[datetime] = _now()

    __table_args__ = (Index("ix_points_user", "user_id"),)


class Referral(Base):
    """A referral edge (inviter → invitee). Reward unlocks only after the invitee
    completes real activity (conditional unlock = anti-fraud)."""

    __tablename__ = "referrals"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    inviter_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    invitee_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(12), default="pending", nullable=False)  # pending|unlocked
    created_at: Mapped[datetime] = _now()
    unlocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AppConfig(Base):
    """Runtime-editable key/value config (e.g. the live Gemini weekly budget),
    so admins can change settings without a redeploy."""

    __tablename__ = "app_config"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


# ── audit log (security events) ──────────────────────────────────────────────

class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = _now()
    actor_type: Mapped[str] = mapped_column(String(12), nullable=False)  # admin|user|bot|worker|system
    actor_id: Mapped[int | None] = mapped_column(BigInteger)
    user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="SET NULL"))
    account_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("accounts.id", ondelete="SET NULL"))
    event: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    detail: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)  # NEVER secrets
    ip: Mapped[str | None] = mapped_column(String(45))

    __table_args__ = (Index("ix_audit_ts", "ts"),)
