"""Invite-code access gate: code parsing, grant logic (global code + referral code),
and middleware enforcement (locked user blocked → unlocks on a valid code)."""

from types import SimpleNamespace

import pytest

from bot import access_gate, middleware
from db.engine import async_session_scope
from db.repositories import appconfig
from db.repositories import rewards as rewards_repo
from db.repositories import users as users_repo


# ── code_from_update ──────────────────────────────────────────────────────────

def _upd(text):
    return SimpleNamespace(message=SimpleNamespace(text=text), callback_query=None,
                           effective_message=None)


def test_code_from_update():
    assert access_gate.code_from_update(_upd("POLYHOLY")) == "POLYHOLY"
    assert access_gate.code_from_update(_upd("  alice123 ")) == "alice123"
    assert access_gate.code_from_update(_upd("/start r-bob")) == "bob"   # invite deep-link
    assert access_gate.code_from_update(_upd("/start")) is None          # bare command
    assert access_gate.code_from_update(_upd("/help")) is None
    # a callback (no .message) offers no code
    assert access_gate.code_from_update(
        SimpleNamespace(message=None, callback_query=object(), effective_message=None)) is None


# ── try_grant ─────────────────────────────────────────────────────────────────

async def _new_user(tg_id):
    async with async_session_scope() as s:
        u = await users_repo.get_or_create_user(s, telegram_id=tg_id, username=None,
                                                first_name=None, default_language="en")
        return u.id


async def test_try_grant_global_code_case_insensitive():
    async with async_session_scope() as s:
        u = await users_repo.get_or_create_user(s, telegram_id=8101, username=None,
                                                first_name=None, default_language="en")
        assert u.access_granted is False
        assert await access_gate.try_grant(s, u, "polyholy") is True   # default POLYHOLY, case-insensitive
        assert u.access_granted is True


async def test_try_grant_referral_code_grants_and_credits_referrer():
    async with async_session_scope() as s:
        ref = await users_repo.get_or_create_user(s, telegram_id=8102, username="ref",
                                                  first_name="R", default_language="en")
        code = await rewards_repo.ensure_referral_code(s, ref)
        invitee = await users_repo.get_or_create_user(s, telegram_id=8103, username=None,
                                                      first_name=None, default_language="en")
        assert await access_gate.try_grant(s, invitee, code) is True
        assert invitee.access_granted is True
        assert invitee.referred_by == ref.id   # referral attributed (referrer credited)


async def test_try_grant_invalid_code():
    async with async_session_scope() as s:
        u = await users_repo.get_or_create_user(s, telegram_id=8104, username=None,
                                                first_name=None, default_language="en")
        assert await access_gate.try_grant(s, u, "definitely-not-a-code") is False
        assert u.access_granted is False


# ── middleware enforcement ────────────────────────────────────────────────────

class _RecMsg:
    def __init__(self, text=None):
        self.text = text
        self.sent: list[str] = []

    async def reply_text(self, text, **kw):
        self.sent.append(text)


def _mw_update(uid, text):
    msg = _RecMsg(text)
    upd = SimpleNamespace(
        effective_user=SimpleNamespace(id=uid, is_bot=False, username="u", first_name="U"),
        effective_message=msg, message=msg, callback_query=None,
        effective_chat=SimpleNamespace(id=uid))
    return upd, msg


async def _enable_gate():
    async with async_session_scope() as s:
        await appconfig.set_(s, appconfig.ACCESS_GATE_ENABLED, "1")


async def test_middleware_gate_blocks_then_unlocks(monkeypatch):
    monkeypatch.setattr(middleware.settings, "middleware_sync_seconds", 0.0)  # always re-read DB
    await _enable_gate()
    ctx = SimpleNamespace(user_data={})

    # 1) locked new user sends a command → blocked + PROMPT
    upd, msg = _mw_update(8201, "/portfolio")
    with pytest.raises(middleware.ApplicationHandlerStop):
        await middleware.preprocess(upd, ctx)
    assert any("invite-only" in m for m in msg.sent)

    # 2) wrong code → blocked + INVALID
    upd2, msg2 = _mw_update(8201, "wrongcode")
    with pytest.raises(middleware.ApplicationHandlerStop):
        await middleware.preprocess(upd2, ctx)
    assert any("isn't valid" in m for m in msg2.sent)
    assert ctx.user_data.get("_access_granted") is not True

    # 3) correct global code → GRANTED (still stops this update, but access is flagged)
    upd3, msg3 = _mw_update(8201, "POLYHOLY")
    with pytest.raises(middleware.ApplicationHandlerStop):
        await middleware.preprocess(upd3, ctx)
    assert any("You're in" in m for m in msg3.sent)
    assert ctx.user_data.get("_access_granted") is True

    # 4) now unlocked → a later update passes through (no stop)
    upd4, _ = _mw_update(8201, "/portfolio")
    await middleware.preprocess(upd4, ctx)  # must NOT raise


async def test_middleware_gate_off_lets_new_user_through(monkeypatch):
    monkeypatch.setattr(middleware.settings, "middleware_sync_seconds", 0.0)
    async with async_session_scope() as s:
        await appconfig.set_(s, appconfig.ACCESS_GATE_ENABLED, "0")  # gate OFF
    upd, msg = _mw_update(8202, "/portfolio")
    await middleware.preprocess(upd, SimpleNamespace(user_data={}))  # no raise, no prompt
    assert msg.sent == []
