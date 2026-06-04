"""Remaining small branches across infra modules.

Targets (each only a few focused tests; no network, no real client/crypto):
  polymarket/account_manager.py — explicit account_id, per-key lock coalescing,
      default_account_id passthrough, close/clear+invalidate dispose paths.
  dashboard/auth.py            — login_form already-authed redirect, logout,
      _safe_redirect_target edge paths, set_language.
  webapp/app.py                — app factory wiring (account_manager, routers,
      root endpoint when frontend unbuilt).
  core/i18n.py                 — normalize_lang variants, t() format-error
      fallback, text_dir, catalog_json/all_keys for unknown langs.
  core/logging.py              — setup_logging() configures without raising.
  core/config.py               — async_database_url derivations, encryption_keys
      comma-parsing, allowed_user_ids, default_language validator.
  db/engine.py                 — session_scope rollback-on-exception path.
  db/bootstrap.py main()       — runs create_all + bootstrap_admin without raising.

Does NOT duplicate tests/test_account_manager.py, test_infra_extra.py,
test_i18n.py, test_dashboard.py.
"""

import re
from types import SimpleNamespace

import pytest

import polymarket.account_manager as am_mod
from polymarket.account_manager import AccountManager
from polymarket.credentials import AccountMeta, NoAccountConnected, PolymarketCreds


# ════════════════════════════════════════════════════════════════════════════
# polymarket/account_manager.py — branches test_account_manager.py misses
# ════════════════════════════════════════════════════════════════════════════

class FakeClient:
    """Same shape as test_account_manager.FakeClient (sync close())."""

    def __init__(self, creds: PolymarketCreds):
        self.creds = creds
        self.order_signing_ready = creds.has_private_key
        self.closed = False

    def close(self):
        self.closed = True


class FakeStore:
    """Credential store with explicit per-account creds keyed by (user, account)."""

    def __init__(self):
        self.decrypt_calls = 0
        self._default = {1: 7}  # user 1 -> default account 7
        # creds keyed by (user_id, account_id)
        self._creds = {
            (1, 7): PolymarketCreds(wallet_address="0x" + "a" * 40, private_key="0x" + "b" * 64),
            (1, 9): PolymarketCreds(wallet_address="0x" + "c" * 40, private_key="0x" + "d" * 64),
        }

    async def default_account_id(self, user_id):
        return self._default.get(user_id)

    async def get_wallet_address(self, user_id, account_id=None):
        acct = account_id if account_id is not None else self._default.get(user_id)
        creds = self._creds.get((user_id, acct))
        return creds.wallet_address if creds else None

    async def load_decrypted_creds(self, user_id, account_id=None):
        acct = account_id if account_id is not None else self._default.get(user_id)
        if (user_id, acct) not in self._creds:
            raise NoAccountConnected(user_id)
        self.decrypt_calls += 1
        return self._creds[(user_id, acct)]

    async def list_accounts(self, user_id):
        return [
            AccountMeta(a, "L", self._creds[(u, a)].wallet_address, 0, "live", "active", a == self._default.get(u))
            for (u, a) in self._creds if u == user_id
        ]


@pytest.fixture(autouse=True)
def _mock_from_creds(monkeypatch):
    # Build a FakeClient instead of the real signing client (no crypto/network).
    monkeypatch.setattr(am_mod.Polymarket, "from_creds", staticmethod(lambda creds: FakeClient(creds)))


async def test_get_trading_client_with_explicit_account_id():
    # Explicit account_id bypasses default resolution and is cached under its own key.
    store = FakeStore()
    mgr = AccountManager(store)
    c_def = await mgr.get_trading_client(1)            # default account 7
    c_alt = await mgr.get_trading_client(1, account_id=9)
    assert c_def is not c_alt                          # distinct cache entries
    assert (1, 7) in mgr._cache and (1, 9) in mgr._cache
    # The explicit-account client used the account-9 creds (distinct wallet).
    assert c_alt.creds.wallet_address == "0x" + "c" * 40
    assert store.decrypt_calls == 2


async def test_default_account_id_passthrough_and_resolution():
    # Explicit account_id is returned verbatim; None resolves via the store default.
    store = FakeStore()
    mgr = AccountManager(store)
    assert await mgr.default_account_id(1, account_id=42) == 42  # passthrough branch
    assert await mgr.default_account_id(1) == 7                  # store default
    assert await mgr.default_account_id(2) is None               # unknown user


async def test_get_trading_client_none_account_raises():
    # default_account_id() returns None for an unknown user → NoAccountConnected.
    store = FakeStore()
    mgr = AccountManager(store)
    with pytest.raises(NoAccountConnected):
        await mgr.get_trading_client(2)  # no default account


async def test_concurrent_gets_coalesce_to_one_build():
    # The per-key lock means two concurrent get_trading_client calls build ONE client.
    import asyncio

    built = {"n": 0}

    def slow_build(creds):
        built["n"] += 1
        return FakeClient(creds)

    store = FakeStore()
    mgr = AccountManager(store)

    # asyncio.to_thread offloads the build; patch it to yield control so both
    # coroutines reach the lock before either finishes, proving coalescing.
    orig_to_thread = asyncio.to_thread

    async def to_thread_yield(fn, *a, **k):
        await asyncio.sleep(0)
        return fn(*a, **k)

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(am_mod.asyncio, "to_thread", to_thread_yield)
        mp.setattr(am_mod.Polymarket, "from_creds", staticmethod(slow_build))
        c1, c2 = await asyncio.gather(
            mgr.get_trading_client(1),
            mgr.get_trading_client(1),
        )
    assert c1 is c2          # both got the single cached client
    assert built["n"] == 1   # only one build happened despite two callers
    _ = orig_to_thread       # keep a reference; nothing else needed


async def test_expired_entry_is_disposed_and_rebuilt():
    # An expired cache entry is disposed and a fresh client built on next get.
    store = FakeStore()
    mgr = AccountManager(store, ttl=0.0)  # everything is immediately "expired"
    c1 = await mgr.get_trading_client(1)
    c2 = await mgr.get_trading_client(1)
    assert c1.closed is True   # the stale client was disposed
    assert c1 is not c2        # a new client replaced it
    assert store.decrypt_calls == 2


async def test_invalidate_specific_account_only():
    # Invalidate one account leaves the user's other cached account untouched.
    store = FakeStore()
    mgr = AccountManager(store)
    c7 = await mgr.get_trading_client(1)            # account 7
    c9 = await mgr.get_trading_client(1, account_id=9)
    mgr.invalidate(1, account_id=9)
    assert c9.closed is True                        # account-9 client disposed
    assert c7.closed is False                       # account-7 client kept
    assert (1, 7) in mgr._cache and (1, 9) not in mgr._cache


async def test_invalidate_unknown_account_is_noop():
    # Invalidating an account that was never cached must not raise.
    store = FakeStore()
    mgr = AccountManager(store)
    await mgr.get_trading_client(1)
    mgr.invalidate(1, account_id=12345)  # not cached → pop returns None, no dispose
    assert (1, 7) in mgr._cache


async def test_invalidate_all_user_accounts_disposes_each():
    # account_id=None drops + disposes every cached client for that user.
    store = FakeStore()
    mgr = AccountManager(store)
    c7 = await mgr.get_trading_client(1)
    c9 = await mgr.get_trading_client(1, account_id=9)
    mgr.invalidate(1)  # all accounts for user 1
    assert c7.closed and c9.closed
    assert not [k for k in mgr._cache if k[0] == 1]


async def test_clear_disposes_all_cached_clients():
    store = FakeStore()
    mgr = AccountManager(store)
    c7 = await mgr.get_trading_client(1)
    c9 = await mgr.get_trading_client(1, account_id=9)
    mgr.clear()
    assert c7.closed and c9.closed
    assert len(mgr._cache) == 0


async def test_dispose_swallows_close_errors():
    # _Entry.dispose() must swallow a client.close() that raises.
    class BoomClient(FakeClient):
        def close(self):
            raise RuntimeError("close blew up")

    entry = am_mod._Entry(client=BoomClient(PolymarketCreds.read_only("0x" + "e" * 40)), created=0.0)
    entry.dispose()  # must not propagate


# ════════════════════════════════════════════════════════════════════════════
# dashboard/auth.py — login_form redirect, logout, _safe_redirect_target, lang
# ════════════════════════════════════════════════════════════════════════════

from dashboard import auth as dash_auth  # noqa: E402


def _fake_request(*, headers=None, netloc="testserver"):
    """Minimal Request stand-in for _safe_redirect_target (reads headers + url.netloc)."""
    return SimpleNamespace(
        headers=headers or {},
        url=SimpleNamespace(netloc=netloc),
    )


def test_safe_redirect_no_referer_defaults_root():
    assert dash_auth._safe_redirect_target(_fake_request()) == "/"


def test_safe_redirect_same_origin_keeps_path_and_query():
    req = _fake_request(headers={"referer": "http://testserver/users?page=2"})
    assert dash_auth._safe_redirect_target(req) == "/users?page=2"


def test_safe_redirect_same_origin_path_no_query():
    req = _fake_request(headers={"referer": "http://testserver/metrics"})
    assert dash_auth._safe_redirect_target(req) == "/metrics"


def test_safe_redirect_cross_origin_ignored():
    # A referer on a different host is dropped → default to "/".
    req = _fake_request(headers={"referer": "http://evil.example/users"})
    assert dash_auth._safe_redirect_target(req) == "/"


def test_safe_redirect_rejects_protocol_relative():
    # A same-origin referer whose path is protocol-relative ("//host") is rejected.
    req = _fake_request(headers={"referer": "http://testserver//evil.example/x"})
    assert dash_auth._safe_redirect_target(req) == "/"


def test_safe_redirect_rejects_backslash_protocol_relative():
    # "/\\host" trick is also rejected.
    req = _fake_request(headers={"referer": "http://testserver/\\evil.example"})
    assert dash_auth._safe_redirect_target(req) == "/"


def test_safe_redirect_empty_path_defaults_root():
    # urlsplit of a bare-host same-origin referer → empty path → "/".
    req = _fake_request(headers={"referer": "http://testserver"})
    assert dash_auth._safe_redirect_target(req) == "/"


# ── TestClient-driven auth flows (login redirect / logout / set_language) ─────

from starlette.testclient import TestClient  # noqa: E402

from core import crypto  # noqa: E402
from db.engine import SessionLocal  # noqa: E402
from db.models import Admin  # noqa: E402


def _csrf(client) -> str:
    html = client.get("/login").text
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', html)
    assert m, "no csrf token in login form"
    return m.group(1)


@pytest.fixture
def dash_client():
    from dashboard.app import app
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def seeded_admin():
    from sqlalchemy import select

    with SessionLocal() as s:
        admin = s.scalar(select(Admin).where(Admin.username == "authadmin"))
        if admin is None:
            s.add(Admin(username="authadmin", password_hash=crypto.hash_password("pw123!"), is_superadmin=True))
            s.commit()


def _login(client):
    token = _csrf(client)
    return client.post("/login", data={"username": "authadmin", "password": "pw123!", "csrf_token": token})


def test_login_form_redirects_when_already_authed(dash_client, seeded_admin):
    # Authenticated session hitting GET /login → 303 to "/" (auth.py line ~51).
    _login(dash_client)
    r = dash_client.get("/login")
    assert r.status_code in (302, 303)
    assert r.headers.get("location") == "/"


def test_logout_clears_session(dash_client, seeded_admin):
    # logout clears the session and redirects to /login (auth.py lines 98-99).
    _login(dash_client)
    assert dash_client.get("/metrics").status_code == 200  # authed
    r = dash_client.get("/logout")
    assert r.status_code in (302, 303)
    assert "/login" in r.headers.get("location", "")
    # After logout, a protected route bounces back to login.
    r2 = dash_client.get("/metrics")
    assert r2.status_code in (302, 303)
    assert "/login" in r2.headers.get("location", "")


def test_set_language_accepts_supported_lang(dash_client, seeded_admin):
    # POST /me/language with a SUPPORTED lang stores it and redirects (lines 133-135).
    # Grab the CSRF token from the login form FIRST (it persists in the session
    # cookie); after login GET /login just redirects and renders no form.
    token = _csrf(dash_client)
    _login(dash_client)
    r = dash_client.post(
        "/me/language",
        data={"lang": "fa", "csrf_token": token},
        headers={"referer": "http://testserver/metrics"},
    )
    assert r.status_code in (302, 303)
    assert r.headers.get("location") == "/metrics"  # same-origin referer honoured


def test_set_language_ignores_unsupported_lang(dash_client, seeded_admin):
    # An unsupported lang is silently ignored (the `if lang in SUPPORTED` guard),
    # still redirecting to the safe target.
    token = _csrf(dash_client)
    _login(dash_client)
    r = dash_client.post("/me/language", data={"lang": "klingon", "csrf_token": token})
    assert r.status_code in (302, 303)
    assert r.headers.get("location") == "/"  # no referer → default root


def test_set_language_requires_admin(dash_client):
    # Unauthenticated POST /me/language → require_admin raises 303 to /login.
    # (No session, so the CSRF dependency may also fire; either way it must NOT
    #  reach the body. Assert it is not a 2xx success.)
    r = dash_client.post("/me/language", data={"lang": "en"})
    assert r.status_code >= 300
    assert r.status_code not in (200, 201)


# ════════════════════════════════════════════════════════════════════════════
# webapp/app.py — app factory wiring
# ════════════════════════════════════════════════════════════════════════════

def test_webapp_app_state_has_account_manager():
    from webapp.app import app
    from polymarket.account_manager import AccountManager as _AM

    assert isinstance(app.state.account_manager, _AM)


def test_webapp_api_router_mounted():
    # The /api router is mounted → an unauthenticated /api/categories returns 401,
    # proving the route exists (not a 404).
    from webapp.app import app
    client = TestClient(app)
    r = client.get("/api/categories")
    assert r.status_code == 401  # mounted + auth-gated, not missing


def test_webapp_root_endpoint_responds():
    # The frontend dist is not built in the test env → the fallback "/" handler
    # serves a JSON status (webapp/app.py lines 75-80). If a build exists, the
    # SPA mount serves it; either way "/" must respond, never 404.
    from webapp.app import app
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    if "application/json" in r.headers.get("content-type", ""):
        assert r.json()["status"] == "ok"


def test_webapp_create_app_returns_fresh_instance():
    # create_app() builds a new wired app each call (covers the factory body).
    from webapp.app import create_app

    fresh = create_app()
    assert fresh.title == "Polymarket Mini App"
    assert fresh.state.account_manager is not None


# ════════════════════════════════════════════════════════════════════════════
# core/i18n.py — normalize variants, t() format-error fallback, dir/catalog/keys
# ════════════════════════════════════════════════════════════════════════════

from core import i18n  # noqa: E402


def test_normalize_lang_supported_passthrough():
    # A supported language is returned unchanged; locale-variant / unknown → en.
    assert i18n.normalize_lang("zh") == "zh"
    assert i18n.normalize_lang("en-US") == "en"  # variant not in SUPPORTED → default
    assert i18n.normalize_lang("") == "en"


def test_t_format_error_returns_raw_value():
    # When the catalog string has a placeholder but the caller passes a DIFFERENT
    # var, str.format raises KeyError → t() returns the raw (unformatted) string
    # rather than crashing (lines 74-75). bot.start.welcome uses {name}.
    raw = i18n.t("bot.start.welcome", "en")   # contains "{name}"
    assert "{name}" in raw
    # Passing an unrelated var triggers the KeyError fallback path.
    got = i18n.t("bot.start.welcome", "en", wrong_var="x")
    assert got == raw  # unformatted value returned unchanged


def test_t_missing_key_unknown_lang_returns_key():
    # Missing key under an unknown (→en) lang returns the key itself (line 69-70).
    assert i18n.t("totally.unknown.key", "klingon") == "totally.unknown.key"


def test_text_dir_unknown_lang_defaults_ltr():
    # An unknown language normalizes to en, whose _meta dir is ltr.
    assert i18n.text_dir("klingon") == "ltr"


def test_catalog_json_for_unknown_lang_is_valid_json():
    import json as _json

    # Unknown lang normalizes to en; the cached JSON is a parseable dict.
    parsed = _json.loads(i18n.catalog_json("klingon"))
    assert isinstance(parsed, dict)


def test_all_keys_excludes_meta_and_returns_set():
    keys = i18n.all_keys("en")
    assert isinstance(keys, set) and keys
    # _meta is filtered out of the leaf-key walk.
    assert not any(k.startswith("_meta") for k in keys)


# ════════════════════════════════════════════════════════════════════════════
# core/logging.py — setup_logging configures without raising
# ════════════════════════════════════════════════════════════════════════════

def test_setup_logging_attaches_redact_filter():
    import logging

    from core.logging import RedactSecretsFilter, setup_logging

    setup_logging("DEBUG")
    root = logging.getLogger()
    # A RedactSecretsFilter is attached to the root logger.
    assert any(isinstance(f, RedactSecretsFilter) for f in root.filters)
    # Noisy third-party loggers were quieted to WARNING.
    assert logging.getLogger("httpx").level == logging.WARNING


def test_setup_logging_default_level_from_settings():
    # No explicit level → uses settings.log_level without raising.
    from core.logging import setup_logging

    setup_logging()  # must not raise


def test_redact_filter_scrubs_hex_and_fernet():
    import logging

    from core.logging import RedactSecretsFilter

    filt = RedactSecretsFilter()
    rec = logging.LogRecord(
        "n", logging.INFO, __file__, 1,
        "key=0x" + "a" * 64, (), None,
    )
    assert filt.filter(rec) is True
    assert "a" * 64 not in rec.getMessage()  # the hex key was redacted


# ════════════════════════════════════════════════════════════════════════════
# core/config.py — async_database_url derivations, encryption_keys, helpers
# ════════════════════════════════════════════════════════════════════════════

from core.config import Settings  # noqa: E402


def test_async_database_url_explicit_value_wins():
    s = Settings(DATABASE_URL="postgresql://u:p@h/db", DATABASE_URL_ASYNC="custom://override")
    assert s.async_database_url == "custom://override"


def test_async_database_url_derives_from_psycopg():
    s = Settings(DATABASE_URL="postgresql+psycopg://u:p@h:5432/db", DATABASE_URL_ASYNC="")
    assert s.async_database_url == "postgresql+asyncpg://u:p@h:5432/db"


def test_async_database_url_derives_from_plain_postgres():
    s = Settings(DATABASE_URL="postgresql://u:p@h/db", DATABASE_URL_ASYNC="")
    assert s.async_database_url == "postgresql+asyncpg://u:p@h/db"


def test_async_database_url_derives_from_sqlite():
    s = Settings(DATABASE_URL="sqlite:///x.db", DATABASE_URL_ASYNC="")
    assert s.async_database_url == "sqlite+aiosqlite:///x.db"


def test_async_database_url_sqlite_already_aiosqlite_unchanged():
    s = Settings(DATABASE_URL="sqlite+aiosqlite:///x.db", DATABASE_URL_ASYNC="")
    assert s.async_database_url == "sqlite+aiosqlite:///x.db"


def test_async_database_url_unknown_scheme_passthrough():
    s = Settings(DATABASE_URL="mysql://u:p@h/db", DATABASE_URL_ASYNC="")
    assert s.async_database_url == "mysql://u:p@h/db"


def test_encryption_keys_orders_current_then_rotated():
    # encryption_key first, then comma-split old keys (whitespace stripped, blanks dropped).
    s = Settings(ENCRYPTION_KEY="new", ENCRYPTION_KEY_OLD=" old1 , , old2 ")
    assert s.encryption_keys == ["new", "old1", "old2"]


def test_encryption_keys_empty_when_unset():
    s = Settings(ENCRYPTION_KEY="", ENCRYPTION_KEY_OLD="")
    assert s.encryption_keys == []


def test_encryption_keys_only_old_when_no_current():
    # No current key but old keys present → only the rotated keys (current omitted).
    s = Settings(ENCRYPTION_KEY="", ENCRYPTION_KEY_OLD="rot1,rot2")
    assert s.encryption_keys == ["rot1", "rot2"]


def test_allowed_user_ids_parses_csv():
    s = Settings(TELEGRAM_ALLOWED_USERS=" 1 , 2 ,,3 ")
    assert s.allowed_user_ids == {1, 2, 3}


def test_allowed_user_ids_empty():
    s = Settings(TELEGRAM_ALLOWED_USERS="")
    assert s.allowed_user_ids == set()


def test_default_language_validator_falls_back_to_en():
    # An unsupported default language is coerced to "en" by the field validator.
    assert Settings(DEFAULT_LANGUAGE="xx").default_language == "en"
    assert Settings(DEFAULT_LANGUAGE="fa").default_language == "fa"


# ════════════════════════════════════════════════════════════════════════════
# db/engine.py — session_scope rollback-on-exception path
# ════════════════════════════════════════════════════════════════════════════

def test_session_scope_rolls_back_on_exception():
    # An exception inside the with-block triggers rollback + re-raise (engine 61-63).
    from db.engine import session_scope
    from db.models import Admin
    from sqlalchemy import select

    with pytest.raises(ValueError):
        with session_scope() as s:
            s.add(Admin(username="rollback-victim", password_hash="x", is_superadmin=False))
            s.flush()
            raise ValueError("boom")  # forces rollback, NOT commit

    # The row must NOT have been committed.
    with SessionLocal() as s2:
        assert s2.scalar(select(Admin).where(Admin.username == "rollback-victim")) is None


def test_session_scope_commits_on_success():
    from db.engine import session_scope
    from db.models import Admin
    from sqlalchemy import select

    with session_scope() as s:
        s.add(Admin(username="commit-ok", password_hash="x", is_superadmin=False))

    with SessionLocal() as s2:
        assert s2.scalar(select(Admin).where(Admin.username == "commit-ok")) is not None


# ════════════════════════════════════════════════════════════════════════════
# db/bootstrap.py main() — runs create_all + bootstrap_admin without raising
# ════════════════════════════════════════════════════════════════════════════

def test_bootstrap_main_runs_and_creates_admin(monkeypatch):
    from sqlalchemy import select

    from db import bootstrap
    from db.models import Admin

    # Hash set + clean DB → main() runs create_all (idempotent) then bootstrap_admin.
    monkeypatch.setattr(bootstrap.settings, "admin_bootstrap_password_hash", "main-hash")
    monkeypatch.setattr(bootstrap.settings, "admin_bootstrap_user", "mainadmin")
    bootstrap.main()  # must not raise
    with SessionLocal() as s:
        admin = s.scalar(select(Admin).where(Admin.username == "mainadmin"))
    assert admin is not None and admin.is_superadmin is True


def test_bootstrap_main_no_hash_skips_admin(monkeypatch):
    from sqlalchemy import select

    from db import bootstrap
    from db.models import Admin

    # No hash → main() still runs create_all but creates no admin.
    monkeypatch.setattr(bootstrap.settings, "admin_bootstrap_password_hash", "")
    bootstrap.main()
    with SessionLocal() as s:
        assert s.scalar(select(Admin).limit(1)) is None
