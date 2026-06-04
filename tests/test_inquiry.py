"""Inquiry handlers — pure formatting helpers + read-only monitoring/market
commands + the inq:/ocancel callbacks. Telegram + AccountManager are faked;
no network, no DB needed (handlers reach the manager via common.manager)."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from bot.handlers import inquiry
from polymarket.credentials import NoAccountConnected, TradingUnavailable


# ── Telegram + manager fakes ─────────────────────────────────────────────────

class _RecMsg:
    """Records reply_text(text, **kw) calls (the screen/reply sink)."""
    def __init__(self):
        self.sent = []

    async def reply_text(self, text, **kw):
        self.sent.append((text, kw))


class _Query:
    def __init__(self, data):
        self.data = data
        self.message = None  # not a telegram.Message → screen falls through to reply

    async def answer(self, *a, **k):
        pass

    async def edit_message_text(self, *a, **k):
        pass


def _cmd_update(msg=None):
    """A command-style update (no callback_query)."""
    return SimpleNamespace(callback_query=None, effective_message=msg or _RecMsg(),
                           effective_user=SimpleNamespace(id=111), effective_chat=None,
                           message=SimpleNamespace(text="/cmd"))


def _cb_update(data, msg=None):
    return SimpleNamespace(callback_query=_Query(data), effective_message=msg or _RecMsg(),
                          effective_user=SimpleNamespace(id=111), effective_chat=None)


def _ctx(**ud):
    ud.setdefault("lang", "en")
    ud.setdefault("db_user_id", 7)
    return SimpleNamespace(user_data=ud, bot=None, application=SimpleNamespace(bot_data={}))


class _FakePM:
    """A fake Polymarket exposing SYNC data methods (handlers call via to_thread)."""
    def __init__(self, **methods):
        self._m = methods

    def __getattr__(self, name):
        fn = self._m.get(name)
        if fn is None:
            raise AttributeError(name)
        return fn


class _FakeMgr:
    """Returns the same fake PM for both readonly + trading clients."""
    def __init__(self, pm=None, *, raise_readonly=None, raise_trading=None):
        self._pm = pm
        self._raise_readonly = raise_readonly
        self._raise_trading = raise_trading

    async def get_readonly_client(self, uid):
        if self._raise_readonly is not None:
            raise self._raise_readonly
        return self._pm

    async def get_trading_client(self, uid, account_id=None):
        if self._raise_trading is not None:
            raise self._raise_trading
        return self._pm


def _install_mgr(monkeypatch, pm=None, **kw):
    mgr = _FakeMgr(pm, **kw)
    monkeypatch.setattr(inquiry.common, "manager", lambda ctx: mgr)
    return mgr


# ── _as_float ─────────────────────────────────────────────────────────────────

def test_as_float_none_empty_and_bad_return_default():
    assert inquiry._as_float(None) == 0.0
    assert inquiry._as_float("") == 0.0
    assert inquiry._as_float("not-a-number") == 0.0
    assert inquiry._as_float(object()) == 0.0  # TypeError path
    assert inquiry._as_float(None, default=3.5) == 3.5


def test_as_float_numeric_str_and_number():
    assert inquiry._as_float("12.5") == 12.5
    assert inquiry._as_float(7) == 7.0
    assert inquiry._as_float(0) == 0.0  # 0 is not "" → real zero


# ── _fmt_money / _fmt_price ─────────────────────────────────────────────────

def test_fmt_money_thousands_and_decimals():
    assert inquiry._fmt_money(1234.5) == "1,234.50"
    assert inquiry._fmt_money(None) == "0.00"
    assert inquiry._fmt_money(1000, decimals=0) == "1,000"


def test_fmt_price_default_four_decimals():
    assert inquiry._fmt_price(0.5) == "0.5000"
    assert inquiry._fmt_price(None) == "0.0000"
    assert inquiry._fmt_price(0.5, decimals=2) == "0.50"


# ── _pnl_emoji ────────────────────────────────────────────────────────────────

def test_pnl_emoji_sign():
    assert inquiry._pnl_emoji(1) == "🟢"
    assert inquiry._pnl_emoji(0) == "🟢"  # >= 0 is green
    assert inquiry._pnl_emoji(-0.01) == "🔴"
    assert inquiry._pnl_emoji(None) == "🟢"  # default 0.0 → green


# ── _fmt_ts ────────────────────────────────────────────────────────────────────

def test_fmt_ts_seconds_and_ms_and_guards():
    # known second-precision timestamp → 2021-01-01 00:00 UTC
    assert inquiry._fmt_ts(1609459200) == "2021-01-01 00:00"
    # same instant in milliseconds (>1e12 → divided by 1000) yields identical text
    assert inquiry._fmt_ts(1609459200000) == "2021-01-01 00:00"
    assert inquiry._fmt_ts(0) == "?"
    assert inquiry._fmt_ts(-5) == "?"
    assert inquiry._fmt_ts(None) == "?"
    # absurdly large (well past year 9999) → OverflowError/OSError/ValueError → "?"
    assert inquiry._fmt_ts(1e30) == "?"


# ── _shorten ─────────────────────────────────────────────────────────────────

def test_shorten_truncates_with_ellipsis():
    assert inquiry._shorten("0123456789ABCDEFGHIJ", head=4) == "0123…"
    assert inquiry._shorten("short", head=16) == "short"  # under limit → unchanged
    assert inquiry._shorten(None) == "?"  # None → "?" (default placeholder)
    assert inquiry._shorten("", head=4) == "?"  # falsy → "?"


# ── _as_rows ─────────────────────────────────────────────────────────────────

def test_as_rows_wrapped_keys_bare_list_and_else():
    for key in ("data", "positions", "trades", "activity", "history"):
        assert inquiry._as_rows({key: [{"x": 1}]}) == [{"x": 1}]
    assert inquiry._as_rows([{"a": 1}]) == [{"a": 1}]   # bare list passes through
    assert inquiry._as_rows({"nope": [1, 2]}) == []     # dict with no known key
    assert inquiry._as_rows("string") == []             # non-list, non-dict
    assert inquiry._as_rows({"data": "notalist"}) == []  # wrapped value not a list


# ── _act_emoji ────────────────────────────────────────────────────────────────

def test_act_emoji_known_and_unknown():
    assert inquiry._act_emoji("TRADE") == "🔁"
    assert inquiry._act_emoji("buy") == "🟢"   # case-insensitive
    assert inquiry._act_emoji("REDEEM") == "🪙"
    assert inquiry._act_emoji("WHATEVER") == "•"  # unknown → bullet
    assert inquiry._act_emoji(None) == "•"


# ── _rel_ts ──────────────────────────────────────────────────────────────────

def test_rel_ts_buckets_and_guards():
    now = datetime.now(timezone.utc).timestamp()
    assert inquiry._rel_ts(now) == "just now"
    assert inquiry._rel_ts(now - 120) == "2m ago"
    assert inquiry._rel_ts(now - 3 * 3600) == "3h ago"
    assert inquiry._rel_ts(now - 2 * 86400) == "2d ago"
    # > 7 days ago → falls back to an absolute YYYY-MM-DD date
    old = inquiry._rel_ts(now - 30 * 86400)
    assert len(old) == 10 and old[4] == "-" and old[7] == "-"
    # ms input is divided by 1000 → still "just now"
    assert inquiry._rel_ts(now * 1000) == "just now"
    # guards
    assert inquiry._rel_ts(0) == "?"
    assert inquiry._rel_ts(-1) == "?"
    assert inquiry._rel_ts(1e30) == "?"  # overflow → "?"


# ── _parse_atomic_usdc ──────────────────────────────────────────────────────

def test_parse_atomic_usdc_divides_when_atomic():
    assert inquiry._parse_atomic_usdc(2_000_000) == 2.0   # >1e6 → /1e6
    assert inquiry._parse_atomic_usdc(500_000) == 500_000  # <=1e6 → passthrough
    assert inquiry._parse_atomic_usdc("3000000") == 3.0
    assert inquiry._parse_atomic_usdc(None) == 0.0


# ── /portfolio ─────────────────────────────────────────────────────────────

async def test_portfolio_renders_screen(monkeypatch):
    pm = _FakePM(
        get_portfolio_value=lambda: {"cash": 100.0, "positionsValue": 50.0,
                                     "value": 150.0, "pnl": 5.0, "pnlPercent": 3.3},
        get_positions=lambda limit, off: [{"currentValue": 50.0, "cashPnl": 5.0}],
    )
    _install_mgr(monkeypatch, pm)
    upd = _cmd_update()
    await inquiry.portfolio(upd, _ctx())
    assert upd.effective_message.sent  # screen sent a message
    text, kw = upd.effective_message.sent[0]
    assert "100.00" in text and "50.00" in text and "150.00" in text
    assert kw["parse_mode"] == "Markdown"
    assert kw["reply_markup"] is not None  # nav keyboard present


async def test_portfolio_derives_totals_from_rows(monkeypatch):
    # value dict has no totals → derive positions_value/total/pnl from rows
    pm = _FakePM(
        get_portfolio_value=lambda: {},
        get_positions=lambda limit, off: [{"currentValue": 20.0, "cashPnl": -2.0},
                                          {"currentValue": 30.0, "cashPnl": 1.0}],
    )
    _install_mgr(monkeypatch, pm)
    upd = _cmd_update()
    await inquiry.portfolio(upd, _ctx())
    text, _ = upd.effective_message.sent[0]
    assert "50.00" in text  # positions_value summed (20 + 30)


async def test_portfolio_no_account(monkeypatch):
    _install_mgr(monkeypatch, raise_readonly=NoAccountConnected(7))
    upd = _cmd_update()
    await inquiry.portfolio(upd, _ctx())
    text, kw = upd.effective_message.sent[0]
    # _no_account → no_account reply with a connect keyboard
    assert "Connect" in text or "connect" in text
    datas = [b.callback_data for row in kw["reply_markup"].inline_keyboard for b in row]
    assert "menu:connect" in datas


async def test_portfolio_trading_unavailable(monkeypatch):
    _install_mgr(monkeypatch, raise_readonly=TradingUnavailable(7))
    upd = _cmd_update()
    await inquiry.portfolio(upd, _ctx())
    assert upd.effective_message.sent  # trading_unavailable reply


async def test_portfolio_generic_error(monkeypatch):
    _install_mgr(monkeypatch, raise_readonly=RuntimeError("boom"))
    upd = _cmd_update()
    await inquiry.portfolio(upd, _ctx())
    text, _ = upd.effective_message.sent[0]
    assert "wrong" in text.lower()  # bot.error.generic


# ── /positions ─────────────────────────────────────────────────────────────

async def test_positions_with_rows(monkeypatch):
    pm = _FakePM(get_positions=lambda limit, off: [
        {"title": "Will it rain?", "outcome": "Yes", "size": 10, "avgPrice": 0.5,
         "currentValue": 6.0, "cashPnl": 1.0, "percentPnl": 20.0},
    ])
    _install_mgr(monkeypatch, pm)
    upd = _cmd_update()
    await inquiry.positions(upd, _ctx())
    text, kw = upd.effective_message.sent[0]
    assert "Will it rain?" in text and "Yes" in text
    assert kw["reply_markup"] is not None


async def test_positions_empty(monkeypatch):
    pm = _FakePM(get_positions=lambda limit, off: [])
    _install_mgr(monkeypatch, pm)
    upd = _cmd_update()
    await inquiry.positions(upd, _ctx())
    text, _ = upd.effective_message.sent[0]
    assert "no open positions" in text.lower()


async def test_positions_no_account(monkeypatch):
    _install_mgr(monkeypatch, raise_readonly=NoAccountConnected(7))
    upd = _cmd_update()
    await inquiry.positions(upd, _ctx())
    datas = [b.callback_data for row in upd.effective_message.sent[0][1]["reply_markup"].inline_keyboard for b in row]
    assert "menu:connect" in datas


async def test_positions_generic_error(monkeypatch):
    pm = _FakePM(get_positions=lambda limit, off: (_ for _ in ()).throw(ValueError("x")))
    _install_mgr(monkeypatch, pm)
    upd = _cmd_update()
    await inquiry.positions(upd, _ctx())
    assert "wrong" in upd.effective_message.sent[0][0].lower()


# ── /balance ─────────────────────────────────────────────────────────────────

async def test_balance_parses_atomic(monkeypatch):
    # atomic USDC (6 decimals) → 12.34
    pm = _FakePM(get_balance=lambda: {"balance": 12_340_000})
    _install_mgr(monkeypatch, pm)
    upd = _cmd_update()
    await inquiry.balance(upd, _ctx())
    text, kw = upd.effective_message.sent[0]
    assert "12.34" in text
    assert kw["parse_mode"] == "Markdown"


async def test_balance_bare_value(monkeypatch):
    pm = _FakePM(get_balance=lambda: 250.0)  # not a dict → used directly
    _install_mgr(monkeypatch, pm)
    upd = _cmd_update()
    await inquiry.balance(upd, _ctx())
    assert "250.00" in upd.effective_message.sent[0][0]


async def test_balance_no_account(monkeypatch):
    _install_mgr(monkeypatch, raise_trading=NoAccountConnected(7))
    upd = _cmd_update()
    await inquiry.balance(upd, _ctx())
    datas = [b.callback_data for row in upd.effective_message.sent[0][1]["reply_markup"].inline_keyboard for b in row]
    assert "menu:connect" in datas


async def test_balance_trading_unavailable(monkeypatch):
    _install_mgr(monkeypatch, raise_trading=TradingUnavailable(7))
    upd = _cmd_update()
    await inquiry.balance(upd, _ctx())
    assert upd.effective_message.sent


async def test_balance_generic_error(monkeypatch):
    pm = _FakePM(get_balance=lambda: (_ for _ in ()).throw(RuntimeError("x")))
    _install_mgr(monkeypatch, pm)
    upd = _cmd_update()
    await inquiry.balance(upd, _ctx())
    assert "wrong" in upd.effective_message.sent[0][0].lower()


# ── /orders ─────────────────────────────────────────────────────────────────

async def test_orders_with_rows_stashes_and_buttons(monkeypatch):
    pm = _FakePM(get_open_orders=lambda: [
        {"id": "ORDER-AAAAAAAAAAAAAAAA", "side": "buy", "price": 0.5,
         "original_size": 10, "asset_id": "token-1234567890"},
        {"id": "ORDER-BBBB", "side": "sell", "price": 0.6, "size": 5, "market": "mkt"},
    ])
    _install_mgr(monkeypatch, pm)
    ctx = _ctx()
    upd = _cmd_update()
    await inquiry.orders(upd, ctx)
    text, kw = upd.effective_message.sent[0]
    # stash recorded under open_orders, index→order_id
    assert ctx.user_data["open_orders"] == {"0": "ORDER-AAAAAAAAAAAAAAAA", "1": "ORDER-BBBB"}
    datas = [b.callback_data for row in kw["reply_markup"].inline_keyboard for b in row]
    assert "ocancel:0" in datas and "ocancel:1" in datas and "ocancelall" in datas


async def test_orders_truncation_note(monkeypatch):
    # more than _MAX_ROWS rows → a "showing N of total" note is appended
    big = [{"id": f"O{i}", "side": "buy", "price": 0.5, "size": 1, "asset_id": "t"}
           for i in range(inquiry._MAX_ROWS + 3)]
    pm = _FakePM(get_open_orders=lambda: big)
    _install_mgr(monkeypatch, pm)
    ctx = _ctx()
    upd = _cmd_update()
    await inquiry.orders(upd, ctx)
    text, _ = upd.effective_message.sent[0]
    assert str(inquiry._MAX_ROWS) in text and str(len(big)) in text
    # only _MAX_ROWS order ids stashed
    assert len(ctx.user_data["open_orders"]) == inquiry._MAX_ROWS


async def test_orders_empty(monkeypatch):
    pm = _FakePM(get_open_orders=lambda: [])
    _install_mgr(monkeypatch, pm)
    upd = _cmd_update()
    await inquiry.orders(upd, _ctx())
    assert "no open orders" in upd.effective_message.sent[0][0].lower()


async def test_orders_no_account(monkeypatch):
    _install_mgr(monkeypatch, raise_trading=NoAccountConnected(7))
    upd = _cmd_update()
    await inquiry.orders(upd, _ctx())
    datas = [b.callback_data for row in upd.effective_message.sent[0][1]["reply_markup"].inline_keyboard for b in row]
    assert "menu:connect" in datas


async def test_orders_trading_unavailable(monkeypatch):
    _install_mgr(monkeypatch, raise_trading=TradingUnavailable(7))
    upd = _cmd_update()
    await inquiry.orders(upd, _ctx())
    assert upd.effective_message.sent


async def test_orders_generic_error(monkeypatch):
    pm = _FakePM(get_open_orders=lambda: (_ for _ in ()).throw(RuntimeError("x")))
    _install_mgr(monkeypatch, pm)
    upd = _cmd_update()
    await inquiry.orders(upd, _ctx())
    assert "wrong" in upd.effective_message.sent[0][0].lower()


# ── /trades ─────────────────────────────────────────────────────────────────

async def test_trades_with_rows(monkeypatch):
    pm = _FakePM(get_trades=lambda limit, off: [
        {"title": "Match A", "side": "buy", "outcome": "Yes", "price": 0.4, "size": 10,
         "timestamp": datetime.now(timezone.utc).timestamp()},
    ])
    _install_mgr(monkeypatch, pm)
    upd = _cmd_update()
    await inquiry.trades(upd, _ctx())
    text, kw = upd.effective_message.sent[0]
    assert "Match A" in text and kw["reply_markup"] is not None


async def test_trades_empty(monkeypatch):
    pm = _FakePM(get_trades=lambda limit, off: [])
    _install_mgr(monkeypatch, pm)
    upd = _cmd_update()
    await inquiry.trades(upd, _ctx())
    assert "no trades" in upd.effective_message.sent[0][0].lower()


async def test_trades_no_account(monkeypatch):
    _install_mgr(monkeypatch, raise_readonly=NoAccountConnected(7))
    upd = _cmd_update()
    await inquiry.trades(upd, _ctx())
    datas = [b.callback_data for row in upd.effective_message.sent[0][1]["reply_markup"].inline_keyboard for b in row]
    assert "menu:connect" in datas


async def test_trades_generic_error(monkeypatch):
    pm = _FakePM(get_trades=lambda limit, off: (_ for _ in ()).throw(RuntimeError("x")))
    _install_mgr(monkeypatch, pm)
    upd = _cmd_update()
    await inquiry.trades(upd, _ctx())
    assert "wrong" in upd.effective_message.sent[0][0].lower()


# ── /activity ────────────────────────────────────────────────────────────────

async def test_activity_with_rows(monkeypatch):
    pm = _FakePM(get_activity=lambda limit: [
        {"type": "REDEEM", "title": "Resolved Mkt", "usdcSize": 12.0,
         "timestamp": datetime.now(timezone.utc).timestamp()},
    ])
    _install_mgr(monkeypatch, pm)
    upd = _cmd_update()
    await inquiry.activity(upd, _ctx())
    text, kw = upd.effective_message.sent[0]
    assert "REDEEM" in text and "Resolved Mkt" in text and kw["reply_markup"] is not None


async def test_activity_empty(monkeypatch):
    pm = _FakePM(get_activity=lambda limit: [])
    _install_mgr(monkeypatch, pm)
    upd = _cmd_update()
    await inquiry.activity(upd, _ctx())
    assert "no recent activity" in upd.effective_message.sent[0][0].lower()


async def test_activity_no_account(monkeypatch):
    _install_mgr(monkeypatch, raise_readonly=NoAccountConnected(7))
    upd = _cmd_update()
    await inquiry.activity(upd, _ctx())
    datas = [b.callback_data for row in upd.effective_message.sent[0][1]["reply_markup"].inline_keyboard for b in row]
    assert "menu:connect" in datas


async def test_activity_generic_error(monkeypatch):
    pm = _FakePM(get_activity=lambda limit: (_ for _ in ()).throw(RuntimeError("x")))
    _install_mgr(monkeypatch, pm)
    upd = _cmd_update()
    await inquiry.activity(upd, _ctx())
    assert "wrong" in upd.effective_message.sent[0][0].lower()


# ── /price ─────────────────────────────────────────────────────────────────

async def test_price_usage_without_args(monkeypatch):
    # no manager call expected; just the usage prompt
    upd = _cmd_update()
    ctx = _ctx()
    ctx.args = []  # SimpleNamespace lacks .args by default
    await inquiry.price(upd, ctx)
    assert "Usage" in upd.effective_message.sent[0][0] and "/price" in upd.effective_message.sent[0][0]


async def test_price_with_token_renders_detail(monkeypatch):
    pm = _FakePM(
        get_price=lambda token, side: {"price": 0.61 if side == "buy" else 0.59},
        get_midpoint=lambda token: {"mid": 0.60},
        get_spread=lambda token: {"spread": 0.02},
    )
    _install_mgr(monkeypatch, pm)
    upd = _cmd_update()
    ctx = _ctx()
    ctx.args = ["tok-1234"]
    await inquiry.price(upd, ctx)
    text, kw = upd.effective_message.sent[0]
    assert "0.6100" in text and "0.5900" in text and "0.6000" in text and "0.0200" in text
    assert kw["parse_mode"] == "Markdown"


async def test_price_no_account(monkeypatch):
    _install_mgr(monkeypatch, raise_readonly=NoAccountConnected(7))
    upd = _cmd_update()
    ctx = _ctx()
    ctx.args = ["tok"]
    await inquiry.price(upd, ctx)
    datas = [b.callback_data for row in upd.effective_message.sent[0][1]["reply_markup"].inline_keyboard for b in row]
    assert "menu:connect" in datas


async def test_price_error_shows_not_found(monkeypatch):
    pm = _FakePM(
        get_price=lambda token, side: (_ for _ in ()).throw(RuntimeError("x")),
        get_midpoint=lambda token: {"mid": 0.5},
        get_spread=lambda token: {"spread": 0.0},
    )
    _install_mgr(monkeypatch, pm)
    upd = _cmd_update()
    ctx = _ctx()
    ctx.args = ["tok"]
    await inquiry.price(upd, ctx)
    assert "not found" in upd.effective_message.sent[0][0].lower()


# ── /book ─────────────────────────────────────────────────────────────────────

async def test_book_usage_without_args():
    upd = _cmd_update()
    ctx = _ctx()
    ctx.args = []
    await inquiry.book(upd, ctx)
    assert "Usage" in upd.effective_message.sent[0][0] and "/book" in upd.effective_message.sent[0][0]


async def test_book_with_ladder(monkeypatch):
    pm = _FakePM(get_orderbook=lambda token: {
        "bids": [{"price": 0.50, "size": 100}, {"price": 0.49, "size": 200}],
        "asks": [{"price": 0.52, "size": 150}, {"price": 0.53, "size": 80}],
    })
    _install_mgr(monkeypatch, pm)
    upd = _cmd_update()
    ctx = _ctx()
    ctx.args = ["tok-XYZ"]
    await inquiry.book(upd, ctx)
    text, kw = upd.effective_message.sent[0]
    assert "0.5000" in text and "0.5200" in text  # ladder rendered both sides
    assert "<pre>" in text and kw["reply_markup"] is not None


async def test_book_empty_book(monkeypatch):
    pm = _FakePM(get_orderbook=lambda token: {"bids": [], "asks": []})
    _install_mgr(monkeypatch, pm)
    upd = _cmd_update()
    ctx = _ctx()
    ctx.args = ["tok"]
    await inquiry.book(upd, ctx)
    assert "no order book" in upd.effective_message.sent[0][0].lower()


async def test_book_no_account(monkeypatch):
    _install_mgr(monkeypatch, raise_readonly=NoAccountConnected(7))
    upd = _cmd_update()
    ctx = _ctx()
    ctx.args = ["tok"]
    await inquiry.book(upd, ctx)
    datas = [b.callback_data for row in upd.effective_message.sent[0][1]["reply_markup"].inline_keyboard for b in row]
    assert "menu:connect" in datas


async def test_book_error_shows_not_found(monkeypatch):
    pm = _FakePM(get_orderbook=lambda token: (_ for _ in ()).throw(RuntimeError("x")))
    _install_mgr(monkeypatch, pm)
    upd = _cmd_update()
    ctx = _ctx()
    ctx.args = ["tok"]
    await inquiry.book(upd, ctx)
    assert "not found" in upd.effective_message.sent[0][0].lower()


# ── on_inq dispatch ──────────────────────────────────────────────────────────

async def test_on_inq_dispatches_to_positions(monkeypatch):
    seen = {}

    async def fake_positions(update, context):
        seen["hit"] = True

    monkeypatch.setattr(inquiry, "positions", fake_positions)
    await inquiry.on_inq(_cb_update("inq:positions"), _ctx())
    assert seen.get("hit") is True


async def test_on_inq_unknown_action_is_noop(monkeypatch):
    # unknown action → fn is None → nothing dispatched
    upd = _cb_update("inq:bogus")
    await inquiry.on_inq(upd, _ctx())
    assert upd.effective_message.sent == []


async def test_on_inq_no_query_returns():
    # callback_query None → early return, no crash
    await inquiry.on_inq(_cmd_update(), _ctx())


# ── on_order_cancel ──────────────────────────────────────────────────────────

async def test_on_order_cancel_all_requests_confirm(monkeypatch):
    captured = {}

    async def fake_request(update, context, intent, key, **vars):
        captured["intent"] = intent
        captured["key"] = key

    monkeypatch.setattr(inquiry.confirm, "request", fake_request)
    await inquiry.on_order_cancel(_cb_update("ocancelall"), _ctx())
    assert captured["intent"]["kind"] == "cancel_all"
    assert captured["key"] == "bot.confirm.cancel_all"


async def test_on_order_cancel_one_with_stash(monkeypatch):
    captured = {}

    async def fake_request(update, context, intent, key, **vars):
        captured["intent"] = intent
        captured["key"] = key

    monkeypatch.setattr(inquiry.confirm, "request", fake_request)
    ctx = _ctx(open_orders={"0": "ORDER-XYZ"})
    await inquiry.on_order_cancel(_cb_update("ocancel:0"), ctx)
    assert captured["intent"]["kind"] == "cancel"
    assert captured["intent"]["order_id"] == "ORDER-XYZ"
    assert captured["key"] == "bot.confirm.cancel"


async def test_on_order_cancel_expired_when_no_stash(monkeypatch):
    called = {}
    monkeypatch.setattr(inquiry.confirm, "request",
                        lambda *a, **k: called.setdefault("hit", True))
    upd = _cb_update("ocancel:9")  # nothing stashed at idx 9
    await inquiry.on_order_cancel(upd, _ctx())
    assert "hit" not in called
    assert "expired" in upd.effective_message.sent[0][0].lower()


async def test_on_order_cancel_no_query_returns():
    await inquiry.on_order_cancel(_cmd_update(), _ctx())  # early return, no crash


# ── register ─────────────────────────────────────────────────────────────────

def test_register_adds_all_handlers():
    added = []

    class _App:
        def add_handler(self, h):
            added.append(h)

    inquiry.register(_App())
    # 3 callback handlers + 8 command handlers = 11
    assert len(added) == 11
    # command names registered
    from telegram.ext import CommandHandler
    cmds = set()
    for h in added:
        if isinstance(h, CommandHandler):
            cmds |= set(h.commands)
    assert {"portfolio", "positions", "balance", "orders", "trades",
            "activity", "price", "book"} <= cmds
