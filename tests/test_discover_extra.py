"""Discovery/browsing UI in bot/handlers/discover.py — everything EXCEPT the bet
funnel that tests/test_news_bet.py already covers. Pure helpers, the list+panel
flow, the generation guard, price/book delegation, search, /market, refresh, and
register() wiring. Network is never hit: the markets.* functions (run via
asyncio.to_thread) are monkeypatched on discover.markets, and inquiry.render_*
are replaced with async recorders."""

from types import SimpleNamespace

import pytest
from telegram import InlineKeyboardMarkup

from bot.handlers import common, discover


# ── fakes (mirror tests/test_news_bet.py) ──────────────────────────────────────

class _RecMsg:
    def __init__(self):
        self.sent = []

    async def reply_text(self, text, **kw):
        self.sent.append((text, kw))


class _Query:
    """Callback query whose .message is NOT a telegram.Message, so common.screen
    falls through to effective_message.reply_text (which we record)."""

    def __init__(self, data):
        self.data = data
        self.message = None

    async def answer(self, *a, **k):
        pass

    async def edit_message_text(self, *a, **k):
        pass


def _update(*, query=None, msg=None):
    return SimpleNamespace(callback_query=query, effective_message=msg,
                           effective_user=SimpleNamespace(id=111), effective_chat=None,
                           message=None)


def _ctx(**user_data):
    user_data.setdefault("lang", "en")
    return SimpleNamespace(user_data=user_data, bot=None,
                           application=SimpleNamespace(bot_data={}))


def _mkt(question="Will it rain?", yes=0.70, no=0.30, vol=1234.0,
         yes_token="tokYES", no_token="tokNO", mid="0xCOND"):
    return {"question": question, "yes_price": yes, "no_price": no, "volume": vol,
            "yes_token": yes_token, "no_token": no_token, "id": mid, "neg_risk": False}


def _datas(markup: InlineKeyboardMarkup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


# ── pure helpers: _pct ──────────────────────────────────────────────────────────

def test_pct_formats_fraction_as_percent():
    assert discover._pct(0.07) == "7%"
    assert discover._pct(0.5) == "50%"
    assert discover._pct(1) == "100%"
    assert discover._pct(0) == "0%"


def test_pct_bad_input_returns_emdash():
    assert discover._pct(None) == "—"
    assert discover._pct("abc") == "—"


# ── pure helpers: _vol ($, K, M tiers; bad → $0) ────────────────────────────────

def test_vol_tiers():
    assert discover._vol(500) == "$500"          # plain dollars
    assert discover._vol(12_345) == "$12K"        # thousands, no decimals
    assert discover._vol(2_500_000) == "$2.5M"    # millions, one decimal
    assert discover._vol(999) == "$999"


def test_vol_bad_input_returns_zero():
    assert discover._vol(None) == "$0"
    assert discover._vol("nope") == "$0"


# ── pure helpers: _truncate (short passthrough; word-boundary + ellipsis) ───────

def test_truncate_short_passthrough_and_strip():
    assert discover._truncate("  hi there  ") == "hi there"
    assert discover._truncate("") == ""
    assert discover._truncate(None) == ""


def test_truncate_long_breaks_on_word_boundary_with_ellipsis():
    s = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda"
    out = discover._truncate(s, n=20)
    assert out.endswith("…")
    body = out[:-1]
    assert len(body) <= 20
    assert " " in body and not body.endswith(" ")   # cut at a word boundary
    assert s.startswith(body)


def test_truncate_single_long_word_falls_back_to_hard_cut():
    # no space inside the window → rsplit yields '' → fall back to s[:n]
    s = "x" * 50
    out = discover._truncate(s, n=10)
    assert out == "x" * 10 + "…"


# ── pure helpers: _new_gen (monotonic, persisted in user_data) ──────────────────

def test_new_gen_monotonic_increment():
    ctx = _ctx()
    assert discover._new_gen(ctx) == 1
    assert discover._new_gen(ctx) == 2
    assert ctx.user_data[discover._GEN] == 2
    # seeded value continues from there
    ctx2 = _ctx(**{discover._GEN: 5})
    assert discover._new_gen(ctx2) == 6


# ── trending: list render / empty / error ───────────────────────────────────────

async def test_trending_renders_markets_and_stashes(monkeypatch):
    mkts = [_mkt(question="Q one"), _mkt(question="Q two", mid="0xC2")]
    monkeypatch.setattr(discover.markets, "trending_markets", lambda n: mkts)
    ctx = _ctx()
    msg = _RecMsg()
    await discover.trending(_update(msg=msg), ctx)
    # stashed under _MKTS as an index→payload map
    assert ctx.user_data[discover._MKTS]["0"]["question"] == "Q one"
    text, kw = msg.sent[0]
    datas = _datas(kw["reply_markup"])
    gen = ctx.user_data[discover._GEN]
    assert f"mkt:{gen}:0" in datas and f"mkt:{gen}:1" in datas
    assert "dcats" in datas and "dtrending" in datas   # cats + refresh row


async def test_trending_empty_shows_none(monkeypatch):
    monkeypatch.setattr(discover.markets, "trending_markets", lambda n: [])
    msg = _RecMsg()
    await discover.trending(_update(msg=msg), _ctx())
    assert "No markets" in msg.sent[0][0]


async def test_trending_thread_error_shows_generic(monkeypatch):
    def boom(n):
        raise RuntimeError("egress blocked")
    monkeypatch.setattr(discover.markets, "trending_markets", boom)
    msg = _RecMsg()
    await discover.trending(_update(msg=msg), _ctx())
    assert "went wrong" in msg.sent[0][0]


# ── categories: list render / empty / error ─────────────────────────────────────

async def test_categories_renders_cats_and_stashes(monkeypatch):
    cats = [{"title": "Politics", "slug": "politics", "volume": 5_000_000},
            {"title": "Sports", "slug": "sports", "volume": 1_200}]
    monkeypatch.setattr(discover.markets, "top_categories", lambda n: cats)
    ctx = _ctx()
    msg = _RecMsg()
    await discover.categories(_update(msg=msg), ctx)
    assert ctx.user_data[discover._CATS]["0"]["slug"] == "politics"
    datas = _datas(msg.sent[0][1]["reply_markup"])
    assert "cat:0" in datas and "cat:1" in datas
    assert "dtrending" in datas   # trending shortcut row


async def test_categories_empty_shows_none(monkeypatch):
    monkeypatch.setattr(discover.markets, "top_categories", lambda n: [])
    msg = _RecMsg()
    await discover.categories(_update(msg=msg), _ctx())
    assert "No markets" in msg.sent[0][0]


async def test_categories_thread_error_shows_generic(monkeypatch):
    def boom(n):
        raise ValueError("bad")
    monkeypatch.setattr(discover.markets, "top_categories", boom)
    msg = _RecMsg()
    await discover.categories(_update(msg=msg), _ctx())
    assert "went wrong" in msg.sent[0][0]


# ── on_cat: valid / missing stash / empty markets ───────────────────────────────

async def test_on_cat_valid_idx_loads_category_markets(monkeypatch):
    cats = [{"title": "Politics", "slug": "politics", "volume": 1.0}]
    seen = {}

    def fake_cat_markets(slug, limit):
        seen["slug"] = slug
        return [_mkt(question="Cat market")]

    monkeypatch.setattr(discover.markets, "top_categories", lambda n: cats)
    monkeypatch.setattr(discover.markets, "category_markets", fake_cat_markets)
    ctx = _ctx()
    await discover.categories(_update(msg=_RecMsg()), ctx)   # stashes _CATS
    msg = _RecMsg()
    await discover.on_cat(_update(query=_Query("cat:0"), msg=msg), ctx)
    assert seen["slug"] == "politics"
    datas = _datas(msg.sent[0][1]["reply_markup"])
    gen = ctx.user_data[discover._GEN]
    assert f"mkt:{gen}:0" in datas   # _show_markets rendered the category's markets


async def test_on_cat_missing_stash_shows_outdated(monkeypatch):
    monkeypatch.setattr(discover.markets, "category_markets", lambda s, n: [_mkt()])
    msg = _RecMsg()
    # no _CATS stash → from_stash returns None
    await discover.on_cat(_update(query=_Query("cat:0"), msg=msg), _ctx())
    assert "outdated" in msg.sent[0][0]


async def test_on_cat_empty_markets_shows_none(monkeypatch):
    cats = [{"title": "Politics", "slug": "politics", "volume": 1.0}]
    monkeypatch.setattr(discover.markets, "top_categories", lambda n: cats)
    monkeypatch.setattr(discover.markets, "category_markets", lambda s, n: [])
    ctx = _ctx()
    await discover.categories(_update(msg=_RecMsg()), ctx)
    msg = _RecMsg()
    await discover.on_cat(_update(query=_Query("cat:0"), msg=msg), ctx)
    assert "No markets" in msg.sent[0][0]


async def test_on_cat_thread_error_shows_generic(monkeypatch):
    cats = [{"title": "P", "slug": "p", "volume": 1.0}]
    monkeypatch.setattr(discover.markets, "top_categories", lambda n: cats)

    def boom(s, n):
        raise RuntimeError("x")
    monkeypatch.setattr(discover.markets, "category_markets", boom)
    ctx = _ctx()
    await discover.categories(_update(msg=_RecMsg()), ctx)
    msg = _RecMsg()
    await discover.on_cat(_update(query=_Query("cat:0"), msg=msg), ctx)
    assert "went wrong" in msg.sent[0][0]


# ── _resolve generation guard ────────────────────────────────────────────────────

def test_resolve_matching_gen_returns_dict():
    ctx = _ctx()
    ctx.user_data[discover._GEN] = 3
    common.stash(ctx, discover._MKTS, [_mkt(question="ok")])
    m = discover._resolve(ctx, "3", "0")
    assert isinstance(m, dict) and m["question"] == "ok"


def test_resolve_mismatched_gen_returns_none():
    ctx = _ctx()
    ctx.user_data[discover._GEN] = 3
    common.stash(ctx, discover._MKTS, [_mkt()])
    assert discover._resolve(ctx, "2", "0") is None   # stale generation


def test_resolve_non_dict_stash_returns_none():
    ctx = _ctx()
    ctx.user_data[discover._GEN] = 1
    common.stash(ctx, discover._MKTS, ["not-a-dict"])   # payload is a str, not a dict
    assert discover._resolve(ctx, "1", "0") is None


# ── on_market: panel render / stale ──────────────────────────────────────────────

async def test_on_market_valid_renders_panel(monkeypatch):
    ctx = _ctx()
    ctx.user_data[discover._GEN] = 1
    common.stash(ctx, discover._MKTS, [_mkt(question="Panel Q")])
    msg = _RecMsg()
    await discover.on_market(_update(query=_Query("mkt:1:0"), msg=msg), ctx)
    text, kw = msg.sent[0]
    assert "Panel Q" in text
    datas = _datas(kw["reply_markup"])
    assert "buy:1:0:yes" in datas and "buy:1:0:no" in datas
    assert "mprice:1:0" in datas and "mbook:1:0" in datas


async def test_on_market_stale_generation_shows_outdated():
    ctx = _ctx()
    ctx.user_data[discover._GEN] = 2
    common.stash(ctx, discover._MKTS, [_mkt()])
    msg = _RecMsg()
    await discover.on_market(_update(query=_Query("mkt:1:0"), msg=msg), ctx)
    assert "outdated" in msg.sent[0][0]


# ── on_market_price / on_market_book: delegate to inquiry.render_* ──────────────

async def test_on_market_price_delegates_with_yes_token(monkeypatch):
    captured = {}

    async def fake_render_price(update, context, token_id):
        captured["token"] = token_id
    monkeypatch.setattr(discover.inquiry, "render_price", fake_render_price)
    ctx = _ctx()
    ctx.user_data[discover._GEN] = 1
    common.stash(ctx, discover._MKTS, [_mkt(yes_token="tokYES")])
    await discover.on_market_price(_update(query=_Query("mprice:1:0")), ctx)
    assert captured["token"] == "tokYES"


async def test_on_market_price_stale_shows_outdated(monkeypatch):
    called = {}
    monkeypatch.setattr(discover.inquiry, "render_price",
                        lambda *a, **k: called.setdefault("hit", True))
    ctx = _ctx()
    ctx.user_data[discover._GEN] = 9
    common.stash(ctx, discover._MKTS, [_mkt()])
    msg = _RecMsg()
    await discover.on_market_price(_update(query=_Query("mprice:1:0"), msg=msg), ctx)
    assert "outdated" in msg.sent[0][0] and "hit" not in called


async def test_on_market_book_delegates_with_yes_token(monkeypatch):
    captured = {}

    async def fake_render_book(update, context, token_id):
        captured["token"] = token_id
    monkeypatch.setattr(discover.inquiry, "render_book", fake_render_book)
    ctx = _ctx()
    ctx.user_data[discover._GEN] = 1
    common.stash(ctx, discover._MKTS, [_mkt(yes_token="tokYES")])
    await discover.on_market_book(_update(query=_Query("mbook:1:0")), ctx)
    assert captured["token"] == "tokYES"


async def test_on_market_book_stale_shows_outdated(monkeypatch):
    called = {}
    monkeypatch.setattr(discover.inquiry, "render_book",
                        lambda *a, **k: called.setdefault("hit", True))
    ctx = _ctx()
    ctx.user_data[discover._GEN] = 9
    common.stash(ctx, discover._MKTS, [_mkt()])
    msg = _RecMsg()
    await discover.on_market_book(_update(query=_Query("mbook:1:0"), msg=msg), ctx)
    assert "outdated" in msg.sent[0][0] and "hit" not in called


# ── on_buy: amount picker / stale ────────────────────────────────────────────────

async def test_on_buy_valid_shows_amount_picker():
    ctx = _ctx()
    ctx.user_data[discover._GEN] = 1
    common.stash(ctx, discover._MKTS, [_mkt(question="Buy Q")])
    msg = _RecMsg()
    await discover.on_buy(_update(query=_Query("buy:1:0:yes"), msg=msg), ctx)
    text, kw = msg.sent[0]
    assert "YES" in text and "Buy Q" in text
    datas = _datas(kw["reply_markup"])
    # preset amount buttons + custom + back to the panel
    for a in discover._AMOUNTS:
        assert f"buyamt:1:0:yes:{a}" in datas
    assert "buycustom:1:0:yes" in datas
    assert "mkt:1:0" in datas   # back button targets the panel


async def test_on_buy_stale_shows_outdated():
    ctx = _ctx()
    ctx.user_data[discover._GEN] = 5
    common.stash(ctx, discover._MKTS, [_mkt()])
    msg = _RecMsg()
    await discover.on_buy(_update(query=_Query("buy:1:0:no"), msg=msg), ctx)
    assert "outdated" in msg.sent[0][0]


# ── search: usage / results / no results / error ────────────────────────────────

async def test_search_no_args_shows_usage():
    ctx = _ctx()
    msg = _RecMsg()
    upd = SimpleNamespace(callback_query=None, effective_message=msg,
                          effective_user=SimpleNamespace(id=111), effective_chat=None)
    # context.args empty
    ctx_with_args = SimpleNamespace(user_data={"lang": "en"}, bot=None,
                                    application=SimpleNamespace(bot_data={}), args=[])
    await discover.search(upd, ctx_with_args)
    assert "/search" in msg.sent[0][0]


async def test_search_results_render_markets(monkeypatch):
    monkeypatch.setattr(discover.markets, "search_markets",
                        lambda q, n: [_mkt(question="Found it")])
    ctx = SimpleNamespace(user_data={"lang": "en"}, bot=None,
                          application=SimpleNamespace(bot_data={}), args=["rain", "today"])
    msg = _RecMsg()
    upd = SimpleNamespace(callback_query=None, effective_message=msg,
                          effective_user=SimpleNamespace(id=111), effective_chat=None)
    await discover.search(upd, ctx)
    datas = _datas(msg.sent[0][1]["reply_markup"])
    gen = ctx.user_data[discover._GEN]
    assert f"mkt:{gen}:0" in datas


async def test_search_no_results_shows_no_results(monkeypatch):
    monkeypatch.setattr(discover.markets, "search_markets", lambda q, n: [])
    ctx = SimpleNamespace(user_data={"lang": "en"}, bot=None,
                          application=SimpleNamespace(bot_data={}), args=["zzz"])
    msg = _RecMsg()
    upd = SimpleNamespace(callback_query=None, effective_message=msg,
                          effective_user=SimpleNamespace(id=111), effective_chat=None)
    await discover.search(upd, ctx)
    text, kw = msg.sent[0]
    assert "zzz" in text   # query interpolated into no_results
    assert "dtrending" in _datas(kw["reply_markup"])


async def test_search_thread_error_shows_generic(monkeypatch):
    def boom(q, n):
        raise RuntimeError("x")
    monkeypatch.setattr(discover.markets, "search_markets", boom)
    ctx = SimpleNamespace(user_data={"lang": "en"}, bot=None,
                          application=SimpleNamespace(bot_data={}), args=["q"])
    msg = _RecMsg()
    upd = SimpleNamespace(callback_query=None, effective_message=msg,
                          effective_user=SimpleNamespace(id=111), effective_chat=None)
    await discover.search(upd, ctx)
    assert "went wrong" in msg.sent[0][0]


# ── show_market_by_id + /market command ──────────────────────────────────────────

async def test_show_market_by_id_found_renders_panel_returns_true(monkeypatch):
    monkeypatch.setattr(discover.markets, "get_market", lambda mid: _mkt(question="Single Q"))
    ctx = _ctx()
    msg = _RecMsg()
    ok = await discover.show_market_by_id(_update(msg=msg), ctx, "0xCOND")
    assert ok is True
    # stashed as a fresh single-element list and a fresh generation
    assert ctx.user_data[discover._MKTS]["0"]["question"] == "Single Q"
    gen = ctx.user_data[discover._GEN]
    text, kw = msg.sent[0]
    assert "Single Q" in text
    assert f"buy:{gen}:0:yes" in _datas(kw["reply_markup"])


async def test_show_market_by_id_not_found_returns_false(monkeypatch):
    monkeypatch.setattr(discover.markets, "get_market", lambda mid: None)
    msg = _RecMsg()
    ok = await discover.show_market_by_id(_update(msg=msg), _ctx(), "0xCOND")
    assert ok is False
    assert "not found" in msg.sent[0][0]


async def test_show_market_by_id_thread_error_returns_false(monkeypatch):
    def boom(mid):
        raise RuntimeError("x")
    monkeypatch.setattr(discover.markets, "get_market", boom)
    msg = _RecMsg()
    ok = await discover.show_market_by_id(_update(msg=msg), _ctx(), "0xCOND")
    assert ok is False
    assert "went wrong" in msg.sent[0][0]


async def test_market_command_no_args_shows_usage():
    ctx = SimpleNamespace(user_data={"lang": "en"}, bot=None,
                          application=SimpleNamespace(bot_data={}), args=[])
    msg = _RecMsg()
    upd = SimpleNamespace(callback_query=None, effective_message=msg,
                          effective_user=SimpleNamespace(id=111), effective_chat=None)
    await discover.market(upd, ctx)
    assert "/market" in msg.sent[0][0]


async def test_market_command_delegates_to_show_by_id(monkeypatch):
    monkeypatch.setattr(discover.markets, "get_market", lambda mid: _mkt(question="Cmd Q", mid=mid))
    ctx = SimpleNamespace(user_data={"lang": "en"}, bot=None,
                          application=SimpleNamespace(bot_data={}), args=["0xABC"])
    msg = _RecMsg()
    upd = SimpleNamespace(callback_query=None, effective_message=msg,
                          effective_user=SimpleNamespace(id=111), effective_chat=None)
    await discover.market(upd, ctx)
    assert "Cmd Q" in msg.sent[0][0]


# ── on_refresh: dcats → categories, anything else → trending ────────────────────

async def test_on_refresh_dcats_routes_to_categories(monkeypatch):
    monkeypatch.setattr(discover.markets, "top_categories",
                        lambda n: [{"title": "Politics", "slug": "p", "volume": 1.0}])
    monkeypatch.setattr(discover.markets, "trending_markets",
                        lambda n: pytest.fail("should not call trending"))
    ctx = _ctx()
    msg = _RecMsg()
    await discover.on_refresh(_update(query=_Query("dcats"), msg=msg), ctx)
    assert "cat:0" in _datas(msg.sent[0][1]["reply_markup"])


async def test_on_refresh_dtrending_routes_to_trending(monkeypatch):
    monkeypatch.setattr(discover.markets, "trending_markets", lambda n: [_mkt()])
    ctx = _ctx()
    msg = _RecMsg()
    await discover.on_refresh(_update(query=_Query("dtrending"), msg=msg), ctx)
    gen = ctx.user_data[discover._GEN]
    assert f"mkt:{gen}:0" in _datas(msg.sent[0][1]["reply_markup"])


async def test_on_refresh_no_callback_data_defaults_to_trending(monkeypatch):
    monkeypatch.setattr(discover.markets, "trending_markets", lambda n: [_mkt()])
    ctx = _ctx()
    msg = _RecMsg()
    # callback_query present but data None → else branch → trending
    await discover.on_refresh(_update(query=_Query(None), msg=msg), ctx)
    assert msg.sent  # trending rendered


# ── register(): wires every handler + the group-1 text handler ──────────────────

def test_register_wires_all_handlers():
    added = []

    class _FakeApp:
        def add_handler(self, handler, group=0):
            added.append((handler, group))

    discover.register(_FakeApp())
    # 4 command handlers + 8 callback handlers (on_cat/on_market/price/book/
    # on_buy/on_buy_amount/on_buy_custom/on_bet_account) + on_refresh + 1 message handler
    assert len(added) == 14
    groups = [g for _, g in added]
    assert groups.count(1) == 1          # exactly one group-1 (typed custom amount)
    assert groups.count(0) == 13
    # the group-1 handler is the typed-amount text handler
    grp1 = [h for h, g in added if g == 1][0]
    assert grp1.callback is discover.on_custom_amount
