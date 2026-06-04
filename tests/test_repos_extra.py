"""Extra repo coverage: commands / orders / users / categories repositories.

All four repos take an AsyncSession; we use DB pattern (a) — the conftest temp DB
via ``async_session_scope`` (commits on exit, shares the per-test pristine schema).
FKs are enforced (PRAGMA foreign_keys=ON), so Command/Order/Trade rows need real
parent User/Account rows.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from db.engine import async_session_scope
from db.models import Account, Category, Command, Order, Trade, User, UserSettings, UserStatus
from db.repositories import categories as cat_repo
from db.repositories import commands as cmd_repo
from db.repositories import orders as ord_repo
from db.repositories import users as users_repo


# ── helpers ──────────────────────────────────────────────────────────────────

async def _mk_user(s, telegram_id: int = 1001) -> User:
    return await users_repo.get_or_create_user(
        s, telegram_id=telegram_id, username="u", first_name="U", default_language="en"
    )


async def _mk_account(s, user_id: int, label: str = "Main") -> Account:
    """Minimal Account row (FK target for orders/trades) — encryption layer not needed."""
    acc = Account(
        user_id=user_id,
        label=label,
        wallet_address="0x" + "a" * 40,
        encrypted_private_key="ciphertext",
    )
    s.add(acc)
    await s.flush()
    return acc


# ── commands.py ──────────────────────────────────────────────────────────────

async def test_pending_only_returns_pending_ordered_and_capped():
    async with async_session_scope() as s:
        u = await _mk_user(s, 2001)
        now = datetime.now(timezone.utc)
        # Two pending (out of insertion order by requested_at) + one non-pending.
        c_late = Command(user_id=u.id, action="broadcast", status="pending",
                         requested_at=now + timedelta(seconds=5))
        c_early = Command(user_id=u.id, action="broadcast", status="pending",
                          requested_at=now + timedelta(seconds=1))
        c_done = Command(user_id=u.id, action="broadcast", status="done",
                         requested_at=now)
        s.add_all([c_late, c_early, c_done])
        await s.flush()

        rows = await cmd_repo.pending(s)
        # Only pending rows, ordered by requested_at ascending.
        assert [r.status for r in rows] == ["pending", "pending"]
        assert [r.requested_at for r in rows] == sorted(r.requested_at for r in rows)
        assert rows[0].id == c_early.id and rows[1].id == c_late.id

        # limit caps the result count.
        assert len(await cmd_repo.pending(s, limit=1)) == 1


async def test_pending_filtered_by_action():
    async with async_session_scope() as s:
        u = await _mk_user(s, 2002)
        s.add_all([
            Command(user_id=u.id, action="broadcast", status="pending"),
            Command(user_id=u.id, action="sync", status="pending"),
        ])
        await s.flush()

        rows = await cmd_repo.pending(s, action="sync")
        assert len(rows) == 1 and rows[0].action == "sync"


async def test_telegram_id_for_known_and_unknown():
    async with async_session_scope() as s:
        u = await _mk_user(s, 2003)
        assert await cmd_repo.telegram_id_for(s, u.id) == 2003
        # Unknown internal id → None.
        assert await cmd_repo.telegram_id_for(s, 999999) is None


async def test_mark_sets_status_and_processed_at():
    async with async_session_scope() as s:
        u = await _mk_user(s, 2004)
        c = Command(user_id=u.id, action="broadcast", status="pending")
        s.add(c)
        await s.flush()
        assert c.processed_at is None

        await cmd_repo.mark(s, c.id, "done")
        assert c.status == "done"
        assert c.processed_at is not None


async def test_mark_missing_id_is_noop():
    async with async_session_scope() as s:
        # No row with this id → must not raise.
        await cmd_repo.mark(s, 123456, "done")


# ── orders.py ────────────────────────────────────────────────────────────────

async def test_log_order_uppercases_side_and_type_and_flushes_id():
    async with async_session_scope() as s:
        u = await _mk_user(s, 3001)
        acc = await _mk_account(s, u.id)

        order = await ord_repo.log_order(
            s, account_id=acc.id, token_id="tok-1", side="buy", order_type="limit",
            size=10.0, price=0.42, status="open", clob_order_id="clob-1", title="Market?",
        )
        assert order.id is not None  # flushed
        assert order.side == "BUY" and order.order_type == "LIMIT"
        assert order.clob_order_id == "clob-1"
        assert float(order.size) == 10.0 and float(order.price) == pytest.approx(0.42)
        assert order.status == "open"


async def test_log_order_persists_error_and_null_price():
    async with async_session_scope() as s:
        u = await _mk_user(s, 3002)
        acc = await _mk_account(s, u.id)
        order = await ord_repo.log_order(
            s, account_id=acc.id, token_id="tok-2", side="sell", order_type="market",
            size=5.0, price=None, status="rejected", error="boom",
        )
        assert order.side == "SELL" and order.order_type == "MARKET"
        assert order.price is None and order.error == "boom"


async def test_log_trade_persists_fields():
    async with async_session_scope() as s:
        u = await _mk_user(s, 3003)
        acc = await _mk_account(s, u.id)
        trade = await ord_repo.log_trade(
            s, account_id=acc.id, token_id="tok-3", side="buy", price=0.5, size=8.0,
            cost=4.0, fee=0.1, pnl=1.25, title="T?", outcome="YES",
            fill_method="clob", is_demo=True,
        )
        assert trade.id is not None
        assert trade.side == "BUY"
        assert float(trade.cost) == 4.0 and float(trade.fee) == pytest.approx(0.1)
        assert float(trade.pnl) == pytest.approx(1.25)
        assert trade.outcome == "YES" and trade.fill_method == "clob"
        assert trade.is_demo is True


async def test_log_trade_defaults_fee_zero_pnl_none():
    async with async_session_scope() as s:
        u = await _mk_user(s, 3004)
        acc = await _mk_account(s, u.id)
        trade = await ord_repo.log_trade(
            s, account_id=acc.id, token_id="tok-4", side="SELL", price=0.3, size=2.0, cost=0.6,
        )
        assert float(trade.fee) == 0.0
        assert trade.pnl is None
        assert trade.is_demo is False


async def test_recent_orders_newest_first_and_limit_scoped_by_account():
    async with async_session_scope() as s:
        u = await _mk_user(s, 3005)
        acc = await _mk_account(s, u.id, label="A")
        other = await _mk_account(s, u.id, label="B")
        base = datetime.now(timezone.utc)
        # Explicit created_at so ordering is deterministic (server_default would tie).
        o1 = Order(account_id=acc.id, token_id="t", side="BUY", order_type="LIMIT",
                   size=1, status="open", created_at=base)
        o2 = Order(account_id=acc.id, token_id="t", side="BUY", order_type="LIMIT",
                   size=1, status="open", created_at=base + timedelta(seconds=10))
        o3 = Order(account_id=acc.id, token_id="t", side="BUY", order_type="LIMIT",
                   size=1, status="open", created_at=base + timedelta(seconds=20))
        # Belongs to a different account — must be excluded.
        o_other = Order(account_id=other.id, token_id="t", side="BUY", order_type="LIMIT",
                        size=1, status="open", created_at=base + timedelta(seconds=30))
        s.add_all([o1, o2, o3, o_other])
        await s.flush()

        rows = await ord_repo.recent_orders(s, acc.id)
        assert [r.id for r in rows] == [o3.id, o2.id, o1.id]  # newest first
        assert all(r.account_id == acc.id for r in rows)

        # limit caps; still newest-first.
        capped = await ord_repo.recent_orders(s, acc.id, limit=2)
        assert [r.id for r in capped] == [o3.id, o2.id]


async def test_recent_trades_newest_first_and_limit():
    async with async_session_scope() as s:
        u = await _mk_user(s, 3006)
        acc = await _mk_account(s, u.id)
        base = datetime.now(timezone.utc)
        t1 = Trade(account_id=acc.id, token_id="t", side="BUY", price=0.1, size=1, cost=0.1,
                   executed_at=base)
        t2 = Trade(account_id=acc.id, token_id="t", side="BUY", price=0.1, size=1, cost=0.1,
                   executed_at=base + timedelta(seconds=10))
        t3 = Trade(account_id=acc.id, token_id="t", side="BUY", price=0.1, size=1, cost=0.1,
                   executed_at=base + timedelta(seconds=20))
        s.add_all([t1, t2, t3])
        await s.flush()

        rows = await ord_repo.recent_trades(s, acc.id)
        assert [r.id for r in rows] == [t3.id, t2.id, t1.id]
        capped = await ord_repo.recent_trades(s, acc.id, limit=1)
        assert [r.id for r in capped] == [t3.id]


# ── users.py ─────────────────────────────────────────────────────────────────

async def test_get_or_create_creates_user_and_settings():
    async with async_session_scope() as s:
        u = await users_repo.get_or_create_user(
            s, telegram_id=4001, username="alice", first_name="Alice", default_language="en"
        )
        assert u.id is not None
        assert u.telegram_id == 4001 and u.language == "en"
        # A UserSettings row is created alongside the user.
        settings = await s.get(UserSettings, u.id)
        assert settings is not None and settings.user_id == u.id


async def test_get_or_create_idempotent_and_refreshes_names():
    async with async_session_scope() as s:
        first = await users_repo.get_or_create_user(
            s, telegram_id=4002, username="old", first_name="Old"
        )
        fid = first.id
        # Same telegram_id → same row, names refreshed.
        again = await users_repo.get_or_create_user(
            s, telegram_id=4002, username="new", first_name="New"
        )
        assert again.id == fid
        assert again.username == "new" and again.first_name == "New"


async def test_get_or_create_normalizes_unsupported_language():
    async with async_session_scope() as s:
        # Unsupported language falls back to default "en".
        u = await users_repo.get_or_create_user(s, telegram_id=4003, default_language="xx")
        assert u.language == "en"


async def test_get_user_unknown_returns_none():
    async with async_session_scope() as s:
        assert await users_repo.get_user(s, 999999) is None


async def test_set_language_normalizes_and_persists():
    async with async_session_scope() as s:
        u = await _mk_user(s, 4004)
        await users_repo.set_language(s, 4004, "fa")
        assert u.language == "fa"
        # Unsupported → normalized to "en".
        await users_repo.set_language(s, 4004, "zzz")
        assert u.language == "en"


async def test_set_language_unknown_user_is_noop():
    async with async_session_scope() as s:
        # No user with this telegram_id → must not raise.
        await users_repo.set_language(s, 888888, "fa")


async def test_set_active_account():
    async with async_session_scope() as s:
        u = await _mk_user(s, 4005)
        acc = await _mk_account(s, u.id)
        await users_repo.set_active_account(s, 4005, acc.id)
        assert u.active_account_id == acc.id
        # Can be cleared back to None.
        await users_repo.set_active_account(s, 4005, None)
        assert u.active_account_id is None


async def test_get_settings_returns_row():
    async with async_session_scope() as s:
        u = await _mk_user(s, 4006)
        settings = await users_repo.get_settings(s, u.id)
        assert isinstance(settings, UserSettings) and settings.user_id == u.id
        # Unknown id → None.
        assert await users_repo.get_settings(s, 777777) is None


async def test_is_blocked_active_suspended_banned():
    async with async_session_scope() as s:
        u = await _mk_user(s, 4007)
        # Default status is ACTIVE → not blocked.
        assert await users_repo.is_blocked(s, 4007) is False

        u.status = UserStatus.SUSPENDED.value
        await s.flush()
        assert await users_repo.is_blocked(s, 4007) is True

        u.status = UserStatus.BANNED.value
        await s.flush()
        assert await users_repo.is_blocked(s, 4007) is True


async def test_is_blocked_unknown_user_false():
    async with async_session_scope() as s:
        assert await users_repo.is_blocked(s, 666666) is False


# ── categories.py ────────────────────────────────────────────────────────────

async def test_upsert_from_tag_inserts_then_updates_in_place():
    async with async_session_scope() as s:
        c1 = await cat_repo.upsert_from_tag(
            s, slug="politics", title="Politics", tag_id="t1", tag_slug="pol", volume=100.0
        )
        cid = c1.id
        assert c1.title == "Politics" and float(c1.volume) == 100.0

        # Same slug → updates the SAME row in place (no new row).
        c2 = await cat_repo.upsert_from_tag(
            s, slug="politics", title="Politics 2", tag_id="t2", tag_slug="pol2", volume=250.0
        )
        assert c2.id == cid
        assert c2.title == "Politics 2" and c2.tag_id == "t2" and c2.tag_slug == "pol2"
        assert float(c2.volume) == 250.0

        # Exactly one row exists for that slug.
        all_pol = list(await s.scalars(select(Category).where(Category.slug == "politics")))
        assert len(all_pol) == 1


async def test_list_visible_excludes_hidden_and_orders():
    async with async_session_scope() as s:
        # pinned beats display_order beats volume; hidden excluded entirely.
        pinned = Category(slug="p", title="Pinned", volume=1, pinned=True, display_order=99)
        ord_lo = Category(slug="a", title="OrderLow", volume=1, display_order=0)
        ord_lo_highvol = Category(slug="b", title="OrderLowHighVol", volume=500, display_order=0)
        ord_hi = Category(slug="c", title="OrderHigh", volume=999, display_order=5)
        hidden = Category(slug="h", title="Hidden", volume=999, hidden=True)
        s.add_all([pinned, ord_lo, ord_lo_highvol, ord_hi, hidden])
        await s.flush()

        rows = await cat_repo.list_visible(s)
        slugs = [r.slug for r in rows]
        assert "h" not in slugs  # hidden excluded
        # pinned first; then display_order asc (0 before 5); within same order, volume desc.
        assert slugs == ["p", "b", "a", "c"]

        # limit caps.
        assert len(await cat_repo.list_visible(s, limit=2)) == 2


async def test_get_and_get_by_slug():
    async with async_session_scope() as s:
        c = await cat_repo.upsert_from_tag(
            s, slug="sports", title="Sports", tag_id=None, tag_slug=None, volume=0.0
        )
        assert (await cat_repo.get(s, c.id)).slug == "sports"
        assert (await cat_repo.get_by_slug(s, "sports")).id == c.id
        # Misses → None.
        assert await cat_repo.get(s, 424242) is None
        assert await cat_repo.get_by_slug(s, "nope") is None


async def test_set_image_ready_stamps_generated_at():
    async with async_session_scope() as s:
        c = await cat_repo.upsert_from_tag(
            s, slug="crypto", title="Crypto", tag_id=None, tag_slug=None, volume=0.0
        )
        assert c.image_generated_at is None

        await cat_repo.set_image(s, c.id, path="/img/crypto.png", status="ready", prompt="a prompt")
        assert c.image_status == "ready"
        assert c.image_path == "/img/crypto.png"
        assert c.image_prompt == "a prompt"
        assert c.image_generated_at is not None  # stamped only for "ready"


async def test_set_image_non_ready_does_not_stamp():
    async with async_session_scope() as s:
        c = await cat_repo.upsert_from_tag(
            s, slug="weather", title="Weather", tag_id=None, tag_slug=None, volume=0.0
        )
        await cat_repo.set_image(s, c.id, path=None, status="generating")
        assert c.image_status == "generating"
        assert c.image_generated_at is None
        # path/prompt left untouched when passed as None.
        assert c.image_path is None and c.image_prompt is None


async def test_set_image_missing_id_is_noop():
    async with async_session_scope() as s:
        # No category with this id → must not raise.
        await cat_repo.set_image(s, 313131, path="x", status="ready")


async def test_needing_images_only_visible_none_or_failed():
    async with async_session_scope() as s:
        c_none = Category(slug="n", title="None", volume=10, image_status="none")
        c_failed = Category(slug="f", title="Failed", volume=20, image_status="failed")
        c_ready = Category(slug="r", title="Ready", volume=30, image_status="ready")
        c_generating = Category(slug="g", title="Generating", volume=40, image_status="generating")
        c_hidden = Category(slug="hf", title="HiddenFailed", volume=50,
                            image_status="failed", hidden=True)
        s.add_all([c_none, c_failed, c_ready, c_generating, c_hidden])
        await s.flush()

        rows = await cat_repo.needing_images(s)
        slugs = {r.slug for r in rows}
        # Only visible categories whose image_status is none/failed.
        assert slugs == {"n", "f"}

        assert len(await cat_repo.needing_images(s, limit=1)) == 1
