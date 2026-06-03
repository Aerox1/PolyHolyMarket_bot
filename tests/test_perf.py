"""Latency work: the markets TTL cache (fewer Gamma round-trips) and the
middleware DB-sync throttle (most updates do zero writes)."""

from types import SimpleNamespace

from bot import middleware
from polymarket import markets

_GAMMA = {"conditionId": "0xC", "question": "Q", "outcomes": '["Yes","No"]',
          "clobTokenIds": '["t1","t2"]', "outcomePrices": '["0.6","0.4"]',
          "closed": False, "active": True, "volume24hr": "1000"}


class _Resp:
    def raise_for_status(self):
        pass

    @property
    def status_code(self):
        return 200

    def json(self):
        return [_GAMMA]


class _Client:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, *a, **k):
        return _Resp()


# ── markets cache ──────────────────────────────────────────────────────────────

def test_trending_served_from_cache(monkeypatch):
    markets.clear_cache()
    monkeypatch.setattr(markets.settings, "markets_cache_ttl_seconds", 30.0)
    calls = []
    monkeypatch.setattr(markets, "_client", lambda: calls.append(1) or _Client())
    a = markets.trending_markets(5)
    b = markets.trending_markets(5)
    assert a == b and len(calls) == 1  # second call hit the cache, no new fetch


def test_cache_disabled_when_ttl_zero(monkeypatch):
    markets.clear_cache()
    monkeypatch.setattr(markets.settings, "markets_cache_ttl_seconds", 0.0)
    calls = []
    monkeypatch.setattr(markets, "_client", lambda: calls.append(1) or _Client())
    markets.trending_markets(5)
    markets.trending_markets(5)
    assert len(calls) == 2  # TTL=0 → no caching


def test_get_market_state_caches_open_not_error(monkeypatch):
    markets.clear_cache()
    monkeypatch.setattr(markets.settings, "markets_cache_ttl_seconds", 30.0)
    calls = []
    monkeypatch.setattr(markets, "_client", lambda: calls.append(1) or _Client())
    assert markets.get_market_state("0xC")[0] == "open"
    assert markets.get_market_state("0xC")[0] == "open"
    assert len(calls) == 1  # open result cached

    # an error must NOT be cached (so a transient blip retries)
    markets.clear_cache()
    err_calls = []

    class _Err(_Client):
        def get(self, *a, **k):
            err_calls.append(1)
            raise RuntimeError("egress blocked")

    monkeypatch.setattr(markets, "_client", lambda: _Err())
    assert markets.get_market_state("0xC") == ("error", None)
    assert markets.get_market_state("0xC") == ("error", None)
    assert len(err_calls) == 2  # both attempts actually fetched (no cached error)


# ── middleware DB-sync throttle ─────────────────────────────────────────────────

async def test_middleware_throttles_db_sync(monkeypatch):
    monkeypatch.setattr(middleware.settings, "middleware_sync_seconds", 60.0)
    calls = {"n": 0}  # allowed_user_ids is empty in tests → middleware is open to all

    class _User:
        id = 1
        language = "en"
        status = "active"
        last_seen_at = None

    async def fake_get_or_create(session, **kw):
        calls["n"] += 1
        return _User()

    monkeypatch.setattr(middleware.users_repo, "get_or_create_user", fake_get_or_create)

    upd = SimpleNamespace(effective_user=SimpleNamespace(id=1, is_bot=False, username="u", first_name="U"),
                          effective_message=SimpleNamespace(), effective_chat=SimpleNamespace(id=1))
    ctx = SimpleNamespace(user_data={})

    await middleware.preprocess(upd, ctx)
    assert calls["n"] == 1 and ctx.user_data["db_user_id"] == 1
    # a rapid second update reuses the cached id/status — no DB sync
    await middleware.preprocess(upd, ctx)
    assert calls["n"] == 1
    # once the throttle window has elapsed, it syncs again
    ctx.user_data["_db_sync_at"] -= 120.0
    await middleware.preprocess(upd, ctx)
    assert calls["n"] == 2
