"""Extra Mini App API tests (webapp/routers/api.py).

Covers the branches NOT exercised by test_webapp.py: a CONNECTED user placing a
real (mocked) bet, /api/portfolio with positions+balance, leaderboard metrics
other than volume, seeded categories (visible vs hidden), /api/me connected,
the category-markets + market-detail endpoints, and the bet guard branches
(bad amount / bad outcome / unknown market / order failure & rejection).

Network is never hit: polymarket.markets.get_market / category_markets are
monkeypatched, and app.state.account_manager is swapped for a fake that returns
fake trading/readonly clients whose data methods are plain sync callables (the
handler invokes them via asyncio.to_thread).
"""

import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from starlette.testclient import TestClient

from core import crypto
from db.engine import async_session_scope
from db.models import Account, Bet, Category, Order, UserStats
from db.repositories import accounts as accounts_repo
from db.repositories import users as users_repo
from polymarket.credentials import NoAccountConnected, TradingUnavailable
from sqlalchemy import select

TOKEN = "test-token"


# ── initData signer (same algorithm as test_webapp.py) ────────────────────────

def _init_data(telegram_id: int = 9991) -> str:
    fields = {
        "user": json.dumps({"id": telegram_id, "username": "miniuser", "first_name": "Mini"}),
        "auth_date": str(int(time.time())),
    }
    dcs = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def _hdr(telegram_id: int = 9991) -> dict:
    return {"X-Telegram-Init-Data": _init_data(telegram_id)}


@pytest.fixture
def client():
    from webapp.app import app
    return TestClient(app)


# ── DB seed helpers ───────────────────────────────────────────────────────────

async def _seed_user(telegram_id: int) -> int:
    """Create the user the way current_user would, return internal user.id."""
    async with async_session_scope() as s:
        u = await users_repo.get_or_create_user(
            s, telegram_id=telegram_id, username="miniuser", first_name="Mini",
            default_language="en")
        await s.flush()
        return u.id


async def _seed_account(user_id: int, wallet: str = "0xWALLET") -> int:
    """A connected Account with a real (encrypted) private key blob."""
    async with async_session_scope() as s:
        acc = Account(
            user_id=user_id,
            label="Main",
            wallet_address=wallet,
            signature_type=0,
            encrypted_private_key=crypto.encrypt("0x" + "11" * 32),
            mode="live",
            status="active",
        )
        s.add(acc)
        await s.flush()
        return acc.id


async def _seed_category(**kw) -> int:
    defaults = dict(slug="politics", title="Politics", tag_slug="politics",
                    volume=1000.0, hidden=False, image_status="ready",
                    image_path="/cards/x.png")
    defaults.update(kw)
    async with async_session_scope() as s:
        c = Category(**defaults)
        s.add(c)
        await s.flush()
        return c.id


# ── Fake AccountManager + clients (no network) ────────────────────────────────

class _FakeClient:
    """A fake Polymarket trading/readonly client. All data methods are SYNC
    (handlers wrap them in asyncio.to_thread)."""

    def __init__(self, *, order_result=None, order_exc=None,
                 positions=None, balance=None, balance_exc=None):
        self._order_result = order_result if order_result is not None else {"orderID": "OID-1"}
        self._order_exc = order_exc
        self._positions = positions if positions is not None else []
        self._balance = balance
        self._balance_exc = balance_exc
        self.placed = []

    def place_market_order(self, token, amount, side):
        self.placed.append((token, amount, side))
        if self._order_exc is not None:
            raise self._order_exc
        return self._order_result

    def get_positions(self):
        return self._positions

    def get_balance(self):
        if self._balance_exc is not None:
            raise self._balance_exc
        return self._balance


class _FakeMgr:
    """Mimics the AccountManager surface api.py uses via webapp.deps.manager()."""

    def __init__(self, *, account_id=1, trading=None, readonly=None,
                 trading_exc=None, readonly_exc=None):
        self._account_id = account_id
        self._trading = trading or _FakeClient()
        self._readonly = readonly or _FakeClient()
        self._trading_exc = trading_exc
        self._readonly_exc = readonly_exc

    async def get_trading_client(self, user_id, account_id=None):
        if self._trading_exc is not None:
            raise self._trading_exc
        return self._trading

    async def get_readonly_client(self, user_id, account_id=None):
        if self._readonly_exc is not None:
            raise self._readonly_exc
        return self._readonly

    async def default_account_id(self, user_id, account_id=None):
        return self._account_id


def _install_mgr(client: TestClient, mgr) -> None:
    client.app.state.account_manager = mgr


# A bettable normalized market (the shape markets.get_market returns).
_MARKET = {
    "id": "0xabc",
    "question": "Will it rain?",
    "yes_token": "TOKEN_YES",
    "no_token": "TOKEN_NO",
    "yes_price": 0.4,
    "no_price": 0.6,
    "image": None,
}


# ═══════════════════════════════════════════════════════════════════════════════
# /api/me — connected
# ═══════════════════════════════════════════════════════════════════════════════

async def test_me_connected_reports_wallet(client):
    uid = await _seed_user(7001)
    await _seed_account(uid, wallet="0xDEADBEEF")
    r = client.get("/api/me", headers=_hdr(7001))
    assert r.status_code == 200
    body = r.json()
    assert body["connected"] is True
    assert body["wallet"] == "0xDEADBEEF"
    assert body["telegram_id"] == 7001
    assert "stats" in body


# ═══════════════════════════════════════════════════════════════════════════════
# /api/portfolio — connected
# ═══════════════════════════════════════════════════════════════════════════════

async def test_portfolio_connected_shape(client):
    await _seed_user(7002)
    positions = [
        {"title": "Mkt A", "outcome": "Yes", "size": "10", "currentValue": "12.5", "cashPnl": "2.5"},
        {"title": None, "market": "Mkt B", "outcome": "No", "size": "3", "curValue": "1.0", "pnl": "-2.0"},
        "not-a-dict",  # skipped by the isinstance guard
    ]
    ro = _FakeClient(positions=positions)
    trading = _FakeClient(balance={"balance": "100.5"})
    _install_mgr(client, _FakeMgr(trading=trading, readonly=ro))

    r = client.get("/api/portfolio", headers=_hdr(7002))
    assert r.status_code == 200
    body = r.json()
    # Balance comes from the trading client's get_balance (parsed via _parse_usdc).
    assert body["balance"] == pytest.approx(100.5)
    assert len(body["positions"]) == 2  # the string row was dropped
    p0 = body["positions"][0]
    assert p0["title"] == "Mkt A" and p0["outcome"] == "Yes"
    assert p0["size"] == pytest.approx(10.0)
    assert p0["value"] == pytest.approx(12.5)
    assert p0["pnl"] == pytest.approx(2.5)
    # Fallback keys (market / curValue / pnl) on the 2nd row.
    p1 = body["positions"][1]
    assert p1["title"] == "Mkt B" and p1["value"] == pytest.approx(1.0) and p1["pnl"] == pytest.approx(-2.0)


async def test_portfolio_positions_dict_data_key(client):
    """get_positions may return {"data": [...]} instead of a bare list."""
    await _seed_user(7003)
    ro = _FakeClient(positions={"data": [{"title": "X", "outcome": "Yes", "size": 1}]})
    _install_mgr(client, _FakeMgr(readonly=ro))
    r = client.get("/api/portfolio", headers=_hdr(7003))
    assert r.status_code == 200
    assert len(r.json()["positions"]) == 1


async def test_portfolio_balance_best_effort_on_trading_error(client):
    """If the trading client is unavailable, balance is None but positions return."""
    await _seed_user(7004)
    ro = _FakeClient(positions=[{"title": "Y", "outcome": "No", "size": 2}])
    _install_mgr(client, _FakeMgr(readonly=ro, trading_exc=TradingUnavailable()))
    r = client.get("/api/portfolio", headers=_hdr(7004))
    assert r.status_code == 200
    body = r.json()
    assert body["balance"] is None
    assert len(body["positions"]) == 1


async def test_portfolio_balance_micro_usdc_scaled(client):
    """_parse_usdc divides values > 1e6 by 1e6 (raw on-chain USDC has 6 decimals)."""
    await _seed_user(7005)
    trading = _FakeClient(balance={"balance": "5000000"})  # 5 USDC in micro-units
    _install_mgr(client, _FakeMgr(readonly=_FakeClient(positions=[]), trading=trading))
    r = client.get("/api/portfolio", headers=_hdr(7005))
    assert r.status_code == 200
    assert r.json()["balance"] == pytest.approx(5.0)


# ═══════════════════════════════════════════════════════════════════════════════
# /api/leaderboard — other metrics
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("metric", ["bets", "pnl", "wins"])
async def test_leaderboard_supported_metrics_echo(client, metric):
    await _seed_user(7100)
    r = client.get(f"/api/leaderboard?metric={metric}", headers=_hdr(7100))
    assert r.status_code == 200
    body = r.json()
    assert body["metric"] == metric
    assert isinstance(body["rows"], list)
    assert "me" in body


async def test_leaderboard_unknown_metric_falls_back_to_bets(client):
    await _seed_user(7101)
    r = client.get("/api/leaderboard?metric=bogus", headers=_hdr(7101))
    assert r.status_code == 200
    assert r.json()["metric"] == "bets"


async def test_leaderboard_default_metric_is_bets(client):
    await _seed_user(7102)
    r = client.get("/api/leaderboard", headers=_hdr(7102))
    assert r.status_code == 200
    assert r.json()["metric"] == "bets"


async def test_leaderboard_rows_reflect_seeded_stats(client):
    """A user with stats appears in the leaderboard rows with computed fields."""
    uid = await _seed_user(7103)
    async with async_session_scope() as s:
        s.add(UserStats(user_id=uid, total_bets=5, total_volume_usd=50.0,
                        wins=2, losses=1, settled_bets=3, realized_pnl_usd=7.0,
                        current_streak=4))
    r = client.get("/api/leaderboard?metric=bets", headers=_hdr(7103))
    assert r.status_code == 200
    rows = r.json()["rows"]
    assert len(rows) == 1
    row = rows[0]
    assert row["rank"] == 1 and row["bets"] == 5
    assert row["volume_usd"] == pytest.approx(50.0)
    assert row["streak"] == 4 and row["wins"] == 2


# ═══════════════════════════════════════════════════════════════════════════════
# /api/categories — seeded visible vs hidden
# ═══════════════════════════════════════════════════════════════════════════════

async def test_categories_lists_visible_excludes_hidden(client):
    await _seed_user(7200)
    vid = await _seed_category(slug="politics", title="Politics", volume=900.0, hidden=False)
    await _seed_category(slug="sports", title="Sports", volume=500.0, hidden=False)
    await _seed_category(slug="secret", title="Secret", volume=9999.0, hidden=True)

    r = client.get("/api/categories", headers=_hdr(7200))
    assert r.status_code == 200
    body = r.json()
    titles = {c["title"] for c in body}
    assert titles == {"Politics", "Sports"}  # hidden one excluded
    pol = next(c for c in body if c["id"] == vid)
    assert pol["slug"] == "politics"
    assert pol["volume"] == pytest.approx(900.0)
    assert pol["image_status"] == "ready"
    assert pol["image_url"] == "/cards/x.png"


# ═══════════════════════════════════════════════════════════════════════════════
# /api/categories/{id}/markets
# ═══════════════════════════════════════════════════════════════════════════════

async def test_category_markets_happy(client, monkeypatch):
    await _seed_user(7300)
    cid = await _seed_category(slug="crypto", title="Crypto", tag_slug="crypto-tag")
    captured = {}

    def _fake_cat_markets(slug, limit):
        captured["slug"] = slug
        captured["limit"] = limit
        return [{"id": "m1", "question": "Q1"}]

    monkeypatch.setattr("polymarket.markets.category_markets", _fake_cat_markets)
    r = client.get(f"/api/categories/{cid}/markets", headers=_hdr(7300))
    assert r.status_code == 200
    body = r.json()
    assert body["category"]["id"] == cid and body["category"]["title"] == "Crypto"
    assert body["markets"] == [{"id": "m1", "question": "Q1"}]
    # tag_slug is preferred over slug, and the cap is 40.
    assert captured["slug"] == "crypto-tag" and captured["limit"] == 40


async def test_category_markets_missing_category_404(client):
    await _seed_user(7301)
    r = client.get("/api/categories/999999/markets", headers=_hdr(7301))
    assert r.status_code == 404


async def test_category_markets_hidden_category_404(client):
    await _seed_user(7302)
    cid = await _seed_category(slug="hush", title="Hush", hidden=True)
    r = client.get(f"/api/categories/{cid}/markets", headers=_hdr(7302))
    assert r.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════════
# /api/markets/{id}
# ═══════════════════════════════════════════════════════════════════════════════

async def test_market_detail_happy(client, monkeypatch):
    await _seed_user(7400)
    monkeypatch.setattr("polymarket.markets.get_market", lambda mid: {**_MARKET, "id": mid})
    r = client.get("/api/markets/0xfeed", headers=_hdr(7400))
    assert r.status_code == 200
    assert r.json()["id"] == "0xfeed"
    assert r.json()["question"] == "Will it rain?"


async def test_market_detail_not_found_404(client, monkeypatch):
    await _seed_user(7401)
    monkeypatch.setattr("polymarket.markets.get_market", lambda mid: None)
    r = client.get("/api/markets/0xnope", headers=_hdr(7401))
    assert r.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════════
# /api/bet — happy path (connected user) + side effects
# ═══════════════════════════════════════════════════════════════════════════════

async def test_bet_happy_creates_bet_order_and_stats(client, monkeypatch):
    uid = await _seed_user(7500)
    acc_id = await _seed_account(uid)
    monkeypatch.setattr("polymarket.markets.get_market", lambda mid: dict(_MARKET))
    trading = _FakeClient(order_result={"orderID": "ORD-77"})
    _install_mgr(client, _FakeMgr(account_id=acc_id, trading=trading))

    r = client.post("/api/bet", headers=_hdr(7500),
                    json={"market_id": "0xabc", "outcome": "yes", "amount_usd": 5})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["order_id"] == "ORD-77"
    assert body["outcome"] == "yes"
    assert body["amount"] == pytest.approx(5.0)
    assert body["question"] == "Will it rain?"
    # The trading client received a BUY for the YES token.
    assert trading.placed == [("TOKEN_YES", 5.0, "buy")]

    # Side effects persisted: a Bet, an Order (open), and UserStats.
    async with async_session_scope() as s:
        bet = await s.scalar(select(Bet).where(Bet.user_id == uid))
        assert bet is not None
        assert bet.outcome == "YES"  # create_bet upper-cases
        assert bet.token_id == "TOKEN_YES"
        assert float(bet.amount_usd) == pytest.approx(5.0)
        assert float(bet.entry_price) == pytest.approx(0.4)  # yes_price
        assert bet.source == "miniapp"
        assert bet.clob_order_id == "ORD-77"

        order = await s.scalar(select(Order).where(Order.account_id == acc_id))
        assert order is not None
        assert order.side == "BUY" and order.order_type == "MARKET"
        assert order.status == "open" and order.clob_order_id == "ORD-77"
        assert float(order.size) == pytest.approx(5.0)

        stats = await s.get(UserStats, uid)
        assert stats is not None and stats.total_bets == 1
        assert float(stats.total_volume_usd) == pytest.approx(5.0)


async def test_bet_no_outcome_uses_no_token_and_price(client, monkeypatch):
    uid = await _seed_user(7501)
    acc_id = await _seed_account(uid)
    monkeypatch.setattr("polymarket.markets.get_market", lambda mid: dict(_MARKET))
    trading = _FakeClient(order_result={"orderId": "ORD-NO"})  # alt key form
    _install_mgr(client, _FakeMgr(account_id=acc_id, trading=trading))

    r = client.post("/api/bet", headers=_hdr(7501),
                    json={"market_id": "0xabc", "outcome": "NO", "amount_usd": 10})
    assert r.status_code == 200
    assert r.json()["outcome"] == "no"
    assert r.json()["order_id"] == "ORD-NO"
    assert trading.placed == [("TOKEN_NO", 10.0, "buy")]
    async with async_session_scope() as s:
        bet = await s.scalar(select(Bet).where(Bet.user_id == uid))
        assert bet.outcome == "NO" and bet.token_id == "TOKEN_NO"
        assert float(bet.entry_price) == pytest.approx(0.6)  # no_price


# ── bet guard branches ────────────────────────────────────────────────────────

async def test_bet_invalid_amount_400(client):
    await _seed_user(7502)
    r = client.post("/api/bet", headers=_hdr(7502),
                    json={"market_id": "0xabc", "outcome": "yes", "amount_usd": "abc"})
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid amount"


async def test_bet_missing_amount_400(client):
    await _seed_user(7503)
    r = client.post("/api/bet", headers=_hdr(7503),
                    json={"market_id": "0xabc", "outcome": "yes"})
    assert r.status_code == 400  # amount_usd is None -> float(None) -> TypeError


async def test_bet_bad_outcome_400(client):
    await _seed_user(7504)
    r = client.post("/api/bet", headers=_hdr(7504),
                    json={"market_id": "0xabc", "outcome": "maybe", "amount_usd": 5})
    assert r.status_code == 400
    assert "outcome" in r.json()["detail"]


async def test_bet_amount_below_min_400(client):
    await _seed_user(7505)
    r = client.post("/api/bet", headers=_hdr(7505),
                    json={"market_id": "0xabc", "outcome": "yes", "amount_usd": 0.1})
    assert r.status_code == 400
    assert "between" in r.json()["detail"]


async def test_bet_amount_above_max_400(client):
    await _seed_user(7506)
    r = client.post("/api/bet", headers=_hdr(7506),
                    json={"market_id": "0xabc", "outcome": "yes", "amount_usd": 5000})
    assert r.status_code == 400
    assert "between" in r.json()["detail"]


async def test_bet_market_not_found_404(client, monkeypatch):
    await _seed_user(7507)
    monkeypatch.setattr("polymarket.markets.get_market", lambda mid: None)
    r = client.post("/api/bet", headers=_hdr(7507),
                    json={"market_id": "0xabc", "outcome": "yes", "amount_usd": 5})
    assert r.status_code == 404
    assert r.json()["detail"] == "market not found"


async def test_bet_trading_unavailable_409(client, monkeypatch):
    await _seed_user(7508)
    monkeypatch.setattr("polymarket.markets.get_market", lambda mid: dict(_MARKET))
    _install_mgr(client, _FakeMgr(trading_exc=TradingUnavailable()))
    r = client.post("/api/bet", headers=_hdr(7508),
                    json={"market_id": "0xabc", "outcome": "yes", "amount_usd": 5})
    assert r.status_code == 409
    assert r.json()["detail"] == "trading_unavailable"


async def test_bet_no_account_via_manager_409(client, monkeypatch):
    await _seed_user(7509)
    monkeypatch.setattr("polymarket.markets.get_market", lambda mid: dict(_MARKET))
    _install_mgr(client, _FakeMgr(trading_exc=NoAccountConnected(1)))
    r = client.post("/api/bet", headers=_hdr(7509),
                    json={"market_id": "0xabc", "outcome": "yes", "amount_usd": 5})
    assert r.status_code == 409
    assert r.json()["detail"] == "no_account"


async def test_bet_order_raises_502_order_failed(client, monkeypatch):
    uid = await _seed_user(7510)
    acc_id = await _seed_account(uid)
    monkeypatch.setattr("polymarket.markets.get_market", lambda mid: dict(_MARKET))
    trading = _FakeClient(order_exc=RuntimeError("clob boom"))
    _install_mgr(client, _FakeMgr(account_id=acc_id, trading=trading))
    r = client.post("/api/bet", headers=_hdr(7510),
                    json={"market_id": "0xabc", "outcome": "yes", "amount_usd": 5})
    assert r.status_code == 502
    assert r.json()["detail"] == "order_failed"
    # No Bet should be recorded for a failed order.
    async with async_session_scope() as s:
        assert await s.scalar(select(Bet).where(Bet.user_id == uid)) is None


async def test_bet_rejected_result_502_order_rejected(client, monkeypatch):
    uid = await _seed_user(7511)
    acc_id = await _seed_account(uid)
    monkeypatch.setattr("polymarket.markets.get_market", lambda mid: dict(_MARKET))
    # success=False signals a rejection (no exception raised).
    trading = _FakeClient(order_result={"success": False, "errorMsg": "rejected"})
    _install_mgr(client, _FakeMgr(account_id=acc_id, trading=trading))
    r = client.post("/api/bet", headers=_hdr(7511),
                    json={"market_id": "0xabc", "outcome": "yes", "amount_usd": 5})
    assert r.status_code == 502
    assert r.json()["detail"] == "order_rejected"
    # Regression for the rollback-on-raise bug: the handler now commits the rejected
    # Order + audit rows before raising, so get_db's rollback-on-exception can't
    # discard them. The audit trail for a rejected bet must survive (a Bet must NOT
    # — we raise before record_bet).
    async with async_session_scope() as s:
        rejected = await s.scalar(select(Order).where(Order.account_id == acc_id))
        assert rejected is not None and rejected.status == "rejected"
        assert await s.scalar(select(Bet).where(Bet.user_id == uid)) is None


async def test_bet_requires_initdata_auth(client):
    """No initData -> 401 before any market work."""
    r = client.post("/api/bet", json={"market_id": "0xabc", "outcome": "yes", "amount_usd": 5})
    assert r.status_code == 401
