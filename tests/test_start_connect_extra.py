"""Extra coverage for bot/handlers/start.py + bot/handlers/connect.py.

Telegram, AccountManager, polymarket.auth and gemini banners are all faked —
no network, no crypto signer, no real encryption beyond the DB upsert path
(which uses the conftest Fernet key on the temp sqlite DB).

Does NOT duplicate test_news_bet.py (start nb-routing, _open_news_bet,
connect._resume_news_bet)."""

from types import SimpleNamespace

import pytest
from telegram import InlineKeyboardMarkup
from telegram.ext import ConversationHandler

from bot.handlers import common, connect, start
from db.engine import async_session_scope
from db.models import Account, NewsItem
from db.repositories import users as users_repo
from polymarket import auth
from polymarket.auth import ConnectResult
from polymarket.credentials import PolymarketCreds


# ── fakes ────────────────────────────────────────────────────────────────────


class _RecMsg:
    """Records reply_text / reply_photo / edit_* calls."""

    def __init__(self, *, photo=None, chat_id=999):
        self.sent = []
        self.photos = []
        self.edits = []
        self.captions = []
        self.deleted = False
        self.photo = photo            # telegram.Message.photo attr (None → text path)
        self.chat_id = chat_id
        self.text = ""

    async def reply_text(self, text, **kw):
        self.sent.append((text, kw))

    async def reply_photo(self, **kw):
        self.photos.append(kw)

    async def edit_message_text(self, text, **kw):
        self.edits.append((text, kw))

    async def edit_caption(self, **kw):
        self.captions.append(kw)

    async def delete(self):
        self.deleted = True


class _Query:
    """Callback query whose .message is a plain object (NOT telegram.Message),
    so isinstance(msg, Message) is False and code falls through to reply paths."""

    def __init__(self, data, message=None):
        self.data = data
        self.message = message if message is not None else _RecMsg()
        self.answered = False

    async def answer(self, *a, **k):
        self.answered = True

    async def edit_message_text(self, text, **kw):
        self.message.edits.append((text, kw))


class _Bot:
    def __init__(self, username="the_bot"):
        self.username = username
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text, kw))


def _cb_update(data, *, message=None, tg_id=111):
    q = _Query(data, message=message)
    return SimpleNamespace(
        callback_query=q,
        effective_message=q.message,
        effective_user=SimpleNamespace(id=tg_id, first_name="Alice"),
        effective_chat=SimpleNamespace(id=q.message.chat_id),
    ), q


def _msg_update(text, *, tg_id=111, chat_id=999):
    msg = _RecMsg(chat_id=chat_id)
    return SimpleNamespace(
        callback_query=None,
        effective_message=msg,
        effective_user=SimpleNamespace(id=tg_id, first_name="Alice"),
        effective_chat=SimpleNamespace(id=chat_id),
        message=SimpleNamespace(text=text, chat_id=chat_id, delete=msg.delete),
    ), msg


def _cmd_update(*, tg_id=111):
    """Command-style update (no callback_query) with args carried on context."""
    msg = _RecMsg()
    return SimpleNamespace(
        callback_query=None,
        effective_message=msg,
        effective_user=SimpleNamespace(id=tg_id, first_name="Alice"),
        effective_chat=SimpleNamespace(id=msg.chat_id),
    ), msg


def _ctx(*, bot=None, args=None, **user_data):
    user_data.setdefault("lang", "en")
    return SimpleNamespace(
        user_data=user_data,
        bot=bot if bot is not None else _Bot(),
        application=SimpleNamespace(bot_data={}),
        args=args if args is not None else [],
    )


async def _seed_user(tg_id=111, lang="en"):
    async with async_session_scope() as s:
        u = await users_repo.get_or_create_user(
            s, telegram_id=tg_id, username="u", first_name="U", default_language=lang)
        return u.id


# ════════════════════════════ start.py ════════════════════════════════════════


def test_parse_usdc_atomic_vs_human_and_garbage():
    assert start._parse_usdc("25") == 25.0                 # already human-scale
    assert start._parse_usdc("5000000") == pytest.approx(5.0)  # > 1e6 → atomic units
    assert start._parse_usdc(None) == 0.0                  # non-numeric → 0
    assert start._parse_usdc("not-a-number") == 0.0


def test_referral_link_with_and_without_code():
    ctx = _ctx(bot=_Bot(username="MyBot"))
    assert start.referral_link(ctx, "abc") == "https://t.me/MyBot?start=r-abc"
    assert start.referral_link(ctx, None) == "https://t.me/MyBot"
    # bot with no username attr → falls back to "the_bot"
    ctx2 = SimpleNamespace(bot=SimpleNamespace())
    assert start.referral_link(ctx2, "z") == "https://t.me/the_bot?start=r-z"


def test_language_keyboard_one_button_per_supported_lang():
    kb = start.language_keyboard()
    from core.i18n import SUPPORTED
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert datas == [f"lang:{c}" for c in SUPPORTED]


def test_dashboard_keyboard_connected_vs_disconnected():
    ctx = _ctx()
    connected = start.dashboard_keyboard(ctx, connected=True)
    disc = start.dashboard_keyboard(ctx, connected=False)

    def datas(kb):
        return [b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data]

    # connected exposes accounts + settings tiles
    assert "menu:accounts" in datas(connected) and "menu:settings" in datas(connected)
    # disconnected exposes the connect + create entry buttons instead
    assert "menu:connect" in datas(disc) and "menu:create" in datas(disc)
    assert "menu:accounts" not in datas(disc)


async def test_show_dashboard_welcome_text_when_not_connected(monkeypatch):
    # no banner → deterministic text path; no account → welcome copy + connect tiles
    monkeypatch.setattr(start.gemini, "welcome_image_file", lambda: None)
    await _seed_user(tg_id=320)
    upd, msg = _cmd_update(tg_id=320)
    await start.show_dashboard(upd, _ctx(db_user_id=None))
    text, kw = msg.sent[0]
    assert isinstance(kw["reply_markup"], InlineKeyboardMarkup)
    # welcome (not connected) → there IS a connect tile in the keyboard
    datas = [b.callback_data for row in kw["reply_markup"].inline_keyboard for b in row if b.callback_data]
    assert "menu:connect" in datas


async def test_show_dashboard_connected_shows_balance(monkeypatch):
    monkeypatch.setattr(start.gemini, "welcome_image_file", lambda: None)
    uid = await _seed_user(tg_id=321)
    # give the user a connected account so the dashboard renders the connected copy
    async with async_session_scope() as s:
        s.add(Account(user_id=uid, label="Main", wallet_address="0x" + "a" * 40,
                       signature_type=0, encrypted_private_key="x", mode="live", status="active"))
    upd, msg = _cmd_update(tg_id=321)
    await start.show_dashboard(upd, _ctx(db_user_id=uid), balance=12.5)
    text, _ = msg.sent[0]
    assert "12.50" in text                       # formatted balance rendered
    assert ("a" * 40) in text                    # wallet shown


async def test_show_dashboard_banner_path(monkeypatch):
    # banner present → reply_photo used (caption-bearing), not reply_text
    import pathlib
    monkeypatch.setattr(start.gemini, "welcome_image_file", lambda: pathlib.Path("/tmp/x.png"))
    await _seed_user(tg_id=322)
    upd, msg = _cmd_update(tg_id=322)
    await start.show_dashboard(upd, _ctx(db_user_id=None))
    assert msg.photos and "caption" in msg.photos[0]
    assert msg.sent == []                          # text path NOT taken


async def test_show_dashboard_edit_path_edits_message(monkeypatch):
    monkeypatch.setattr(start.gemini, "welcome_image_file", lambda: None)
    await _seed_user(tg_id=323)
    upd, q = _cb_update("menu:home", tg_id=323)
    await start.show_dashboard(upd, _ctx(db_user_id=None), edit=True)
    # callback edit path → edit_message_text on the query, no fresh reply
    assert q.message.edits and q.message.sent == []


async def test_start_no_args_opens_dashboard(monkeypatch):
    seen = {}

    async def fake_dash(update, context, **kw):
        seen["dash"] = True

    monkeypatch.setattr(start, "show_dashboard", fake_dash)
    await start.start(_cmd_update()[0], _ctx())  # context.args missing → treated as []
    assert seen.get("dash") is True


async def test_start_attributes_referral_then_dashboard(monkeypatch):
    uid = await _seed_user(tg_id=330)
    captured = {}

    async def fake_attr(session, user, code):
        captured["code"] = code

    async def fake_dash(update, context, **kw):
        captured["dash"] = True

    monkeypatch.setattr(start.rewards_repo, "attribute_referral", fake_attr)
    monkeypatch.setattr(start, "show_dashboard", fake_dash)
    ctx = SimpleNamespace(args=["r-FRIEND"], user_data={"lang": "en", "db_user_id": uid},
                          bot=_Bot(), application=SimpleNamespace(bot_data={}))
    await start.start(_cmd_update(tg_id=330)[0], ctx)
    assert captured.get("code") == "FRIEND" and captured.get("dash") is True


async def test_start_swallows_errors_into_generic_reply(monkeypatch):
    # show_dashboard raising must surface bot.error.generic, not crash
    async def boom(update, context, **kw):
        raise RuntimeError("nope")

    monkeypatch.setattr(start, "show_dashboard", boom)
    upd, msg = _cmd_update()
    await start.start(upd, _ctx())
    assert msg.sent                                       # generic error reply sent
    assert msg.sent[0][0]                                 # non-empty error text rendered


async def test_open_news_item_routes_to_market_when_resolved(monkeypatch):
    # seed a NewsItem with a cta_market_id → routes to show_market_by_id
    async with async_session_scope() as s:
        it = NewsItem(url="https://n/1", url_hash="h-open-1", title_orig="T",
                      cta_market_id="0xMKT")
        s.add(it)
        await s.flush()
        item_id = it.id
    captured = {}

    async def fake_show(update, context, market_id):
        captured["market_id"] = market_id

    async def fake_dash(update, context, **kw):
        captured["dash"] = True

    monkeypatch.setattr(start.discover, "show_market_by_id", fake_show)
    monkeypatch.setattr(start, "show_dashboard", fake_dash)
    await start._open_news_item(_cmd_update()[0], _ctx(), item_id)
    assert captured.get("market_id") == "0xMKT" and "dash" not in captured


async def test_open_news_item_missing_item_falls_back_to_dashboard(monkeypatch):
    captured = {}

    async def fake_dash(update, context, **kw):
        captured["dash"] = True

    monkeypatch.setattr(start, "show_dashboard", fake_dash)
    # no NewsItem with id 999999 → market_id None → dashboard fallback
    await start._open_news_item(_cmd_update()[0], _ctx(), 999999)
    assert captured.get("dash") is True


async def test_on_language_choice_sets_language_and_renders(monkeypatch):
    monkeypatch.setattr(start.gemini, "welcome_image_file", lambda: None)
    uid = await _seed_user(tg_id=340, lang="en")
    upd, q = _cb_update("lang:fa", tg_id=340)
    ctx = _ctx(db_user_id=uid, lang="en")
    await start.on_language_choice(upd, ctx)
    # user_data updated + persisted
    assert ctx.user_data["lang"] == "fa"
    async with async_session_scope() as s:
        u = await users_repo.get_user(s, 340)
        assert u.language == "fa"
    assert q.answered is True
    assert q.message.sent          # "language set" confirmation + dashboard


async def test_on_language_choice_ignores_unsupported(monkeypatch):
    uid = await _seed_user(tg_id=341, lang="en")
    upd, q = _cb_update("lang:zz", tg_id=341)
    ctx = _ctx(db_user_id=uid, lang="en")
    await start.on_language_choice(upd, ctx)
    # unsupported code → early return, language unchanged, no confirmation sent
    assert ctx.user_data["lang"] == "en"
    assert q.message.sent == []


async def test_on_language_choice_no_query_is_noop():
    upd = SimpleNamespace(callback_query=None)
    await start.on_language_choice(upd, _ctx())  # returns immediately, no raise


async def test_on_menu_dispatches_home_to_dashboard(monkeypatch):
    seen = {}

    async def fake_dash(update, context, *, balance=None, edit=False):
        seen["edit"] = edit

    monkeypatch.setattr(start, "show_dashboard", fake_dash)
    upd, q = _cb_update("menu:home")
    await start.on_menu(upd, _ctx())
    assert seen.get("edit") is True and q.answered is True


async def test_on_menu_refresh_pulls_balance(monkeypatch):
    seen = {}

    async def fake_dash(update, context, *, balance=None, edit=False):
        seen["balance"] = balance

    class _PM:
        def get_balance(self):
            return {"balance": "5000000"}      # atomic → 5.0 USDC

    class _Mgr:
        async def get_trading_client(self, uid):
            return _PM()

    monkeypatch.setattr(start, "show_dashboard", fake_dash)
    monkeypatch.setattr(start.common, "manager", lambda ctx: _Mgr())
    upd, _ = _cb_update("menu:refresh")
    await start.on_menu(upd, _ctx(db_user_id=7))
    assert seen.get("balance") == pytest.approx(5.0)


async def test_on_menu_refresh_balance_unavailable_is_none(monkeypatch):
    seen = {}

    async def fake_dash(update, context, *, balance=None, edit=False):
        seen["balance"] = balance

    class _Mgr:
        async def get_trading_client(self, uid):
            raise RuntimeError("no account")

    monkeypatch.setattr(start, "show_dashboard", fake_dash)
    monkeypatch.setattr(start.common, "manager", lambda ctx: _Mgr())
    upd, _ = _cb_update("menu:refresh")
    await start.on_menu(upd, _ctx(db_user_id=7))
    assert seen.get("balance") is None       # swallowed → no balance shown


async def test_on_menu_trending_routes_to_discover(monkeypatch):
    seen = {}

    async def fake_trending(update, context):
        seen["hit"] = True

    monkeypatch.setattr(start.discover, "trending", fake_trending)
    upd, _ = _cb_update("menu:trending")
    await start.on_menu(upd, _ctx())
    assert seen.get("hit") is True


async def test_on_menu_create_sends_instructions():
    upd, q = _cb_update("menu:create")
    await start.on_menu(upd, _ctx())
    # create reply uses query.message.reply_text with the create keyboard
    assert q.message.sent
    kb = q.message.sent[0][1]["reply_markup"]
    datas = [b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data]
    assert "menu:connect" in datas


async def test_on_menu_buy_shows_trade_hint():
    upd, q = _cb_update("menu:buy")
    await start.on_menu(upd, _ctx())
    assert q.message.sent       # trade hint with nav keyboard


async def test_on_menu_no_query_is_noop():
    upd = SimpleNamespace(callback_query=None)
    await start.on_menu(upd, _ctx())  # returns immediately


async def test_on_menu_swallows_handler_error(monkeypatch):
    async def boom(update, context):
        raise RuntimeError("boom")

    monkeypatch.setattr(start.discover, "trending", boom)
    upd, q = _cb_update("menu:trending")
    await start.on_menu(upd, _ctx())
    # the except branch replies bot.error.generic via common.reply (effective_message)
    assert q.message.sent


async def test_rewards_screen_renders_with_share_button(monkeypatch):
    uid = await _seed_user(tg_id=350)
    upd, q = _cb_update("menu:rewards", tg_id=350)
    await start.rewards_screen(upd, _ctx(db_user_id=uid, bot=_Bot(username=" B")))
    assert q.message.sent
    kb = q.message.sent[0][1]["reply_markup"]
    # a Share button (url to t.me/share/url) is present in the keyboard
    urls = [b.url for row in kb.inline_keyboard for b in row if getattr(b, "url", None)]
    assert any("share" in u for u in urls)


async def test_settings_prompts_language_keyboard():
    upd, q = _cb_update("menu:settings")
    await start._settings(upd, _ctx())
    kb = q.message.sent[0][1]["reply_markup"]
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert all(d.startswith("lang:") for d in datas)


async def test_accounts_empty_shows_none(monkeypatch):
    class _Mgr:
        async def list_accounts(self, uid):
            return []

    monkeypatch.setattr(start.common, "manager", lambda ctx: _Mgr())
    upd, q = _cb_update("menu:accounts")
    await start._accounts(upd, _ctx(db_user_id=5))
    assert q.message.sent      # "no accounts" message


async def test_accounts_lists_with_disconnect_buttons(monkeypatch):
    accts = [SimpleNamespace(account_id=1, label="A", wallet_address="0x" + "a" * 40, mode="live")]

    class _Mgr:
        async def list_accounts(self, uid):
            return accts

    monkeypatch.setattr(start.common, "manager", lambda ctx: _Mgr())
    upd, q = _cb_update("menu:accounts")
    await start._accounts(upd, _ctx(db_user_id=5))
    kb = q.message.sent[0][1]["reply_markup"]
    datas = [b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data]
    assert any(d == "disc:1" for d in datas)


# ════════════════════════════ connect.py ══════════════════════════════════════


def test_clear_connect_zeroizes_key():
    ctx = _ctx(connect={"sig_type": 0, "key": "0x" + "1" * 64})
    connect._clear_connect(ctx)
    assert "connect" not in ctx.user_data


def test_short_address_abbreviation():
    assert connect._short("0x" + "a" * 40) == "0xaaaa…aaaa"
    assert connect._short("short") == "short"   # too short → unchanged
    assert connect._short("") == ""


def test_type_keyboard_offers_three_signature_types():
    kb = connect._type_keyboard(_ctx())
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert set(datas) == {"ctype:0", "ctype:1", "ctype:2"}


async def test_start_connect_pops_awaiting_bet_and_prompts():
    upd, q = _cb_update("menu:connect")
    ctx = _ctx(awaiting_bet={"side": "yes"})
    state = await connect.start_connect(upd, ctx)
    assert state == connect.CHOOSE_TYPE
    assert "awaiting_bet" not in ctx.user_data        # disarmed (key-safety)
    assert ctx.user_data["connect"] == {}             # fresh transient state
    assert q.answered is True
    assert q.message.sent                              # wallet-type prompt shown
    kb = q.message.sent[0][1]["reply_markup"]
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "ctype:0" in datas


async def test_choose_type_eoa_goes_to_key():
    upd, q = _cb_update("ctype:0")
    ctx = _ctx()
    state = await connect.choose_type(upd, ctx)
    assert state == connect.ENTER_KEY
    assert ctx.user_data["connect"] == {"sig_type": 0}
    assert q.message.edits                             # enter-key prompt edited in


async def test_choose_type_proxy_asks_for_funder():
    upd, q = _cb_update("ctype:1")
    ctx = _ctx()
    state = await connect.choose_type(upd, ctx)
    assert state == connect.ENTER_FUNDER               # proxy → funder first
    assert ctx.user_data["connect"]["sig_type"] == 1


async def test_choose_type_malformed_defaults_to_eoa():
    upd, q = _cb_update("ctype:bogus")
    ctx = _ctx()
    state = await connect.choose_type(upd, ctx)
    assert state == connect.ENTER_KEY                  # unparseable → sig_type 0
    assert ctx.user_data["connect"]["sig_type"] == 0


async def test_enter_funder_rejects_malformed_address():
    upd, msg = _msg_update("not-an-address")
    ctx = _ctx(connect={"sig_type": 1})
    state = await connect.enter_funder(upd, ctx)
    assert state == connect.ENTER_FUNDER               # stays put, asks again
    assert msg.sent                                    # bad_address reply
    assert "funder" not in ctx.user_data["connect"]


async def test_enter_funder_accepts_valid_address_advances_to_key():
    good = "0x" + "b" * 40
    upd, msg = _msg_update(good)
    ctx = _ctx(connect={"sig_type": 2})
    state = await connect.enter_funder(upd, ctx)
    assert state == connect.ENTER_KEY
    assert ctx.user_data["connect"]["funder"] == good


async def test_conn_nav_cancel_ends_and_clears():
    upd, q = _cb_update("conn:cancel")
    ctx = _ctx(connect={"sig_type": 0, "key": "0x" + "1" * 64})
    state = await connect.conn_nav(upd, ctx)
    assert state == ConversationHandler.END
    assert "connect" not in ctx.user_data              # key state zeroized


async def test_conn_nav_to_type_resets_state():
    upd, q = _cb_update("conn:to_type")
    ctx = _ctx(connect={"sig_type": 1, "funder": "0x" + "a" * 40})
    state = await connect.conn_nav(upd, ctx)
    assert state == connect.CHOOSE_TYPE
    assert ctx.user_data["connect"] == {}              # back-to-start clears funder/type


async def test_conn_nav_to_funder_returns_to_funder_step():
    upd, q = _cb_update("conn:to_funder")
    ctx = _ctx(connect={"sig_type": 1})
    state = await connect.conn_nav(upd, ctx)
    assert state == connect.ENTER_FUNDER


async def test_conn_nav_unknown_action_returns_none():
    upd, q = _cb_update("conn:bogus")
    state = await connect.conn_nav(upd, _ctx(connect={}))
    assert state is None                               # leave conversation as-is


async def test_enter_key_deletes_message_and_rejects_bad_key():
    upd, msg = _msg_update("garbage-not-a-key")
    bot = _Bot()
    ctx = _ctx(bot=bot, connect={"sig_type": 0})
    state = await connect.enter_key(upd, ctx)
    assert state == connect.ENTER_KEY                  # bad key → stay, ask again
    assert msg.deleted is True                         # SECURITY: inbound deleted first
    # a bad_key message was sent via bot.send_message (to the message's chat)
    assert bot.sent and bot.sent[0][0] == msg.chat_id


async def test_enter_key_validation_failure_returns_retry(monkeypatch):
    upd, msg = _msg_update("0x" + "1" * 64)
    bot = _Bot()
    ctx = _ctx(bot=bot, connect={"sig_type": 0})

    def boom(**kw):
        raise auth.ConnectError("bad creds")

    monkeypatch.setattr(connect.auth, "validate_and_derive", boom)
    state = await connect.enter_key(upd, ctx)
    assert state == connect.RETRY
    assert msg.deleted is True
    # key zeroized from transient state in all exit paths
    assert ctx.user_data["connect"].get("key") in (None,)


async def test_enter_key_no_user_id_ends(monkeypatch):
    upd, msg = _msg_update("0x" + "1" * 64)
    bot = _Bot()
    ctx = _ctx(bot=bot, connect={"sig_type": 0})   # NO db_user_id

    creds = PolymarketCreds(wallet_address="0x" + "c" * 40, signature_type=0,
                            private_key="0x" + "1" * 64, api_key="k",
                            api_secret="s", api_passphrase="p")

    def ok(**kw):
        return ConnectResult(creds=creds, balance_usdc=0.0)

    monkeypatch.setattr(connect.auth, "validate_and_derive", ok)
    state = await connect.enter_key(upd, ctx)
    assert state == ConversationHandler.END        # no account context → bail
    assert ctx.user_data["connect"].get("key") in (None,)


async def test_enter_key_success_stores_account_and_shows_balance(monkeypatch):
    uid = await _seed_user(tg_id=400)
    wallet = "0x" + "d" * 40
    creds = PolymarketCreds(wallet_address=wallet, signature_type=0,
                            private_key="0x" + "2" * 64, api_key="k",
                            api_secret="s", api_passphrase="p")

    def ok(**kw):
        # assert auth is called with the normalized key + derive-from-key (wallet None)
        assert kw["wallet_address"] is None
        assert kw["signature_type"] == 0
        return ConnectResult(creds=creds, balance_usdc=42.0)

    monkeypatch.setattr(connect.auth, "validate_and_derive", ok)

    invalidated = {}

    class _Mgr:
        def invalidate(self, uid, account_id=None):
            invalidated["uid"] = uid

    monkeypatch.setattr(connect.common, "manager", lambda ctx: _Mgr())

    upd, msg = _msg_update("0x" + "2" * 64, tg_id=400)
    bot = _Bot()
    ctx = _ctx(bot=bot, db_user_id=uid, connect={"sig_type": 0})
    state = await connect.enter_key(upd, ctx)

    assert state == ConversationHandler.END
    assert msg.deleted is True                        # inbound key message deleted
    assert invalidated.get("uid") == uid              # cache invalidated post-store
    # account persisted to the DB
    async with async_session_scope() as s:
        from sqlalchemy import select
        acc = await s.scalar(select(Account).where(Account.user_id == uid))
        assert acc is not None and acc.wallet_address == wallet
    # success message (with balance) sent via bot.send_message; key gone from state
    assert ctx.user_data["connect"].get("key") in (None,)
    assert any("42" in text or "42.00" in text for _cid, text, _kw in bot.sent)


async def test_enter_key_persistence_failure_returns_retry(monkeypatch):
    uid = await _seed_user(tg_id=401)
    creds = PolymarketCreds(wallet_address="0x" + "e" * 40, signature_type=0,
                            private_key="0x" + "3" * 64, api_key="k",
                            api_secret="s", api_passphrase="p")
    monkeypatch.setattr(connect.auth, "validate_and_derive",
                        lambda **kw: ConnectResult(creds=creds, balance_usdc=1.0))

    async def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(connect.accounts_repo, "upsert_account", boom)
    upd, msg = _msg_update("0x" + "3" * 64, tg_id=401)
    ctx = _ctx(db_user_id=uid, connect={"sig_type": 0})
    state = await connect.enter_key(upd, ctx)
    assert state == connect.RETRY                     # persistence failed → retry
    assert ctx.user_data["connect"].get("key") in (None,)


async def test_cancel_command_ends_and_clears():
    upd, msg = _msg_update("/cancel")
    ctx = _ctx(connect={"sig_type": 0, "key": "0x" + "1" * 64})
    state = await connect.cancel(upd, ctx)
    assert state == ConversationHandler.END
    assert "connect" not in ctx.user_data
    assert msg.sent                                   # cancelled message


async def test_on_timeout_deletes_message_and_clears():
    upd, msg = _msg_update("0x" + "1" * 64)
    bot = _Bot()
    ctx = _ctx(bot=bot, connect={"sig_type": 0, "key": "0x" + "1" * 64})
    state = await connect.on_timeout(upd, ctx)
    assert state == ConversationHandler.END
    assert msg.deleted is True                        # SECURITY: delete pasted key
    assert "connect" not in ctx.user_data
    assert bot.sent                                   # timeout notice sent


async def test_disconnect_cmd_no_account_replies(monkeypatch):
    upd, msg = _msg_update("/disconnect")
    ctx = _ctx()  # no db_user_id
    await connect.disconnect_cmd(upd, ctx)
    assert msg.sent                                   # no_account reply


async def test_disconnect_cmd_none_when_no_accounts(monkeypatch):
    class _Mgr:
        async def list_accounts(self, uid):
            return []

    monkeypatch.setattr(connect.common, "manager", lambda ctx: _Mgr())
    upd, msg = _msg_update("/disconnect")
    await connect.disconnect_cmd(upd, _ctx(db_user_id=5))
    assert msg.sent                                   # "none connected"


async def test_disconnect_cmd_lists_accounts(monkeypatch):
    accts = [SimpleNamespace(account_id=3, label="Main", wallet_address="0x" + "a" * 40)]

    class _Mgr:
        async def list_accounts(self, uid):
            return accts

    monkeypatch.setattr(connect.common, "manager", lambda ctx: _Mgr())
    upd, msg = _msg_update("/disconnect")
    await connect.disconnect_cmd(upd, _ctx(db_user_id=5))
    kb = msg.sent[0][1]["reply_markup"]
    datas = [b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data]
    assert "disc:3" in datas


async def test_disconnect_cmd_list_failure_replies_generic(monkeypatch):
    class _Mgr:
        async def list_accounts(self, uid):
            raise RuntimeError("boom")

    monkeypatch.setattr(connect.common, "manager", lambda ctx: _Mgr())
    upd, msg = _msg_update("/disconnect")
    await connect.disconnect_cmd(upd, _ctx(db_user_id=5))
    assert msg.sent                                   # generic error reply


async def test_on_disconnect_asks_confirmation(monkeypatch):
    accts = [SimpleNamespace(account_id=9, label="Main", wallet_address="0x" + "f" * 40)]

    class _Mgr:
        async def list_accounts(self, uid):
            return accts

    monkeypatch.setattr(connect.common, "manager", lambda ctx: _Mgr())
    upd, q = _cb_update("disc:9")
    await connect.on_disconnect(upd, _ctx(db_user_id=5))
    # confirmation keyboard: discok:9 (yes) + discno (no)
    kb = q.message.edits[0][1]["reply_markup"]
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "discok:9" in datas and "discno" in datas


async def test_on_disconnect_unknown_account_says_none(monkeypatch):
    class _Mgr:
        async def list_accounts(self, uid):
            return []          # account_id 9 not present

    monkeypatch.setattr(connect.common, "manager", lambda ctx: _Mgr())
    upd, q = _cb_update("disc:9")
    await connect.on_disconnect(upd, _ctx(db_user_id=5))
    assert q.message.edits     # edited to "none" message


async def test_on_disconnect_bad_callback_data_replies_generic():
    upd, q = _cb_update("disc:notanint")
    await connect.on_disconnect(upd, _ctx(db_user_id=5))
    assert q.message.sent      # generic error via message.reply_text


async def test_on_disconnect_cancel_edits_message():
    upd, q = _cb_update("discno")
    await connect.on_disconnect_cancel(upd, _ctx())
    assert q.message.edits     # cancelled message edited in
    assert q.answered is True


async def test_on_disconnect_confirmed_deletes_account_and_audits(monkeypatch):
    # Regression for the audit-FK disconnect bug: on_disconnect_confirmed must
    # actually delete the account (and its encrypted key). The audit row records the
    # account id in `detail` rather than the account_id FK column, so deleting the
    # account in the SAME transaction can't trip the FK and roll back the disconnect.
    from db.models import AuditLog
    from sqlalchemy import select

    uid = await _seed_user(tg_id=410)
    async with async_session_scope() as s:
        acc = Account(user_id=uid, label="Main", wallet_address="0x" + "9" * 40,
                      signature_type=0, encrypted_private_key="x", mode="live", status="active")
        s.add(acc)
        await s.flush()
        acc_id = acc.id

    invalidated = {}

    class _Mgr:
        def invalidate(self, uid, account_id=None):
            invalidated["args"] = (uid, account_id)

    monkeypatch.setattr(connect.common, "manager", lambda ctx: _Mgr())
    upd, q = _cb_update(f"discok:{acc_id}", tg_id=410)
    await connect.on_disconnect_confirmed(upd, _ctx(db_user_id=uid))

    # Account is gone, the cache was invalidated, and a "done" reply was sent.
    async with async_session_scope() as s:
        assert await s.get(Account, acc_id) is None
        audit_row = await s.scalar(select(AuditLog).where(AuditLog.event == "ACCOUNT_DISCONNECTED"))
        assert audit_row is not None
        assert audit_row.account_id is None                  # not the FK (account was deleted)
        assert audit_row.detail.get("account_id") == acc_id  # id preserved in detail
    assert invalidated.get("args") == (uid, acc_id)          # invalidate() reached
    assert q.message.sent                                    # success reply sent


async def test_on_disconnect_confirmed_missing_account_says_none(monkeypatch):
    uid = await _seed_user(tg_id=411)

    class _Mgr:
        def invalidate(self, *a, **k):
            pass

    monkeypatch.setattr(connect.common, "manager", lambda ctx: _Mgr())
    upd, q = _cb_update("discok:987654", tg_id=411)   # no such account
    await connect.on_disconnect_confirmed(upd, _ctx(db_user_id=uid))
    assert q.message.sent       # "none" — nothing deleted


async def test_on_disconnect_confirmed_no_user_id_replies():
    upd, q = _cb_update("discok:1")
    await connect.on_disconnect_confirmed(upd, _ctx())  # no db_user_id
    assert q.message.sent       # no_account reply


async def test_on_disconnect_confirmed_bad_data_replies_generic():
    upd, q = _cb_update("discok:notanint")
    await connect.on_disconnect_confirmed(upd, _ctx(db_user_id=5))
    assert q.message.sent       # generic error
