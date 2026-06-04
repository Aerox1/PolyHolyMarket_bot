"""Button-driven /manage position view + ^pos: action routing.

Telegram + AccountManager + confirm.request are mocked — no network, no orders.
We assert STRUCTURE (callback_data, stash contents, intent kind/fields) over copy.
"""

from types import SimpleNamespace

import pytest

from bot.handlers import common, positions_ui
from polymarket.credentials import NoAccountConnected


# ── fakes ──────────────────────────────────────────────────────────────────────

class _RecMsg:
    def __init__(self):
        self.sent = []

    async def reply_text(self, text, **kw):
        self.sent.append((text, kw))


class _Query:
    def __init__(self, data):
        self.data = data
        self.message = _RecMsg()  # not a telegram.Message → screen falls through to reply
        self.answered = False

    async def answer(self, *a, **k):
        self.answered = True

    async def edit_message_text(self, *a, **k):
        pass


def _cmd_update(msg=None):
    # command-style update (no callback_query); effective_chat=None so typing() no-ops
    return SimpleNamespace(callback_query=None, effective_message=(msg or _RecMsg()),
                           effective_user=SimpleNamespace(id=111), effective_chat=None,
                           message=SimpleNamespace(text="/manage"))


def _cb_update(query):
    return SimpleNamespace(callback_query=query, effective_message=query.message,
                           effective_user=SimpleNamespace(id=111), effective_chat=None)


def _ctx(**ud):
    ud.setdefault("lang", "en")
    ud.setdefault("db_user_id", 1)
    return SimpleNamespace(user_data=ud, bot=None,
                           application=SimpleNamespace(bot_data={}))


def _row(token="0xToken1234567890", size=10, **extra):
    r = {"asset": token, "size": size, "title": "Will it rain?", "outcome": "Yes",
         "avgPrice": 0.42, "currentValue": 12.5, "percentPnl": 3.2}
    r.update(extra)
    return r


class FakeMgr:
    """AccountManager stand-in. Returns a readonly client whose get_positions is SYNC."""

    def __init__(self, positions=None, raise_no_account=False):
        self._positions = positions
        self._raise = raise_no_account

    async def get_readonly_client(self, uid):
        if self._raise:
            raise NoAccountConnected(uid)
        pos = self._positions

        class _Client:
            def get_positions(self_inner):  # SYNC — handler calls via asyncio.to_thread
                return pos
        return _Client()


def _install_mgr(monkeypatch, mgr):
    monkeypatch.setattr(positions_ui.common, "manager", lambda ctx: mgr)


# ── pure helpers: _rows ──────────────────────────────────────────────────────────

def test_rows_extracts_data_key():
    assert positions_ui._rows({"data": [1, 2]}) == [1, 2]


def test_rows_extracts_positions_key_when_no_data():
    assert positions_ui._rows({"positions": [3]}) == [3]
    # data takes precedence over positions
    assert positions_ui._rows({"data": ["a"], "positions": ["b"]}) == ["a"]


def test_rows_dict_without_lists_yields_empty():
    # dict with neither key → [] (the `or []` fallback, then list-check passes)
    assert positions_ui._rows({"foo": 1}) == []
    # dict whose "data" is not a list → [] (final isinstance guard)
    assert positions_ui._rows({"data": {"nope": 1}}) == []


def test_rows_bare_list_passthrough():
    assert positions_ui._rows([{"x": 1}]) == [{"x": 1}]


def test_rows_non_list_non_dict_yields_empty():
    assert positions_ui._rows(None) == []
    assert positions_ui._rows("string") == []
    assert positions_ui._rows(42) == []


# ── pure helpers: _field ─────────────────────────────────────────────────────────

def test_field_returns_first_non_empty():
    row = {"a": "", "b": None, "c": "hit", "d": "later"}
    assert positions_ui._field(row, "a", "b", "c", "d") == "hit"


def test_field_skips_empty_string_and_none():
    assert positions_ui._field({"a": "", "b": "val"}, "a", "b") == "val"
    # 0 is non-empty (only None / "" are skipped) → returned
    assert positions_ui._field({"a": 0}, "a") == 0


def test_field_none_when_all_missing_or_empty():
    assert positions_ui._field({"a": "", "b": None}, "a", "b") is None
    assert positions_ui._field({}, "x", "y") is None


# ── pure helpers: _f ─────────────────────────────────────────────────────────────

def test_f_parses_numbers_and_strings():
    assert positions_ui._f("3.5") == 3.5
    assert positions_ui._f(2) == 2.0


def test_f_none_and_bad_become_zero():
    assert positions_ui._f(None) == 0.0
    assert positions_ui._f("not-a-number") == 0.0
    assert positions_ui._f([1, 2]) == 0.0  # TypeError path
    assert positions_ui._f(0) == 0.0


# ── manage: happy path ───────────────────────────────────────────────────────────

async def test_manage_renders_numbered_rows_and_stashes_tokens(monkeypatch):
    rows = [_row(token="0xAAA1111111", size=10, title="Rain?"),
            _row(token="0xBBB2222222", size=5, title="Snow?")]
    _install_mgr(monkeypatch, FakeMgr(positions=rows))
    msg = _RecMsg()
    ctx = _ctx()
    await positions_ui.manage(_cmd_update(msg), ctx)

    # one screen rendered via effective_message.reply_text (no callback_query)
    assert len(msg.sent) == 1
    text, kw = msg.sent[0]
    # numbered lines (1. / 2.) with the titles
    assert "1. Rain?" in text and "2. Snow?" in text

    # stash populated with (token, size, title, value) tuples, indexed by string
    stash = ctx.user_data["pos_tokens"]
    assert stash["0"][0] == "0xAAA1111111" and stash["0"][1] == 10
    assert stash["1"][0] == "0xBBB2222222"

    # each row gets the 25/50/75/100 keyboard; nav row appended by with_nav
    kb = kw["reply_markup"].inline_keyboard
    datas = [b.callback_data for r in kb for b in r if b.callback_data]
    for pct in (25, 50, 75, 100):
        assert f"pos:0:{pct}" in datas
        assert f"pos:1:{pct}" in datas


async def test_manage_skips_rows_without_token_or_nonpositive_size(monkeypatch):
    rows = [
        _row(token="0xGOOD0000000", size=10, title="Keep"),      # valid → idx 0
        {"size": 5, "title": "NoToken"},                         # no token → skipped
        _row(token="0xZERO0000000", size=0, title="ZeroSize"),   # size 0 → skipped
        _row(token="0xNEG00000000", size=-3, title="NegSize"),   # size<0 → skipped
        "not-a-dict",                                            # non-dict → skipped
    ]
    _install_mgr(monkeypatch, FakeMgr(positions=rows))
    ctx = _ctx()
    msg = _RecMsg()
    await positions_ui.manage(_cmd_update(msg), ctx)

    stash = ctx.user_data["pos_tokens"]
    assert list(stash.keys()) == ["0"]  # only the one valid row survived
    assert stash["0"][2] == "Keep"
    text, _kw = msg.sent[0]
    assert "NoToken" not in text and "ZeroSize" not in text and "NegSize" not in text


async def test_manage_caps_at_ten_with_showing_note(monkeypatch):
    rows = [_row(token=f"0xTok{i:08d}", size=i + 1, title=f"M{i}") for i in range(15)]
    _install_mgr(monkeypatch, FakeMgr(positions=rows))
    ctx = _ctx()
    msg = _RecMsg()
    await positions_ui.manage(_cmd_update(msg), ctx)

    stash = ctx.user_data["pos_tokens"]
    assert len(stash) == positions_ui._MANAGE_CAP == 10  # capped
    text, _kw = msg.sent[0]
    # the localized "showing N of total" note (15 valid, 10 shown)
    assert "10" in text and "15" in text


async def test_manage_no_valid_positions_replies_no_positions(monkeypatch):
    # rows present but ALL invalid → no_positions branch with a trending keyboard
    rows = [{"size": 5}, _row(token="0xX", size=0)]
    _install_mgr(monkeypatch, FakeMgr(positions=rows))
    ctx = _ctx()
    msg = _RecMsg()
    await positions_ui.manage(_cmd_update(msg), ctx)

    assert "pos_tokens" not in ctx.user_data  # nothing stashed
    text, kw = msg.sent[0]
    assert text == common.tr(ctx, "bot.inquiry.no_positions")
    datas = [b.callback_data for r in kw["reply_markup"].inline_keyboard for b in r if b.callback_data]
    assert "menu:trending" in datas


async def test_manage_empty_payload_also_no_positions(monkeypatch):
    _install_mgr(monkeypatch, FakeMgr(positions=[]))
    ctx = _ctx()
    msg = _RecMsg()
    await positions_ui.manage(_cmd_update(msg), ctx)
    assert "pos_tokens" not in ctx.user_data
    assert msg.sent[0][0] == common.tr(ctx, "bot.inquiry.no_positions")


async def test_manage_no_account_connected(monkeypatch):
    _install_mgr(monkeypatch, FakeMgr(raise_no_account=True))
    ctx = _ctx()
    msg = _RecMsg()
    await positions_ui.manage(_cmd_update(msg), ctx)

    text, kw = msg.sent[0]
    assert text == common.tr(ctx, "bot.error.no_account")
    # connect_keyboard offers the connect button
    datas = [b.callback_data for r in kw["reply_markup"].inline_keyboard for b in r if b.callback_data]
    assert "menu:connect" in datas


async def test_manage_generic_error_on_unexpected_exception(monkeypatch):
    class _BoomMgr:
        async def get_readonly_client(self, uid):
            raise RuntimeError("egress blocked")
    _install_mgr(monkeypatch, _BoomMgr())
    ctx = _ctx()
    msg = _RecMsg()
    await positions_ui.manage(_cmd_update(msg), ctx)
    assert msg.sent[0][0] == common.tr(ctx, "bot.error.generic")


# ── on_position_action ───────────────────────────────────────────────────────────

def _stash_one(ctx, token="0xTokABC", size=20.0, title="Will it rain?", value=30.0):
    common.stash(ctx, "pos_tokens", [(token, size, title, value)])


async def test_action_sell_partial_builds_market_sell_intent(monkeypatch):
    captured = {}

    async def fake_request(update, context, intent, key, **vars):
        captured["intent"] = intent
        captured["key"] = key
        captured["vars"] = vars

    monkeypatch.setattr(positions_ui.confirm, "request", fake_request)
    ctx = _ctx()
    _stash_one(ctx, size=20.0, value=30.0)
    q = _Query("pos:0:50")
    await positions_ui.on_position_action(_cb_update(q), ctx)

    assert q.answered  # query.answer() awaited
    it = captured["intent"]
    assert it["kind"] == "market" and it["side"] == "sell"
    assert it["token_id"] == "0xTokABC"
    # sell_size = round(size * pct/100, 6)
    assert it["amount"] == round(20.0 * 0.50, 6) == 10.0
    assert captured["key"] == "bot.confirm.sell_pos"
    assert captured["vars"]["pct"] == 50
    # est in vars is value*pct/100 formatted; shares is the sell_size
    assert captured["vars"]["shares"] == "10"


async def test_action_sell_25_and_75_round_sizes(monkeypatch):
    captured = []

    async def fake_request(update, context, intent, key, **vars):
        captured.append((intent, vars))

    monkeypatch.setattr(positions_ui.confirm, "request", fake_request)
    ctx = _ctx()
    _stash_one(ctx, size=3.0, value=9.0)  # 3 * 0.25 = 0.75 share
    await positions_ui.on_position_action(_cb_update(_Query("pos:0:25")), ctx)
    await positions_ui.on_position_action(_cb_update(_Query("pos:0:75")), ctx)

    assert captured[0][0]["amount"] == round(3.0 * 0.25, 6) == 0.75
    assert captured[0][1]["pct"] == 25
    assert captured[1][0]["amount"] == round(3.0 * 0.75, 6) == 2.25
    assert captured[1][1]["pct"] == 75


async def test_action_full_close_builds_close_intent(monkeypatch):
    captured = {}

    async def fake_request(update, context, intent, key, **vars):
        captured["intent"] = intent
        captured["key"] = key
        captured["vars"] = vars

    monkeypatch.setattr(positions_ui.confirm, "request", fake_request)
    ctx = _ctx()
    _stash_one(ctx, size=20.0, value=30.0)
    await positions_ui.on_position_action(_cb_update(_Query("pos:0:100")), ctx)

    it = captured["intent"]
    assert it["kind"] == "close" and it["side"] == "sell"
    assert it["size"] == 20.0 and it["token_id"] == "0xTokABC"
    assert captured["key"] == "bot.confirm.close"
    assert captured["vars"]["shares"] == "20"


async def test_action_malformed_data_returns_silently(monkeypatch):
    called = {}

    async def fake_request(*a, **k):
        called["hit"] = True

    monkeypatch.setattr(positions_ui.confirm, "request", fake_request)
    ctx = _ctx()
    _stash_one(ctx)
    q = _Query("pos:0")  # only 2 parts → len != 3 → return
    await positions_ui.on_position_action(_cb_update(q), ctx)
    assert "hit" not in called
    assert q.answered  # answered before the early return
    assert q.message.sent == []  # nothing replied


async def test_action_non_integer_pct_returns(monkeypatch):
    called = {}

    async def fake_request(*a, **k):
        called["hit"] = True

    monkeypatch.setattr(positions_ui.confirm, "request", fake_request)
    ctx = _ctx()
    _stash_one(ctx)
    q = _Query("pos:0:abc")  # 3 parts but pct not an int → ValueError → return
    await positions_ui.on_position_action(_cb_update(q), ctx)
    assert "hit" not in called
    assert q.message.sent == []


async def test_action_missing_stash_replies_expired(monkeypatch):
    called = {}

    async def fake_request(*a, **k):
        called["hit"] = True

    monkeypatch.setattr(positions_ui.confirm, "request", fake_request)
    ctx = _ctx()  # no pos_tokens stash at all
    q = _Query("pos:0:50")
    await positions_ui.on_position_action(_cb_update(q), ctx)

    assert "hit" not in called
    assert q.message.sent[0][0] == common.tr(ctx, "bot.confirm.expired")


async def test_action_expired_index_replies_expired(monkeypatch):
    async def fake_request(*a, **k):
        raise AssertionError("should not be reached")

    monkeypatch.setattr(positions_ui.confirm, "request", fake_request)
    ctx = _ctx()
    _stash_one(ctx)  # only index "0" exists
    q = _Query("pos:5:50")  # index 5 missing → expired
    await positions_ui.on_position_action(_cb_update(q), ctx)
    assert q.message.sent[0][0] == common.tr(ctx, "bot.confirm.expired")


# ── register ─────────────────────────────────────────────────────────────────────

def test_register_adds_command_and_callback_handlers():
    from telegram.ext import CallbackQueryHandler, CommandHandler

    added = []

    class _App:
        def add_handler(self, h):
            added.append(h)

    positions_ui.register(_App())
    assert any(isinstance(h, CommandHandler) for h in added)
    cqs = [h for h in added if isinstance(h, CallbackQueryHandler)]
    assert cqs and cqs[0].pattern.pattern == "^pos:"
