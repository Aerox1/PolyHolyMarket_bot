"""Infra coverage: db/bootstrap.py (admin bootstrap), bot/main.py
(build_application + post hooks), bot/middleware.py (status gate + caching).

No network. bootstrap uses the sync session_scope (conftest temp DB); middleware
is exercised with the SimpleNamespace fakes from tests/test_perf.py.
"""

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from db import bootstrap
from db.engine import SessionLocal
from db.models import Admin, UserStatus
from bot import main as bot_main
from bot import middleware


# ── db/bootstrap.bootstrap_admin ────────────────────────────────────────────

def _admin_count() -> int:
    with SessionLocal() as s:
        return len(list(s.scalars(select(Admin))))


def test_bootstrap_admin_empty_hash_noop(monkeypatch):
    # No ADMIN_BOOTSTRAP_PASSWORD_HASH → early return, no Admin row created.
    monkeypatch.setattr(bootstrap.settings, "admin_bootstrap_password_hash", "")
    bootstrap.bootstrap_admin()
    assert _admin_count() == 0


def test_bootstrap_admin_creates_when_none(monkeypatch):
    # Hash set + empty admins table → creates one superadmin with the env username.
    monkeypatch.setattr(bootstrap.settings, "admin_bootstrap_password_hash", "hashed-pw")
    monkeypatch.setattr(bootstrap.settings, "admin_bootstrap_user", "rootadmin")
    bootstrap.bootstrap_admin()
    with SessionLocal() as s:
        admins = list(s.scalars(select(Admin)))
    assert len(admins) == 1
    a = admins[0]
    assert a.username == "rootadmin"
    assert a.password_hash == "hashed-pw"
    assert a.is_superadmin is True


def test_bootstrap_admin_idempotent_when_present(monkeypatch):
    # An admin already exists → bootstrap must NOT add a second.
    with SessionLocal() as s:
        s.add(Admin(username="existing", password_hash="x", is_superadmin=False))
        s.commit()
    monkeypatch.setattr(bootstrap.settings, "admin_bootstrap_password_hash", "hashed-pw")
    monkeypatch.setattr(bootstrap.settings, "admin_bootstrap_user", "rootadmin")
    bootstrap.bootstrap_admin()
    with SessionLocal() as s:
        admins = list(s.scalars(select(Admin)))
    assert len(admins) == 1
    assert admins[0].username == "existing"  # untouched


# ── bot/main.build_application + post hooks ──────────────────────────────────

def test_build_application_wires_manager_and_handlers():
    # TELEGRAM_BOT_TOKEN is "test-token" in conftest → builds a real Application.
    app = bot_main.build_application()
    assert app is not None
    assert app.bot_data.get("account_manager") is not None
    # Middleware (group -1) plus every module's handlers were registered.
    assert app.handlers  # non-empty handler registry
    assert -1 in app.handlers  # middleware lives in group -1


def test_build_application_requires_token(monkeypatch):
    monkeypatch.setattr(bot_main.settings, "telegram_bot_token", "")
    with pytest.raises(RuntimeError):
        bot_main.build_application()


async def test_on_error_just_logs():
    # The error handler logs the error type; it must never re-raise.
    ctx = SimpleNamespace(error=RuntimeError("boom"))
    assert await bot_main._on_error(None, ctx) is None


async def test_post_shutdown_clears_manager():
    cleared = {"n": 0}

    class _Mgr:
        def clear(self):
            cleared["n"] += 1

    app = SimpleNamespace(bot_data={"account_manager": _Mgr()})
    await bot_main._post_shutdown(app)
    assert cleared["n"] == 1


async def test_post_shutdown_no_manager_noop():
    # No account_manager in bot_data → nothing to clear, no error.
    app = SimpleNamespace(bot_data={})
    assert await bot_main._post_shutdown(app) is None


async def test_post_init_swallows_set_commands_error():
    class _Bot:
        async def set_my_commands(self, commands):
            raise RuntimeError("network down")

    app = SimpleNamespace(bot=_Bot())
    # set_my_commands raising must be swallowed (non-fatal startup path).
    assert await bot_main._post_init(app) is None


async def test_post_init_sets_commands_happy():
    seen = {}

    class _Bot:
        async def set_my_commands(self, commands):
            seen["cmds"] = commands

    app = SimpleNamespace(bot=_Bot())
    await bot_main._post_init(app)
    assert seen["cmds"] is bot_main.COMMANDS


# ── bot/middleware.preprocess ────────────────────────────────────────────────

def _upd(uid: int, *, is_bot: bool = False, with_msg: bool = True):
    msg = _RecMsg() if with_msg else None
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=uid, is_bot=is_bot, username="u", first_name="U"),
        effective_message=msg,
        effective_chat=SimpleNamespace(id=uid),
    )


def _ctx(**ud):
    return SimpleNamespace(user_data=dict(ud))


class _RecMsg:
    def __init__(self):
        self.sent = []

    async def reply_text(self, text, **kw):
        self.sent.append((text, kw))


def _stub_user(uid, status, language="en"):
    return SimpleNamespace(id=uid, language=language, status=status, last_seen_at=None)


async def test_middleware_caches_lang_and_uid_on_first_sync(monkeypatch):
    monkeypatch.setattr(middleware.settings, "middleware_sync_seconds", 60.0)
    calls = {"n": 0}

    async def fake_get_or_create(session, **kw):
        calls["n"] += 1
        return _stub_user(uid=4242, status=UserStatus.ACTIVE.value, language="fa")

    monkeypatch.setattr(middleware.users_repo, "get_or_create_user", fake_get_or_create)

    upd, ctx = _upd(4242), _ctx()
    await middleware.preprocess(upd, ctx)
    # Internal id + language cached from the DB user on first sync.
    assert ctx.user_data["db_user_id"] == 4242
    assert ctx.user_data["lang"] == "fa"
    assert ctx.user_data["_status"] == UserStatus.ACTIVE.value
    assert "_db_sync_at" in ctx.user_data
    assert calls["n"] == 1
    # Active user is not gated → no message was sent.
    assert upd.effective_message.sent == []


async def test_middleware_none_user_returns_early(monkeypatch):
    # No effective_user → preprocess returns immediately, never touches the DB.
    def _boom(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("DB sync should not run for None user")

    monkeypatch.setattr(middleware.users_repo, "get_or_create_user", _boom)
    upd = SimpleNamespace(effective_user=None, effective_message=_RecMsg(),
                          effective_chat=None)
    assert await middleware.preprocess(upd, _ctx()) is None


async def test_middleware_bot_user_returns_early(monkeypatch):
    # A bot effective_user is ignored (is_bot=True) → no DB sync.
    def _boom(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("DB sync should not run for bot user")

    monkeypatch.setattr(middleware.users_repo, "get_or_create_user", _boom)
    upd = _upd(9001, is_bot=True)
    assert await middleware.preprocess(upd, _ctx()) is None


async def test_middleware_suspended_user_is_gated(monkeypatch):
    monkeypatch.setattr(middleware.settings, "middleware_sync_seconds", 60.0)

    async def fake_get_or_create(session, **kw):
        return _stub_user(uid=5151, status=UserStatus.SUSPENDED.value)

    monkeypatch.setattr(middleware.users_repo, "get_or_create_user", fake_get_or_create)

    upd, ctx = _upd(5151), _ctx()
    # Suspended status raises ApplicationHandlerStop to block downstream handlers.
    with pytest.raises(middleware.ApplicationHandlerStop):
        await middleware.preprocess(upd, ctx)
    # A localized suspension notice was sent to the user first.
    assert len(upd.effective_message.sent) == 1


async def test_middleware_banned_user_is_gated(monkeypatch):
    monkeypatch.setattr(middleware.settings, "middleware_sync_seconds", 60.0)

    async def fake_get_or_create(session, **kw):
        return _stub_user(uid=5252, status=UserStatus.BANNED.value)

    monkeypatch.setattr(middleware.users_repo, "get_or_create_user", fake_get_or_create)

    upd, ctx = _upd(5252), _ctx()
    with pytest.raises(middleware.ApplicationHandlerStop):
        await middleware.preprocess(upd, ctx)
    assert len(upd.effective_message.sent) == 1


async def test_middleware_banned_cached_status_gates_without_db(monkeypatch):
    # Within the throttle window a cached banned status still gates, doing no DB sync.
    monkeypatch.setattr(middleware.settings, "middleware_sync_seconds", 60.0)

    def _boom(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("cached status should avoid the DB round-trip")

    monkeypatch.setattr(middleware.users_repo, "get_or_create_user", _boom)

    import time as _time
    ctx = _ctx(db_user_id=5353, lang="en",
               _status=UserStatus.BANNED.value, _db_sync_at=_time.monotonic())
    upd = _upd(5353)
    with pytest.raises(middleware.ApplicationHandlerStop):
        await middleware.preprocess(upd, ctx)
    assert len(upd.effective_message.sent) == 1


async def test_middleware_allowlist_blocks_unlisted(monkeypatch):
    # Non-empty allowlist that excludes the user → blocked before any DB work.
    # allowed_user_ids is a computed property over telegram_allowed_users.
    monkeypatch.setattr(middleware.settings, "telegram_allowed_users", "7777")
    assert middleware.settings.allowed_user_ids == {7777}

    def _boom(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("unlisted user must be blocked before DB sync")

    monkeypatch.setattr(middleware.users_repo, "get_or_create_user", _boom)
    upd = _upd(1234)  # not in {7777}
    with pytest.raises(middleware.ApplicationHandlerStop):
        await middleware.preprocess(upd, _ctx())
