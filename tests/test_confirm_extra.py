"""Money-path coverage for bot/handlers/confirm.py.

Covers the confirm/decline callbacks, _execute happy + error/guard branches for
every order kind, the audit/order-log side effects, news-bet recording, and the
pure helpers (_fail_key / _exc_detail / _result_detail / _notional_usd).

Network + Telegram are fully faked; DB is the conftest temp sqlite (pattern (a):
confirm opens its own sessions via async_session_scope)."""

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from bot.handlers import confirm
from db.engine import async_session_scope
from db.models import AuditLog, Bet, Order, PendingIntent
from db.repositories import accounts as accounts_repo
from db.repositories import pending_intents as intents_repo
from db.repositories import users as users_repo
from polymarket.credentials import (
    NoAccountConnected,
    PolymarketCreds,
    TradingUnavailable,
)


# ── fakes ────────────────────────────────────────────────────────────────────

class _RecMsg:
    def __init__(self):
        self.sent = []

    async def reply_text(self, text, **kw):
        self.sent.append((text, kw))


class _Query:
    def __init__(self, data):
        self.data = data
        self.message = None
        self.edits = []

    async def answer(self, *a, **k):
        pass

    async def edit_message_text(self, text, *a, **kw):
        self.edits.append((text, kw))


def _update(*, query=None, msg=None):
    return SimpleNamespace(callback_query=query, effective_message=msg,
                           effective_user=SimpleNamespace(id=111), effective_chat=None)


def _ctx(**user_data):
    user_data.setdefault("lang", "en")
    return SimpleNamespace(user_data=user_data, bot=None,
                           application=SimpleNamespace(bot_data={}))


class _FakePM:
    """Fake Polymarket whose data methods are SYNC (handlers call via to_thread)."""

    def __init__(self, result=None, raise_exc=None):
        self._result = result if result is not None else {"orderID": "OID", "status": "matched"}
        self._raise = raise_exc
        self.calls = []

    def _ret(self, name, *args):
        self.calls.append((name, args))
        if self._raise is not None:
            raise self._raise
        return self._result

    def place_limit_order(self, *a):
        return self._ret("place_limit_order", *a)

    def place_market_order(self, *a):
        return self._ret("place_market_order", *a)

    def place_capped_buy(self, *a):
        return self._ret("place_capped_buy", *a)

    def cancel_order(self, *a):
        return self._ret("cancel_order", *a)

    def cancel_all_orders(self, *a):
        return self._ret("cancel_all_orders", *a)


class _FakeMgr:
    def __init__(self, pm, account_id, *, get_exc=None):
        self._pm = pm
        self._account_id = account_id
        self._get_exc = get_exc

    async def get_trading_client(self, uid, account_id=None):
        if self._get_exc is not None:
            raise self._get_exc
        return self._pm

    async def default_account_id(self, uid, account_id=None):
        return account_id if account_id is not None else self._account_id


def _install_mgr(monkeypatch, pm, account_id, *, get_exc=None):
    monkeypatch.setattr(confirm.common, "manager",
                        lambda ctx: _FakeMgr(pm, account_id, get_exc=get_exc))


# ── DB seeding ─────────────────────────────────────────────────────────────────

async def _seed_user_account(tg_id=900):
    """Real User + Account so default_account_id resolves and Order FK persists."""
    async with async_session_scope() as s:
        u = await users_repo.get_or_create_user(s, telegram_id=tg_id, username="u",
                                                 first_name="U", default_language="en")
        creds = PolymarketCreds(wallet_address="0x" + "a" * 40,
                                private_key="0x" + "1" * 64)
        acc = await accounts_repo.upsert_account(s, u.id, creds, label="Main", mode="live")
        return u.id, acc.id


# ── callbacks: on_confirm / on_decline ──────────────────────────────────────────

async def test_on_confirm_valid_intent_invokes_execute(monkeypatch):
    seen = {}

    async def fake_exec(update, context, intent, query=None):
        seen["intent"] = intent
        seen["query"] = query

    monkeypatch.setattr(confirm, "_execute", fake_exec)
    intent = confirm.make_intent("limit", side="buy", token_id="t", price=0.5, size=10)
    q = _Query("ord_ok:abcd1234")
    ctx = _ctx(pending_orders={"abcd1234": intent})
    await confirm.on_confirm(_update(query=q), ctx)
    assert seen["intent"] is intent and seen["query"] is q
    # consumed from the pending map (single-use)
    assert "abcd1234" not in ctx.user_data["pending_orders"]


async def test_on_confirm_missing_intent_shows_expired():
    q = _Query("ord_ok:nope")
    await confirm.on_confirm(_update(query=q), _ctx(pending_orders={}))
    assert q.edits and "expired" in q.edits[0][0].lower()


async def test_on_confirm_stale_intent_shows_expired(monkeypatch):
    called = {}

    async def fake_exec(*a, **k):
        called["hit"] = True

    monkeypatch.setattr(confirm, "_execute", fake_exec)
    intent = confirm.make_intent("limit", side="buy", token_id="t", price=0.5, size=10)
    intent["ts"] = 0  # epoch → far older than _TTL_SECONDS
    q = _Query("ord_ok:old1")
    await confirm.on_confirm(_update(query=q), _ctx(pending_orders={"old1": intent}))
    assert "hit" not in called  # never executed
    assert q.edits and "expired" in q.edits[0][0].lower()


async def test_on_decline_pops_intent_and_aborts():
    intent = confirm.make_intent("limit", side="buy", token_id="t", price=0.5, size=10)
    q = _Query("ord_no:zz99")
    ctx = _ctx(pending_orders={"zz99": intent})
    await confirm.on_decline(_update(query=q), ctx)
    assert "zz99" not in ctx.user_data["pending_orders"]
    # bot.confirm.aborted == "Cancelled — no order was placed."
    assert q.edits and "Cancelled" in q.edits[0][0]


# ── _execute happy paths ─────────────────────────────────────────────────────────

async def test_execute_limit_places_order_and_logs_open(monkeypatch):
    uid, acc_id = await _seed_user_account(tg_id=901)
    pm = _FakePM({"orderID": "OID", "status": "matched"})
    _install_mgr(monkeypatch, pm, acc_id)
    intent = confirm.make_intent("limit", side="buy", token_id="0xTOK", price=0.6, size=10)
    q = _Query("ord_ok:x")
    await confirm._execute(_update(query=q), _ctx(db_user_id=uid), intent, query=q)

    assert pm.calls[0] == ("place_limit_order", ("0xTOK", 0.6, 10, "buy"))
    # final edit is bot.order.placed (carries the order id)
    assert any("OID" in e[0] for e in q.edits)
    async with async_session_scope() as s:
        order = await s.scalar(select(Order).where(Order.account_id == acc_id))
        assert order is not None and order.status == "open"
        assert order.clob_order_id == "OID" and order.order_type == "LIMIT"
        assert order.side == "BUY" and float(order.price) == 0.6 and float(order.size) == 10.0


async def test_execute_market_buy_with_max_price_uses_capped_buy(monkeypatch):
    uid, acc_id = await _seed_user_account(tg_id=902)
    pm = _FakePM({"orderID": "OID", "status": "matched"})
    _install_mgr(monkeypatch, pm, acc_id)
    intent = confirm.make_intent("market", side="buy", token_id="0xT", amount=25.0,
                                 max_price=0.8, neg_risk=False)
    q = _Query("ord_ok:x")
    await confirm._execute(_update(query=q), _ctx(db_user_id=uid), intent, query=q)
    assert pm.calls[0] == ("place_capped_buy", ("0xT", 25.0, 0.8, False))


async def test_execute_market_buy_without_max_price_uses_market_order(monkeypatch):
    uid, acc_id = await _seed_user_account(tg_id=903)
    pm = _FakePM({"success": True})
    _install_mgr(monkeypatch, pm, acc_id)
    intent = confirm.make_intent("market", side="buy", token_id="0xT", amount=25.0)
    q = _Query("ord_ok:x")
    await confirm._execute(_update(query=q), _ctx(db_user_id=uid), intent, query=q)
    assert pm.calls[0] == ("place_market_order", ("0xT", 25.0, "buy"))


async def test_execute_close_sells_full_size_and_responds_closed(monkeypatch):
    uid, acc_id = await _seed_user_account(tg_id=904)
    pm = _FakePM({"orderID": "OID", "status": "matched"})
    _install_mgr(monkeypatch, pm, acc_id)
    intent = confirm.make_intent("close", token_id="0xTOKEN12345", size=7.0)
    q = _Query("ord_ok:x")
    await confirm._execute(_update(query=q), _ctx(db_user_id=uid), intent, query=q)
    assert pm.calls[0] == ("place_market_order", ("0xTOKEN12345", 7.0, "sell"))
    # bot.order.closed mentions "closed"
    assert any("closed" in e[0].lower() for e in q.edits)
    async with async_session_scope() as s:
        order = await s.scalar(select(Order).where(Order.account_id == acc_id))
        assert order.side == "SELL" and order.order_type == "MARKET"


async def test_execute_cancel_calls_cancel_order_and_responds(monkeypatch):
    uid, acc_id = await _seed_user_account(tg_id=905)
    pm = _FakePM({"success": True})
    _install_mgr(monkeypatch, pm, acc_id)
    intent = confirm.make_intent("cancel", order_id="ORD-99")
    q = _Query("ord_ok:x")
    await confirm._execute(_update(query=q), _ctx(db_user_id=uid), intent, query=q)
    assert pm.calls[0] == ("cancel_order", ("ORD-99",))
    assert any("ORD-99" in e[0] for e in q.edits)  # bot.order.cancelled
    # cancels are NOT logged as Order rows
    async with async_session_scope() as s:
        assert await s.scalar(select(Order)) is None


async def test_execute_cancel_all_reports_count(monkeypatch):
    uid, acc_id = await _seed_user_account(tg_id=906)
    pm = _FakePM({"canceled": ["a", "b", "c"]})
    _install_mgr(monkeypatch, pm, acc_id)
    intent = confirm.make_intent("cancel_all")
    q = _Query("ord_ok:x")
    await confirm._execute(_update(query=q), _ctx(db_user_id=uid), intent, query=q)
    assert pm.calls[0][0] == "cancel_all_orders"
    # bot.order.cancelled_all == "✅ Cancelled {count} open order(s)." → count 3
    assert any("3" in e[0] for e in q.edits)


# ── _execute error / guard paths ─────────────────────────────────────────────────

async def test_execute_no_account_connected_responds_no_account(monkeypatch):
    uid, acc_id = await _seed_user_account(tg_id=907)
    _install_mgr(monkeypatch, _FakePM(), acc_id, get_exc=NoAccountConnected(uid))
    intent = confirm.make_intent("market", side="buy", token_id="t", amount=10.0)
    q = _Query("ord_ok:x")
    await confirm._execute(_update(query=q), _ctx(db_user_id=uid), intent, query=q)
    # bot.error.no_account mentions "Connect"
    assert q.edits and "Connect" in q.edits[-1][0]


async def test_execute_trading_unavailable_responds(monkeypatch):
    uid, acc_id = await _seed_user_account(tg_id=908)
    _install_mgr(monkeypatch, _FakePM(), acc_id, get_exc=TradingUnavailable())
    intent = confirm.make_intent("market", side="buy", token_id="t", amount=10.0)
    q = _Query("ord_ok:x")
    await confirm._execute(_update(query=q), _ctx(db_user_id=uid), intent, query=q)
    # bot.error.trading_unavailable mentions "Trading"
    assert q.edits and "Trading" in q.edits[-1][0]


async def test_execute_client_build_generic_error(monkeypatch):
    uid, acc_id = await _seed_user_account(tg_id=909)
    _install_mgr(monkeypatch, _FakePM(), acc_id, get_exc=RuntimeError("weird"))
    intent = confirm.make_intent("market", side="buy", token_id="t", amount=10.0)
    q = _Query("ord_ok:x")
    await confirm._execute(_update(query=q), _ctx(db_user_id=uid), intent, query=q)
    # bot.error.generic mentions "wrong"
    assert q.edits and "wrong" in q.edits[-1][0].lower()


async def test_execute_pm_raises_logs_rejected_order_and_audit(monkeypatch):
    uid, acc_id = await _seed_user_account(tg_id=910)
    pm = _FakePM(raise_exc=RuntimeError("not enough balance to place this order"))
    _install_mgr(monkeypatch, pm, acc_id)
    intent = confirm.make_intent("limit", side="buy", token_id="0xT", price=0.5, size=4)
    q = _Query("ord_ok:x")
    await confirm._execute(_update(query=q), _ctx(db_user_id=uid), intent, query=q)

    # funding-looking detail → friendly bot.order.insufficient ("not enough balance")
    assert q.edits and "balance" in q.edits[-1][0].lower()
    async with async_session_scope() as s:
        order = await s.scalar(select(Order).where(Order.account_id == acc_id))
        assert order is not None and order.status == "rejected"
        assert order.error == "RuntimeError"
        # an ORDER_ERROR audit row was written
        ev = await s.scalar(select(AuditLog).where(AuditLog.event == "ORDER_ERROR"))
        assert ev is not None


async def test_execute_pm_raises_non_funding_uses_failed_reason(monkeypatch):
    uid, acc_id = await _seed_user_account(tg_id=911)
    pm = _FakePM(raise_exc=ValueError("market is paused"))
    _install_mgr(monkeypatch, pm, acc_id)
    intent = confirm.make_intent("limit", side="buy", token_id="0xT", price=0.5, size=4)
    q = _Query("ord_ok:x")
    await confirm._execute(_update(query=q), _ctx(db_user_id=uid), intent, query=q)
    # bot.order.failed_reason carries the actual reason text
    assert q.edits and "paused" in q.edits[-1][0].lower()


async def test_execute_result_success_false_insufficient(monkeypatch):
    uid, acc_id = await _seed_user_account(tg_id=912)
    pm = _FakePM({"success": False, "error": "insufficient balance"})
    _install_mgr(monkeypatch, pm, acc_id)
    intent = confirm.make_intent("market", side="buy", token_id="0xT", amount=10.0)
    q = _Query("ord_ok:x")
    await confirm._execute(_update(query=q), _ctx(db_user_id=uid), intent, query=q)
    # _fail with the insufficient key
    assert q.edits and "balance" in q.edits[-1][0].lower()
    async with async_session_scope() as s:
        order = await s.scalar(select(Order).where(Order.account_id == acc_id))
        assert order is not None and order.status == "rejected"


async def test_execute_news_success_records_bet_and_fulfills_intent(monkeypatch):
    uid, acc_id = await _seed_user_account(tg_id=913)
    async with async_session_scope() as s:
        row = await intents_repo.upsert_intent(s, user_id=uid, news_item_id=None,
                                                market_id="0xCOND", outcome="YES")
        pid = row.id
    pm = _FakePM({"orderID": "OID-NEWS", "status": "matched"})
    _install_mgr(monkeypatch, pm, acc_id)
    intent = confirm.make_intent("market", side="buy", token_id="tokYES", amount=20.0,
                                 max_price=0.74, neg_risk=False, source="news",
                                 market_id="0xCOND", outcome="YES", entry_price=0.70,
                                 title="Will it rain?", pending_intent_id=pid)
    q = _Query("ord_ok:x")
    await confirm._execute(_update(query=q), _ctx(db_user_id=uid), intent, query=q)

    assert pm.calls[0][0] == "place_capped_buy"
    async with async_session_scope() as s:
        bet = await s.scalar(select(Bet))
        assert bet is not None and bet.source == "news" and bet.outcome == "YES"
        # entry is the executed FOK ceiling (max_price=0.74), not the stale 0.70
        # quote — so the derived shares match what the order was actually sized for.
        assert bet.clob_order_id == "OID-NEWS" and float(bet.entry_price) == 0.74
        assert (await s.get(PendingIntent, pid)).status == "fulfilled"


# ── pure helpers ────────────────────────────────────────────────────────────────

def test_fail_key_funding_vs_generic():
    assert confirm._fail_key("insufficient funds for order") == "bot.order.insufficient"
    assert confirm._fail_key("allowance too low") == "bot.order.insufficient"
    assert confirm._fail_key("not enough balance") == "bot.order.insufficient"
    assert confirm._fail_key("market paused") == "bot.order.failed"
    assert confirm._fail_key("") == "bot.order.failed"
    assert confirm._fail_key(None) == "bot.order.failed"


def test_exc_detail_dict_str_and_plain():
    # error_msg dict → prefers the 'error' member
    e_dict = Exception()
    e_dict.error_msg = {"error": "not enough balance", "code": 7}
    assert confirm._exc_detail(e_dict) == "not enough balance"
    # error_msg dict without 'error'/'message'/'errorMsg' → str(dict)
    e_dict2 = Exception()
    e_dict2.error_msg = {"code": 7}
    assert "code" in confirm._exc_detail(e_dict2)
    # error_msg as a plain string
    e_str = Exception()
    e_str.error_msg = "boom from api"
    assert confirm._exc_detail(e_str) == "boom from api"
    # no error_msg attr → falls back to str(exc)
    assert confirm._exc_detail(RuntimeError("plain reason")) == "plain reason"


def test_result_detail_variants():
    assert confirm._result_detail({"error": "bad"}) == "bad"
    assert confirm._result_detail({"errorMsg": "worse"}) == "worse"
    assert confirm._result_detail({"message": "info"}) == "info"
    assert confirm._result_detail({"nothing": 1}) == ""
    assert confirm._result_detail("not-a-dict") == ""


def test_notional_usd_per_kind():
    # market buy = amount (USD)
    assert confirm._notional_usd({"kind": "market", "side": "buy", "amount": 25.0}) == 25.0
    # market sell = 0 (amount is a share count, no USD notional)
    assert confirm._notional_usd({"kind": "market", "side": "sell", "amount": 25.0}) == 0.0
    # limit = price * size
    assert confirm._notional_usd({"kind": "limit", "price": 0.5, "size": 10}) == 5.0
    # close = 0
    assert confirm._notional_usd({"kind": "close", "size": 7.0}) == 0.0
