"""Bet-on-news CTA: channel buttons + deep-link, fresh outcome→token resolution,
the slippage-capped buy, deferred-intent persistence/resume, and bet recording.

Network (Gamma/CLOB) and Telegram are mocked — no egress, no real orders."""

from types import SimpleNamespace

import pytest

from bot.handlers import common, confirm, connect, discover, start
from bot.news import cta as news_cta
from bot.news import publisher
from db.engine import async_session_scope
from db.models import Bet, PendingIntent
from db.repositories import news_items as items_repo
from db.repositories import pending_intents as intents_repo
from db.repositories import users as users_repo
from polymarket import markets
from polymarket.client import Polymarket


# ── fakes ──────────────────────────────────────────────────────────────────────

class _Query:
    def __init__(self, data):
        self.data = data

    async def answer(self):
        pass

    async def edit_message_text(self, *a, **k):
        pass


class _RecMsg:
    def __init__(self):
        self.sent = []

    async def reply_text(self, text, **kw):
        self.sent.append((text, kw))


def _update(*, query=None, msg=None):
    return SimpleNamespace(callback_query=query, effective_message=msg,
                           effective_user=SimpleNamespace(id=111))


def _ctx(**user_data):
    user_data.setdefault("lang", "en")
    return SimpleNamespace(user_data=user_data, bot=None, application=None)


def _gamma_market(closed=False, active=True):
    return {"conditionId": "0xCOND", "question": "Will it rain?",
            "outcomes": '["Yes","No"]', "clobTokenIds": '["tokYES","tokNO"]',
            "outcomePrices": '["0.70","0.30"]', "closed": closed, "active": active,
            "volume24hr": "1000", "negRisk": False}


# ── deep-link + channel keyboard (Phase A) ─────────────────────────────────────

def test_bet_deeplink_encodes_item_and_outcome():
    assert news_cta.bet_deeplink("Bot", item_id=42, outcome="YES") == "https://t.me/Bot?start=nb-42-y"
    assert news_cta.bet_deeplink("Bot", item_id=42, outcome="NO") == "https://t.me/Bot?start=nb-42-n"
    # well under Telegram's 64-char start-payload cap, no conditionId encoded
    assert len("nb-42-y") < 64 and "0x" not in news_cta.bet_deeplink("Bot", item_id=42, outcome="YES")


def test_build_keyboard_two_bet_buttons_when_market_resolved():
    item = SimpleNamespace(id=7, cta_market_id="0xCOND", cta_url="https://t.me/Bot?start=n-7")
    kb = publisher.build_keyboard(item, bot_username="Bot", lang="en")
    row = kb.inline_keyboard[0]
    assert [b.url for b in row] == ["https://t.me/Bot?start=nb-7-y", "https://t.me/Bot?start=nb-7-n"]
    assert "YES" in row[0].text and "NO" in row[1].text


def test_build_keyboard_single_button_without_market():
    item = SimpleNamespace(id=7, cta_market_id=None, cta_url="https://t.me/Bot?start=n-7")
    kb = publisher.build_keyboard(item, bot_username="Bot", lang="en")
    assert len(kb.inline_keyboard[0]) == 1
    assert kb.inline_keyboard[0][0].url == "https://t.me/Bot?start=n-7"
    # no link target at all → no keyboard
    assert publisher.build_keyboard(SimpleNamespace(id=7, cta_market_id=None, cta_url=None),
                                    bot_username=None, lang="en") is None


# ── market-state: closed vs transient (markets.get_market_state) ───────────────

def _fake_client(resp):
    class _C:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, path, params=None):
            if isinstance(resp, Exception):
                raise resp
            return resp
    return _C()


def _resp(status, payload):
    return SimpleNamespace(status_code=status, json=lambda: payload)


def test_get_market_state_open_closed_error(monkeypatch):
    # open: a live binary market
    monkeypatch.setattr(markets, "_client", lambda: _fake_client(_resp(200, [_gamma_market()])))
    state, m = markets.get_market_state("0xCOND")
    assert state == "open" and m["yes_token"] == "tokYES" and m["no_token"] == "tokNO"

    # closed: exists but resolved/inactive → don't bet (NOT retryable)
    monkeypatch.setattr(markets, "_client", lambda: _fake_client(_resp(200, [_gamma_market(closed=True)])))
    assert markets.get_market_state("0xCOND") == ("closed", None)

    # transient: non-200, empty body, and exception ALL map to retryable 'error'
    monkeypatch.setattr(markets, "_client", lambda: _fake_client(_resp(503, [])))
    assert markets.get_market_state("0xCOND") == ("error", None)
    monkeypatch.setattr(markets, "_client", lambda: _fake_client(_resp(200, [])))
    assert markets.get_market_state("0xCOND") == ("error", None)
    monkeypatch.setattr(markets, "_client", lambda: _fake_client(RuntimeError("egress blocked")))
    assert markets.get_market_state("0xCOND") == ("error", None)


# ── slippage-capped buy (client) ───────────────────────────────────────────────

class _FakeClob:
    def __init__(self):
        self.created = None
        self.posted = None

    def create_order(self, order_args, options):
        self.created = (order_args, options)
        return "signed"

    def post_order(self, signed, order_type):
        self.posted = (signed, order_type)
        return {"orderID": "OID"}


def _pm_with_clob(clob):
    pm = Polymarket.__new__(Polymarket)          # bypass __init__ (no network)
    pm._order_signing_ready = True
    pm._clob = clob
    return pm


def test_place_capped_buy_is_fok_limit_sized_to_amount():
    from py_clob_client.clob_types import OrderType
    from py_clob_client.order_builder.constants import BUY

    clob = _FakeClob()
    pm = _pm_with_clob(clob)
    pm.place_capped_buy("tokYES", 25.0, 0.80, neg_risk=False)
    args, _opts = clob.created
    assert args.price == 0.80
    assert args.size == round(25.0 / 0.80, 2)     # shares; cost ≤ $25 at the ceiling
    assert args.side == BUY
    assert clob.posted[1] == OrderType.FOK         # fill-or-kill, never a worse partial


def test_place_capped_buy_clamps_price_to_tick_range():
    clob = _FakeClob()
    _pm_with_clob(clob).place_capped_buy("t", 10.0, 1.5)   # above $1 → clamp to 0.99
    assert clob.created[0].price == 0.99
    clob2 = _FakeClob()
    _pm_with_clob(clob2).place_capped_buy("t", 10.0, 0.0)  # below tick → clamp to 0.01
    assert clob2.created[0].price == 0.01


# ── pending-intent repo (Phase C) ──────────────────────────────────────────────

async def _seed_user(tg_id=555):
    async with async_session_scope() as s:
        u = await users_repo.get_or_create_user(s, telegram_id=tg_id, username="u",
                                                 first_name="U", default_language="en")
        return u.id


async def test_intent_upsert_idempotent_per_outcome():
    uid = await _seed_user()
    async with async_session_scope() as s:
        a = await intents_repo.upsert_intent(s, user_id=uid, news_item_id=None, market_id="0xC", outcome="YES")
        a_id = a.id
    async with async_session_scope() as s:
        b = await intents_repo.upsert_intent(s, user_id=uid, news_item_id=None, market_id="0xC", outcome="YES")
        assert b.id == a_id  # same (user,item,outcome) → updates the SAME row
    async with async_session_scope() as s:
        c = await intents_repo.upsert_intent(s, user_id=uid, news_item_id=None, market_id="0xC", outcome="NO")
        assert c.id != a_id  # the other outcome → a distinct row
        latest = await intents_repo.latest_pending(s, uid)
        assert latest.id == c.id  # newest pending wins (last-tap-wins)


async def test_intent_expire_stale_marks_past_ttl():
    uid = await _seed_user(tg_id=556)
    async with async_session_scope() as s:
        row = await intents_repo.upsert_intent(s, user_id=uid, news_item_id=None, market_id="0xC",
                                               outcome="YES", ttl_hours=-1)  # already expired
        rid = row.id
    async with async_session_scope() as s:
        n = await intents_repo.expire_stale(s)
        assert n >= 1
        assert (await s.get(PendingIntent, rid)).status == "expired"
        assert await intents_repo.latest_pending(s, uid) is None  # expired → not resumable


# ── /start nb- routing (Phase A/B) ─────────────────────────────────────────────

async def test_start_routes_nb_payload_to_open_news_bet(monkeypatch):
    seen = {}

    async def fake_bet(update, context, item_id, outcome):
        seen["call"] = (item_id, outcome)

    async def fake_item(update, context, item_id):
        seen["item"] = item_id

    monkeypatch.setattr(start, "_open_news_bet", fake_bet)
    monkeypatch.setattr(start, "_open_news_item", fake_item)
    await start.start(_update(), SimpleNamespace(args=["nb-5-y"], user_data={"lang": "en"}))
    assert seen.get("call") == (5, "YES") and "item" not in seen
    seen.clear()
    await start.start(_update(), SimpleNamespace(args=["nb-9-n"], user_data={"lang": "en"}))
    assert seen.get("call") == (9, "NO")
    # plain n- still opens the item (not the bet funnel)
    seen.clear()
    await start.start(_update(), SimpleNamespace(args=["n-7"], user_data={"lang": "en"}))
    assert seen.get("item") == 7 and "call" not in seen


async def test_start_malformed_nb_falls_through_to_dashboard(monkeypatch):
    seen = {}

    async def fake_bet(update, context, item_id, outcome):
        seen["bet"] = True

    async def fake_dash(update, context, **kw):
        seen["dash"] = True

    monkeypatch.setattr(start, "_open_news_bet", fake_bet)
    monkeypatch.setattr(start, "show_dashboard", fake_dash)
    await start.start(_update(), SimpleNamespace(args=["nb-abc-y"], user_data={"lang": "en"}))
    assert seen.get("dash") is True and "bet" not in seen


async def _seed_item(market_id="0xCOND"):
    async with async_session_scope() as s:
        it = await items_repo.create(s, url="u1", url_hash="h1", title_orig="Big Headline")
        it.cta_market_id = market_id
        return it.id


async def test_open_news_bet_connected_jumps_to_picker(monkeypatch):
    uid = await _seed_user(tg_id=601)
    item_id = await _seed_item()
    captured = {}

    async def fake_show(update, context, market_id, **kw):
        captured.update(market_id=market_id, **kw)

    async def fake_resolve(session, user_id, account_id=None):
        return SimpleNamespace(id=1)  # connected

    monkeypatch.setattr(start.discover, "show_market_for_bet", fake_show)
    monkeypatch.setattr(start.accounts_repo, "resolve_account", fake_resolve)
    await start._open_news_bet(_update(), _ctx(db_user_id=uid), item_id, "NO")
    assert captured["market_id"] == "0xCOND"
    assert captured["preselect_outcome"] == "NO" and captured["news_item_id"] == item_id


async def test_open_news_bet_new_user_persists_intent_and_prompts(monkeypatch):
    uid = await _seed_user(tg_id=602)
    item_id = await _seed_item()

    async def fake_resolve(session, user_id, account_id=None):
        return None  # not connected

    monkeypatch.setattr(start.accounts_repo, "resolve_account", fake_resolve)
    msg = _RecMsg()
    ctx = _ctx(db_user_id=uid)
    await start._open_news_bet(_update(msg=msg), ctx, item_id, "YES")

    # intent persisted + resume armed + a prompt shown with the headline + connect button
    assert ctx.user_data.get("news_bet_armed")
    async with async_session_scope() as s:
        row = await intents_repo.latest_pending(s, uid)
        assert row is not None and row.outcome == "YES" and row.market_id == "0xCOND"
    text, kw = msg.sent[0]
    assert "Big Headline" in text
    assert kw["reply_markup"].inline_keyboard[0][0].callback_data == "menu:connect"


# ── show_market_for_bet (Phase B) ──────────────────────────────────────────────

async def test_show_market_for_bet_open_renders_picker(monkeypatch):
    monkeypatch.setattr(discover.markets, "get_market_state",
                        lambda mid: ("open", markets._normalize_market(_gamma_market())))
    msg = _RecMsg()
    ctx = _ctx()
    ok = await discover.show_market_for_bet(_update(msg=msg), ctx, "0xCOND",
                                            preselect_outcome="YES", news_item_id=5,
                                            pending_intent_id=99)
    assert ok is True
    nb = ctx.user_data[discover._NEWS_BET]
    assert nb["item_id"] == 5 and nb["pending_intent_id"] == 99
    assert ctx.user_data[discover._MKTS]["0"]["yes_token"] == "tokYES"  # stashed fresh market
    assert msg.sent  # picker rendered


async def test_show_market_for_bet_closed_blocks(monkeypatch):
    monkeypatch.setattr(discover.markets, "get_market_state", lambda mid: ("closed", None))
    msg = _RecMsg()
    ok = await discover.show_market_for_bet(_update(msg=msg), _ctx(), "0xCOND", preselect_outcome="YES")
    assert ok is False
    assert "closed" in msg.sent[0][0].lower()


async def test_show_market_for_bet_transient_is_not_closed(monkeypatch):
    # a Gamma blip must NOT read as 'closed' (which on resume would kill the intent)
    monkeypatch.setattr(discover.markets, "get_market_state", lambda mid: ("error", None))
    msg = _RecMsg()
    ok = await discover.show_market_for_bet(_update(msg=msg), _ctx(), "0xCOND", preselect_outcome="YES")
    assert ok is False
    assert "closed" not in msg.sent[0][0].lower()  # shows 'try again', not 'closed'


# ── on_buy_amount: news tagging vs plain discover buy (Phase B) ────────────────

async def test_on_buy_amount_tags_news_bet_with_cap(monkeypatch):
    m = markets._normalize_market(_gamma_market())  # yes_price 0.70
    captured = {}

    async def fake_request(update, context, intent, key, **vars):
        captured["intent"] = intent

    monkeypatch.setattr(discover.confirm, "request", fake_request)
    ctx = _ctx()
    ctx.user_data[discover._GEN] = 1
    common.stash(ctx, discover._MKTS, [m])
    ctx.user_data[discover._NEWS_BET] = {"gen": 1, "item_id": 5, "pending_intent_id": 99}

    await discover.on_buy_amount(_update(query=_Query("buyamt:1:0:yes:25")), ctx)
    it = captured["intent"]
    assert it["source"] == "news" and it["news_item_id"] == 5 and it["pending_intent_id"] == 99
    assert it["market_id"] == "0xCOND" and it["entry_price"] == 0.70
    assert it["max_price"] == pytest.approx(min(0.70 * 1.05, 0.99))


async def test_on_buy_amount_news_refuses_when_unpriced(monkeypatch):
    # a news market with no price → REFUSE (never place an uncapped channel bet)
    m = markets._normalize_market(_gamma_market())
    m["yes_price"] = None
    called = {}

    async def fake_request(update, context, intent, key, **vars):
        called["hit"] = True

    monkeypatch.setattr(discover.confirm, "request", fake_request)
    ctx = _ctx()
    ctx.user_data[discover._GEN] = 1
    common.stash(ctx, discover._MKTS, [m])
    ctx.user_data[discover._NEWS_BET] = {"gen": 1, "item_id": 5, "pending_intent_id": None}
    msg = _RecMsg()
    await discover.on_buy_amount(_update(query=_Query("buyamt:1:0:yes:25"), msg=msg), ctx)
    assert "hit" not in called  # no order placed
    assert "again" in msg.sent[0][0].lower() or "couldn't" in msg.sent[0][0].lower()


async def test_on_buy_amount_plain_buy_is_not_tagged(monkeypatch):
    m = markets._normalize_market(_gamma_market())
    captured = {}

    async def fake_request(update, context, intent, key, **vars):
        captured["intent"] = intent

    monkeypatch.setattr(discover.confirm, "request", fake_request)
    ctx = _ctx()
    ctx.user_data[discover._GEN] = 1
    common.stash(ctx, discover._MKTS, [m])  # NO _NEWS_BET context → ordinary discover buy
    await discover.on_buy_amount(_update(query=_Query("buyamt:1:0:no:10")), ctx)
    it = captured["intent"]
    assert "source" not in it and "max_price" not in it  # plain market buy, unchanged behavior


# ── resume after connect (Phase C) ─────────────────────────────────────────────

async def test_resume_news_bet_requires_armed_flag(monkeypatch):
    called = {}
    monkeypatch.setattr(discover, "show_market_for_bet",
                        lambda *a, **k: called.setdefault("hit", True))
    # not armed → no-op even if a pending intent exists
    uid = await _seed_user(tg_id=701)
    async with async_session_scope() as s:
        await intents_repo.upsert_intent(s, user_id=uid, news_item_id=None, market_id="0xC", outcome="YES")
    await connect._resume_news_bet(_update(), _ctx(), chat_id=123, user_id=uid)
    assert "hit" not in called


async def test_resume_news_bet_resumes_latest_and_marks_resumed(monkeypatch):
    uid = await _seed_user(tg_id=702)
    async with async_session_scope() as s:
        row = await intents_repo.upsert_intent(s, user_id=uid, news_item_id=None, market_id="0xCOND",
                                               outcome="NO", question="Headline?")
        rid = row.id
    captured = {}

    async def fake_show(update, context, market_id, **kw):
        captured.update(market_id=market_id, **kw)
        return True  # picker rendered → intent should flip to 'resumed'

    monkeypatch.setattr(discover, "show_market_for_bet", fake_show)
    ctx = _ctx(news_bet_armed=True)
    await connect._resume_news_bet(_update(), ctx, chat_id=4242, user_id=uid)

    assert captured["market_id"] == "0xCOND" and captured["preselect_outcome"] == "NO"
    assert captured["chat_id"] == 4242 and captured["pending_intent_id"] == rid
    assert "news_bet_armed" not in ctx.user_data  # flag consumed
    async with async_session_scope() as s:
        assert (await s.get(PendingIntent, rid)).status == "resumed"


async def test_resume_leaves_intent_pending_when_market_unavailable(monkeypatch):
    # if the picker can't render (market closed / transient), the intent must NOT
    # be stranded in 'resumed' — it stays 'pending' so the TTL reaper can claim it.
    uid = await _seed_user(tg_id=703)
    async with async_session_scope() as s:
        row = await intents_repo.upsert_intent(s, user_id=uid, news_item_id=None,
                                               market_id="0xCOND", outcome="YES")
        rid = row.id

    async def fake_show(update, context, market_id, **kw):
        return False  # market closed/unavailable

    monkeypatch.setattr(discover, "show_market_for_bet", fake_show)
    ctx = _ctx(news_bet_armed=True)
    await connect._resume_news_bet(_update(), ctx, chat_id=1, user_id=uid)
    async with async_session_scope() as s:
        assert (await s.get(PendingIntent, rid)).status == "pending"
    assert "news_bet_armed" not in ctx.user_data


# ── force-confirm: a news bet never auto-places (confirm.request) ──────────────

async def test_news_bet_always_confirms_even_if_pref_off(monkeypatch):
    executed = {}

    async def fake_exec(update, context, intent, query=None):
        executed["hit"] = True

    async def pref_off(uid):
        return False

    monkeypatch.setattr(confirm, "_execute", fake_exec)
    monkeypatch.setattr(confirm, "_wants_confirmation", pref_off)
    ctx = _ctx(db_user_id=1)

    msg = _RecMsg()
    news = confirm.make_intent("market", side="buy", token_id="t", amount=25.0,
                               source="news", outcome="YES", title="Q", max_price=0.74)
    await confirm.request(_update(msg=msg), ctx, news, "bot.confirm.buy_market",
                          outcome="YES", title="Q", amount="25")
    assert "hit" not in executed                                  # forced ✅, not auto-placed
    assert msg.sent and msg.sent[0][1].get("reply_markup") is not None

    # control: an ordinary discover buy with the pref off DOES auto-place
    executed.clear()
    plain = confirm.make_intent("market", side="buy", token_id="t", amount=10.0, outcome="NO", title="Q")
    await confirm.request(_update(msg=_RecMsg()), ctx, plain, "bot.confirm.buy_market",
                          outcome="NO", title="Q", amount="10")
    assert executed.get("hit") is True


# ── bet recording on a successful news order (confirm._record_news_bet) ────────

async def test_record_news_bet_creates_settleable_bet_and_fulfills_intent():
    uid = await _seed_user(tg_id=801)
    async with async_session_scope() as s:
        row = await intents_repo.upsert_intent(s, user_id=uid, news_item_id=None, market_id="0xCOND",
                                               outcome="YES")
        pid = row.id
    intent = {"kind": "market", "side": "buy", "source": "news", "market_id": "0xCOND",
              "token_id": "tokYES", "outcome": "YES", "amount": 25.0, "entry_price": 0.70,
              "title": "Will it rain?", "pending_intent_id": pid}
    await confirm._record_news_bet(uid, None, intent, "ORDER-1")
    async with async_session_scope() as s:
        from sqlalchemy import select
        bet = await s.scalar(select(Bet))
        assert bet is not None and bet.source == "news" and bet.market_id == "0xCOND"
        assert bet.outcome == "YES" and float(bet.entry_price) == 0.70 and bet.clob_order_id == "ORDER-1"
        assert float(bet.shares) == pytest.approx(25.0 / 0.70)  # settleable (shares derived)
        assert (await s.get(PendingIntent, pid)).status == "fulfilled"
