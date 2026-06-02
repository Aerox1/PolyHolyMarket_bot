"""Connect rework: the signer address is derived from the key (no user-typed
address), and for proxy/Safe the stored account address is the funder. Plus the
new choose_type routing (EOA → key, proxy/Safe → funder)."""

from types import SimpleNamespace

import pytest

from polymarket import auth
from polymarket.credentials import WalletMismatchError

_SIGNER = "0x" + "a" * 40
_KEY = "0x" + "b" * 64


class _FakeClob:
    def __init__(self, **kw):
        self.kw = kw

    def get_address(self):
        return _SIGNER

    def create_or_derive_api_creds(self):
        return SimpleNamespace(api_key="k", api_secret="s", api_passphrase="p")

    def set_api_creds(self, creds):
        pass

    def get_balance_allowance(self, params):
        return {"balance": "5000000"}  # 5 USDC in atomic units


@pytest.fixture(autouse=True)
def _fake_clob(monkeypatch):
    monkeypatch.setattr(auth, "ClobClient", _FakeClob)


# ── address derivation ────────────────────────────────────────────────────────

def test_eoa_derives_account_from_key():
    res = auth.validate_and_derive(private_key=_KEY, wallet_address=None, signature_type=0, funder_address=None)
    assert res.creds.wallet_address == _SIGNER     # derived from the key
    assert res.creds.funder_address is None
    assert res.balance_usdc == 5.0
    assert res.creds.has_api_creds


def test_proxy_uses_funder_as_account_address():
    funder = "0x" + "c" * 40
    res = auth.validate_and_derive(private_key=_KEY, wallet_address=None, signature_type=1, funder_address=funder)
    # account address (positions/balance/display) = the funder, NOT the signer EOA
    assert res.creds.wallet_address == funder
    assert res.creds.funder_address == funder
    assert res.creds.signature_type == 1


def test_explicit_wallet_address_still_mismatch_checked():
    # backward compat: if an address IS supplied and disagrees with the key → error
    with pytest.raises(WalletMismatchError):
        auth.validate_and_derive(private_key=_KEY, wallet_address="0x" + "d" * 40,
                                 signature_type=0, funder_address=None)


# ── choose_type routing ───────────────────────────────────────────────────────

class _Query:
    def __init__(self, data):
        self.data = data
        self.edited = None

    async def answer(self):
        pass

    async def edit_message_text(self, text, **kw):
        self.edited = text


def _ctx():
    return SimpleNamespace(user_data={"lang": "en"})


async def test_choose_type_eoa_goes_straight_to_key():
    from bot.handlers import connect
    q = _Query("ctype:0")
    state = await connect.choose_type(SimpleNamespace(callback_query=q), _ctx())
    assert state == connect.ENTER_KEY  # EOA needs only the key


async def test_choose_type_proxy_asks_funder_first():
    from bot.handlers import connect
    q = _Query("ctype:1")
    state = await connect.choose_type(SimpleNamespace(callback_query=q), _ctx())
    assert state == connect.ENTER_FUNDER


async def test_conn_nav_cancel_ends_conversation():
    from telegram.ext import ConversationHandler

    from bot.handlers import connect
    ctx = _ctx()
    ctx.user_data["connect"] = {"sig_type": 1}
    q = _Query("conn:cancel")
    state = await connect.conn_nav(SimpleNamespace(callback_query=q, message=SimpleNamespace()), ctx)
    assert state == ConversationHandler.END
    assert "connect" not in ctx.user_data  # transient state cleared


def test_type_keyboard_lists_email_magic_first():
    # the most-common (Email/Magic = proxy, ctype:1) option must be the top button
    from bot.handlers import connect
    kb = connect._type_keyboard(_ctx())
    assert kb.inline_keyboard[0][0].callback_data == "ctype:1"


async def test_timeout_deletes_inbound_key_message():
    # BLOCKER guard: a key pasted just as the conversation times out must still be
    # deleted from the chat (mirrors enter_key's delete-first).
    from telegram.ext import ConversationHandler

    from bot.handlers import connect
    deleted = {"called": False}

    class _Msg:
        async def delete(self):
            deleted["called"] = True

    class _Bot:
        async def send_message(self, **kw):
            pass

    ctx = SimpleNamespace(user_data={"lang": "en", "connect": {"key": "0x" + "f" * 64}}, bot=_Bot())
    update = SimpleNamespace(message=_Msg(), effective_chat=SimpleNamespace(id=1))
    state = await connect.on_timeout(update, ctx)
    assert deleted["called"] is True
    assert state == ConversationHandler.END
    assert "connect" not in ctx.user_data  # transient key state cleared
