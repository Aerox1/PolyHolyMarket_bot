"""Webapp API smoke tests via TestClient: initData auth gate + categories list."""

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest
from starlette.testclient import TestClient

TOKEN = "test-token"


def _init_data(telegram_id: int = 9991) -> str:
    fields = {
        "user": json.dumps({"id": telegram_id, "username": "miniuser", "first_name": "Mini"}),
        "auth_date": str(int(time.time())),
    }
    dcs = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


@pytest.fixture
def client():
    from webapp.app import app
    return TestClient(app)


def test_categories_requires_initdata(client):
    r = client.get("/api/categories")
    assert r.status_code == 401


def test_categories_with_valid_initdata(client):
    r = client.get("/api/categories", headers={"X-Telegram-Init-Data": _init_data()})
    assert r.status_code == 200
    assert isinstance(r.json(), list)  # empty (no categories seeded) but authorized


def test_me_reports_not_connected(client):
    r = client.get("/api/me", headers={"X-Telegram-Init-Data": _init_data(9992)})
    assert r.status_code == 200
    body = r.json()
    assert body["connected"] is False and body["telegram_id"] == 9992


def test_me_includes_stats(client):
    r = client.get("/api/me", headers={"X-Telegram-Init-Data": _init_data(9994)})
    assert r.status_code == 200
    stats = r.json()["stats"]
    assert stats["current_streak"] == 0 and stats["total_bets"] == 0


def test_leaderboard_shape(client):
    r = client.get("/api/leaderboard?metric=volume", headers={"X-Telegram-Init-Data": _init_data(9995)})
    assert r.status_code == 200
    body = r.json()
    assert body["metric"] == "volume" and isinstance(body["rows"], list) and "me" in body


def test_portfolio_requires_account(client):
    r = client.get("/api/portfolio", headers={"X-Telegram-Init-Data": _init_data(9996)})
    assert r.status_code == 409


def test_bet_requires_account(client, monkeypatch):
    # A user with no connected account cannot bet (409 no_account), and we never
    # hit the network because get_market is only called after auth — stub it anyway.
    monkeypatch.setattr("polymarket.markets.get_market",
                        lambda mid: {"yes_token": "1", "no_token": "2", "question": "Q?"})
    r = client.post("/api/bet", headers={"X-Telegram-Init-Data": _init_data(9993)},
                    json={"market_id": "0xabc", "outcome": "yes", "amount_usd": 5})
    assert r.status_code == 409
    assert r.json()["detail"] == "no_account"
