"""Command-handler tests for bot/handlers/trading.py.

Covers /buy /sell /marketbuy /marketsell /close /cancel /cancelall + register():
usage/guard branches (no confirm.request), bad-number/bad-value branches, and
the happy paths that build the correct intent and call confirm.request once with
the right i18n key. confirm.request is replaced by a recorder so nothing executes.
The pure helpers (_floats, _to_float, _position_row, make_intent, _result_*) are
already covered by tests/test_trading_logic.py and are NOT duplicated here.
"""

from types import SimpleNamespace

import pytest

from bot.handlers import trading
from polymarket.credentials import NoAccountConnected


# ── Telegram fakes ──────────────────────────────────────────────────────────────

class _RecMsg:
    def __init__(self):
        self.sent = []  # list of (text, kwargs)

    async def reply_text(self, text, **kw):
        self.sent.append((text, kw))


def _update(msg=None):
    # command-style update (no callback_query); effective_chat=None so common.typing
    # is a no-op and common.reply routes through effective_message.reply_text.
    return SimpleNamespace(callback_query=None, effective_message=msg or _RecMsg(),
                           effective_user=SimpleNamespace(id=111), effective_chat=None)


def _ctx(args=None, **user_data):
    user_data.setdefault("lang", "en")
    user_data.setdefault("db_user_id", 7)
    return SimpleNamespace(user_data=user_data, args=(args or []), bot=None,
                           application=SimpleNamespace(bot_data={}))


@pytest.fixture
def rec(monkeypatch):
    """Recorder swapped in for confirm.request: captures (intent, key, vars)."""
    calls = []

    async def fake_request(update, context, intent, confirm_key, **text_vars):
        calls.append({"intent": intent, "key": confirm_key, "vars": text_vars})

    monkeypatch.setattr(trading.confirm, "request", fake_request)
    return calls


# ── /buy and /sell (limit) ──────────────────────────────────────────────────────

async def test_buy_too_few_args_shows_usage_no_confirm(rec):
    msg = _RecMsg()
    await trading.buy(_update(msg), _ctx(["0xtok", "0.5"]))  # missing size
    assert rec == []                                          # no order
    assert msg.sent and "Usage: /buy" in msg.sent[0][0]
    assert msg.sent[0][1].get("reply_markup") is not None     # browse keyboard


async def test_buy_no_args_shows_usage(rec):
    msg = _RecMsg()
    await trading.buy(_update(msg), _ctx([]))
    assert rec == []
    assert "Usage: /buy" in msg.sent[0][0]


async def test_buy_non_numeric_args_replies_bad_number(rec):
    msg = _RecMsg()
    await trading.buy(_update(msg), _ctx(["0xtok", "notnum", "10"]))
    assert rec == []
    assert "valid numbers" in msg.sent[0][0]
    assert msg.sent[0][1].get("reply_markup") is not None     # browse keyboard on bad_number


async def test_buy_price_out_of_range_replies_bad_price(rec):
    msg = _RecMsg()
    await trading.buy(_update(msg), _ctx(["0xtok", "1.5", "10"]))  # price not in (0,1)
    assert rec == []
    assert "Price must be between 0 and 1" in msg.sent[0][0]
    # bad_price has no reply_markup (handler passes none)
    assert msg.sent[0][1].get("reply_markup") is None


async def test_buy_zero_price_rejected(rec):
    await trading.buy(_update(), _ctx(["0xtok", "0", "10"]))
    assert rec == []


async def test_buy_nonpositive_size_replies_bad_size(rec):
    msg = _RecMsg()
    await trading.buy(_update(msg), _ctx(["0xtok", "0.5", "0"]))  # size <= 0
    assert rec == []
    assert "Size must be" in msg.sent[0][0]


async def test_buy_valid_builds_limit_buy_intent(rec):
    await trading.buy(_update(), _ctx(["0xTOKEN", "0.62", "10"]))
    assert len(rec) == 1
    c = rec[0]
    assert c["intent"]["kind"] == "limit"
    assert c["intent"]["side"] == "buy"
    assert c["intent"]["token_id"] == "0xTOKEN"
    assert c["intent"]["price"] == 0.62
    assert c["intent"]["size"] == 10.0
    assert c["key"] == "bot.confirm.buy"
    # text vars carry the numbers + shortened token
    assert c["vars"]["price"] == 0.62 and c["vars"]["size"] == 10.0
    assert c["vars"]["token"] == trading.common.short("0xTOKEN")


async def test_sell_valid_builds_limit_sell_intent(rec):
    await trading.sell(_update(), _ctx(["0xT", "0.40", "5"]))
    assert len(rec) == 1
    assert rec[0]["intent"]["kind"] == "limit"
    assert rec[0]["intent"]["side"] == "sell"
    assert rec[0]["intent"]["price"] == 0.40 and rec[0]["intent"]["size"] == 5.0
    assert rec[0]["key"] == "bot.confirm.sell"


async def test_sell_too_few_args_uses_sell_usage(rec):
    msg = _RecMsg()
    await trading.sell(_update(msg), _ctx(["0xT"]))
    assert rec == []
    assert "Usage: /sell" in msg.sent[0][0]


async def test_limit_uses_last_two_args_for_numbers(rec):
    # _floats takes the trailing n args, so extra leading tokens are ignored as price/size.
    await trading.buy(_update(), _ctx(["0xT", "junk", "0.5", "10"]))
    assert len(rec) == 1
    assert rec[0]["intent"]["price"] == 0.5 and rec[0]["intent"]["size"] == 10.0


# ── /marketbuy (market buy in USD) ───────────────────────────────────────────────

async def test_marketbuy_too_few_args_shows_usage(rec):
    msg = _RecMsg()
    await trading.marketbuy(_update(msg), _ctx(["0xT"]))  # no usd
    assert rec == []
    assert "Usage: /marketbuy" in msg.sent[0][0]
    assert msg.sent[0][1].get("reply_markup") is not None


async def test_marketbuy_non_numeric_replies_bad_number(rec):
    msg = _RecMsg()
    await trading.marketbuy(_update(msg), _ctx(["0xT", "lots"]))
    assert rec == []
    assert "valid numbers" in msg.sent[0][0]


async def test_marketbuy_nonpositive_amount_replies_bad_amount(rec):
    msg = _RecMsg()
    await trading.marketbuy(_update(msg), _ctx(["0xT", "0"]))
    assert rec == []
    assert "Amount must be" in msg.sent[0][0]


async def test_marketbuy_valid_builds_market_buy_intent(rec):
    await trading.marketbuy(_update(), _ctx(["0xABC", "25"]))
    assert len(rec) == 1
    c = rec[0]
    assert c["intent"]["kind"] == "market"
    assert c["intent"]["side"] == "buy"
    assert c["intent"]["token_id"] == "0xABC"
    assert c["intent"]["amount"] == 25.0
    assert "price" not in c["intent"] and "size" not in c["intent"]
    assert c["key"] == "bot.confirm.marketbuy"
    assert c["vars"]["amount"] == 25.0
    assert c["vars"]["token"] == trading.common.short("0xABC")


async def test_marketbuy_over_cap_replies_bad_amount(rec):
    # A BUY's amount is USD — an oversized one is rejected before building an intent.
    msg = _RecMsg()
    await trading.marketbuy(_update(msg), _ctx(["0xT", "5000"]))
    assert rec == []
    assert "Amount must be" in msg.sent[0][0]


async def test_marketsell_large_share_count_allowed(rec):
    # A SELL's amount is a SHARE count, not USD — the USD cap must NOT apply.
    await trading.marketsell(_update(), _ctx(["0xDEF", "5000"]))
    assert len(rec) == 1 and rec[0]["intent"]["side"] == "sell"
    assert rec[0]["intent"]["amount"] == 5000.0


# ── /marketsell (market sell in shares) ──────────────────────────────────────────

async def test_marketsell_no_args_routes_to_manage(rec, monkeypatch):
    # No args → /manage list (one-tap sell), NOT a usage error or a confirm.
    seen = {}

    async def fake_manage(update, context):
        seen["manage"] = True

    monkeypatch.setattr(trading.positions_ui, "manage", fake_manage)
    await trading.marketsell(_update(), _ctx([]))
    assert seen.get("manage") is True
    assert rec == []


async def test_marketsell_non_numeric_replies_bad_number(rec):
    msg = _RecMsg()
    await trading.marketsell(_update(msg), _ctx(["0xT", "abc"]))
    assert rec == []
    assert "valid numbers" in msg.sent[0][0]


async def test_marketsell_nonpositive_amount_rejected(rec):
    msg = _RecMsg()
    await trading.marketsell(_update(msg), _ctx(["0xT", "-3"]))
    assert rec == []
    assert "Amount must be" in msg.sent[0][0]


async def test_marketsell_valid_builds_market_sell_intent(rec):
    await trading.marketsell(_update(), _ctx(["0xDEF", "12"]))
    assert len(rec) == 1
    c = rec[0]
    assert c["intent"]["kind"] == "market"
    assert c["intent"]["side"] == "sell"
    assert c["intent"]["token_id"] == "0xDEF"
    assert c["intent"]["amount"] == 12.0
    assert c["key"] == "bot.confirm.marketsell"


# ── /close (market-sell full position) ───────────────────────────────────────────

class _FakeMgr:
    def __init__(self, positions=None, raise_exc=None):
        self._positions = positions or []
        self._raise = raise_exc

    async def get_readonly_client(self, uid):
        if self._raise is not None:
            raise self._raise
        return SimpleNamespace(get_positions=lambda: self._positions)


async def test_close_no_args_routes_to_manage(rec, monkeypatch):
    seen = {}

    async def fake_manage(update, context):
        seen["manage"] = True

    monkeypatch.setattr(trading.positions_ui, "manage", fake_manage)
    await trading.close(_update(), _ctx([]))
    assert seen.get("manage") is True
    assert rec == []


async def test_close_no_account_replies_no_account(rec, monkeypatch):
    mgr = _FakeMgr(raise_exc=NoAccountConnected(7))
    monkeypatch.setattr(trading.common, "manager", lambda ctx: mgr)
    msg = _RecMsg()
    await trading.close(_update(msg), _ctx(["0xT"]))
    assert rec == []
    assert msg.sent  # bot.error.no_account rendered


async def test_close_positions_fetch_error_replies_generic(rec, monkeypatch):
    mgr = _FakeMgr(raise_exc=RuntimeError("egress blocked"))
    monkeypatch.setattr(trading.common, "manager", lambda ctx: mgr)
    msg = _RecMsg()
    await trading.close(_update(msg), _ctx(["0xT"]))
    assert rec == []
    assert msg.sent  # bot.error.generic rendered (caught by the broad except)


async def test_close_no_matching_position_replies_no_positions(rec, monkeypatch):
    # token not in positions → row None → size 0 → no_positions, no confirm.
    mgr = _FakeMgr(positions=[{"asset": "0xOTHER", "size": "5"}])
    monkeypatch.setattr(trading.common, "manager", lambda ctx: mgr)
    msg = _RecMsg()
    await trading.close(_update(msg), _ctx(["0xMINE"]))
    assert rec == []
    assert msg.sent  # bot.inquiry.no_positions


async def test_close_zero_size_position_replies_no_positions(rec, monkeypatch):
    mgr = _FakeMgr(positions=[{"asset": "0xMINE", "size": "0"}])
    monkeypatch.setattr(trading.common, "manager", lambda ctx: mgr)
    await trading.close(_update(), _ctx(["0xMINE"]))
    assert rec == []


async def test_close_valid_builds_close_intent(rec, monkeypatch):
    mgr = _FakeMgr(positions=[{"asset": "0xMINE", "size": "12.5",
                               "title": "Will it rain?", "currentValue": "9.25"}])
    monkeypatch.setattr(trading.common, "manager", lambda ctx: mgr)
    await trading.close(_update(), _ctx(["0xMINE"]))
    assert len(rec) == 1
    c = rec[0]
    assert c["intent"]["kind"] == "close"
    assert c["intent"]["side"] == "sell"
    assert c["intent"]["token_id"] == "0xMINE"
    assert c["intent"]["size"] == 12.5
    assert c["intent"]["title"] == "Will it rain?"
    assert c["key"] == "bot.confirm.close"
    # shares formatted with %g, est with thousands+2dp
    assert c["vars"]["shares"] == "12.5"
    assert c["vars"]["est"] == "9.25"
    assert "rain" in c["vars"]["title"]


async def test_close_falls_back_to_token_title_when_unlabeled(rec, monkeypatch):
    # row present but no title/outcome → title defaults to the token id.
    mgr = _FakeMgr(positions=[{"asset": "0xMINE", "size": "3"}])
    monkeypatch.setattr(trading.common, "manager", lambda ctx: mgr)
    await trading.close(_update(), _ctx(["0xMINE"]))
    assert len(rec) == 1
    assert rec[0]["intent"]["title"] == "0xMINE"
    assert rec[0]["vars"]["est"] == "0.00"  # no currentValue → 0


# ── /cancel and /cancelall ───────────────────────────────────────────────────────

async def test_cancel_no_args_shows_usage(rec):
    msg = _RecMsg()
    await trading.cancel(_update(msg), _ctx([]))
    assert rec == []
    assert "Usage: /cancel" in msg.sent[0][0]


async def test_cancel_valid_builds_cancel_intent(rec):
    await trading.cancel(_update(), _ctx(["ORDER-123"]))
    assert len(rec) == 1
    c = rec[0]
    assert c["intent"]["kind"] == "cancel"
    assert c["intent"]["order_id"] == "ORDER-123"
    assert c["key"] == "bot.confirm.cancel"
    assert c["vars"]["order_id"] == "ORDER-123"


async def test_cancelall_always_confirms_with_no_args(rec):
    await trading.cancelall(_update(), _ctx([]))
    assert len(rec) == 1
    c = rec[0]
    assert c["intent"]["kind"] == "cancel_all"
    # cancel_all carries no side/token fields beyond kind + ts
    assert set(c["intent"]) <= {"kind", "ts"}
    assert c["key"] == "bot.confirm.cancel_all"


async def test_handlers_tolerate_missing_args_attr(rec):
    # context.args is None (not yet populated) → treated as empty, usage shown.
    ctx = _ctx([])
    ctx.args = None
    msg = _RecMsg()
    await trading.buy(_update(msg), ctx)
    assert rec == []
    assert "Usage: /buy" in msg.sent[0][0]


# ── register() wires every command handler ───────────────────────────────────────

def test_register_wires_all_commands():
    added = []

    class _App:
        def add_handler(self, h):
            added.append(h)

    trading.register(_App())
    # one CommandHandler per command; collect the command strings
    cmds = set()
    for h in added:
        cmds |= set(getattr(h, "commands", []))
    assert {"buy", "sell", "marketbuy", "marketsell", "close", "cancel", "cancelall"} <= cmds
    assert len(added) == 7
