"""Extra coverage for polymarket/markets.py: pure helpers (_jsarr/_as_list/
_normalize_market), parse_resolution gaps, the Gamma fetchers (top_categories,
category_markets, trending_markets, search_markets, get_market,
market_resolution), and cache semantics (clear/deep-copy/expiry-drop).

Network is fully mocked by replacing markets._client with a fake context-manager
client whose .get() returns a fake response. No egress.

Does NOT duplicate test_news_bet (get_market_state open/closed/error),
test_settlement (parse_resolution basics), or test_perf (cache TTL on/off)."""

import json
import time
from types import SimpleNamespace

from polymarket import markets


# ── fake HTTP layer (matches test_news_bet/_test_perf shape) ──────────────────

def _resp(status, payload):
    return SimpleNamespace(status_code=status,
                           json=lambda: payload,
                           raise_for_status=lambda: None)


def _fake_client(resp):
    """Context-manager client whose .get() returns `resp` (or raises if it's an
    Exception)."""
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


def _client_returning(resp):
    return lambda: _fake_client(resp)


# A normalizable binary market (Gamma encodes arrays as JSON strings).
def _gamma(cond="0xCOND", q="Will it rain?", vol="1000", outcomes='["Yes","No"]',
           tokens='["tokYES","tokNO"]', prices='["0.70","0.30"]',
           closed=False, active=True):
    return {"conditionId": cond, "question": q, "outcomes": outcomes,
            "clobTokenIds": tokens, "outcomePrices": prices,
            "closed": closed, "active": active, "volume24hr": vol, "negRisk": False}


# ── _jsarr ────────────────────────────────────────────────────────────────────

def test_jsarr_list_passthrough():
    src = ["Yes", "No"]
    assert markets._jsarr(src) is src  # lists are handed straight back


def test_jsarr_json_string_parsed():
    assert markets._jsarr('["Yes","No"]') == ["Yes", "No"]


def test_jsarr_invalid_string_empty():
    assert markets._jsarr("not json") == []  # JSONDecodeError → []


def test_jsarr_non_str_non_list_empty():
    assert markets._jsarr(None) == []
    assert markets._jsarr(123) == []
    assert markets._jsarr({"a": 1}) == []


# ── _as_list ────────────────────────────────────────────────────────────────────

def test_as_list_passes_list():
    src = [1, 2, 3]
    assert markets._as_list(src) is src


def test_as_list_dict_data_key():
    assert markets._as_list({"data": [1]}) == [1]


def test_as_list_dict_events_key():
    # no "data" → falls through to "events"
    assert markets._as_list({"events": [{"x": 1}]}) == [{"x": 1}]


def test_as_list_dict_markets_key():
    assert markets._as_list({"markets": ["m"]}) == ["m"]


def test_as_list_dict_no_known_keys_empty():
    assert markets._as_list({"foo": "bar"}) == []


def test_as_list_other_type_empty():
    assert markets._as_list("scalar") == []
    assert markets._as_list(None) == []


# ── _normalize_market ─────────────────────────────────────────────────────────

def test_normalize_valid_binary():
    nm = markets._normalize_market(_gamma())
    assert nm["id"] == "0xCOND"
    assert nm["question"] == "Will it rain?"
    assert nm["yes_token"] == "tokYES" and nm["no_token"] == "tokNO"
    assert nm["yes_price"] == 0.70 and nm["no_price"] == 0.30
    assert nm["volume"] == 1000.0
    assert nm["neg_risk"] is False


def test_normalize_falls_back_to_id_and_title():
    # no conditionId → id falls back to "id"; no question → falls back to "title".
    m = _gamma()
    del m["conditionId"]
    del m["question"]
    m["id"] = "fallback-id"
    m["title"] = "Fallback Q"
    nm = markets._normalize_market(m)
    assert nm["id"] == "fallback-id" and nm["question"] == "Fallback Q"


def test_normalize_outcomes_not_two_returns_none():
    # 3 outcomes → not a binary market
    assert markets._normalize_market(_gamma(outcomes='["A","B","C"]')) is None


def test_normalize_tokens_not_two_returns_none():
    assert markets._normalize_market(_gamma(tokens='["only-one"]')) is None


def test_normalize_closed_true_returns_none():
    assert markets._normalize_market(_gamma(closed=True)) is None


def test_normalize_active_false_returns_none():
    assert markets._normalize_market(_gamma(active=False)) is None


def test_normalize_unparseable_price_is_none_for_that_side():
    # bad yes price (non-numeric), good no price → yes_price None, no_price set.
    nm = markets._normalize_market(_gamma(prices='["abc","0.30"]'))
    assert nm is not None
    assert nm["yes_price"] is None and nm["no_price"] == 0.30


def test_normalize_missing_price_index_is_none():
    # only one price → index 1 raises IndexError → no_price None.
    nm = markets._normalize_market(_gamma(prices='["0.70"]'))
    assert nm is not None
    assert nm["yes_price"] == 0.70 and nm["no_price"] is None


# ── parse_resolution GAPS (basics live in test_settlement) ─────────────────────

def _mkt(closed, uma, prices, tokens=("TA", "TB")):
    return {"closed": closed, "umaResolutionStatus": uma,
            "outcomePrices": json.dumps(list(prices)),
            "clobTokenIds": json.dumps(list(tokens))}


def test_resolution_two_legs_high_is_void():
    # Two legs >= 0.99 (ambiguous data glitch) → resolved + void, no winner.
    r = markets.parse_resolution(_mkt(True, "resolved", ["0.99", "1.0"]))
    assert r == {"resolved": True, "winning_token": None, "void": True}


def test_resolution_winner_index_beyond_tokens_is_void():
    # Single winner at index 1, but only ONE token → winners[0] >= len(tokens) → void.
    r = markets.parse_resolution(_mkt(True, "resolved", ["0", "1"], tokens=("ONLY",)))
    assert r == {"resolved": True, "winning_token": None, "void": True}


# ── top_categories ──────────────────────────────────────────────────────────────

def test_top_categories_aggregates_ranks_and_denylists(monkeypatch):
    monkeypatch.setattr(markets.settings, "markets_cache_ttl_seconds", 0)  # bypass cache
    events = [
        {"volume24hr": "100", "tags": [{"label": "Trump", "slug": "trump", "id": 1},
                                       {"label": "All", "slug": "all", "id": 9}]},
        {"volume24hr": "50", "tags": [{"label": "Trump", "slug": "trump", "id": 1}]},
        {"volume24hr": "300", "tags": [{"label": "Sports", "slug": "sports", "id": 2}]},
        {"volume24hr": "10", "tags": [{"label": "Trending", "slug": "trending", "id": 3}]},
    ]
    monkeypatch.setattr(markets, "_client", _client_returning(_resp(200, events)))
    cats = markets.top_categories(limit=10)
    slugs = [c["slug"] for c in cats]
    # "all" + "trending" are denylisted; trump aggregated 100+50=150; sports 300.
    assert "all" not in slugs and "trending" not in slugs
    assert slugs == ["sports", "trump"]  # ranked desc by aggregated volume
    trump = next(c for c in cats if c["slug"] == "trump")
    assert trump["volume"] == 150.0 and trump["title"] == "Trump" and trump["tag_id"] == "1"


def test_top_categories_respects_limit(monkeypatch):
    monkeypatch.setattr(markets.settings, "markets_cache_ttl_seconds", 0)
    events = [{"volume24hr": str(i), "tags": [{"label": f"T{i}", "slug": f"t{i}", "id": i}]}
              for i in range(1, 6)]
    monkeypatch.setattr(markets, "_client", _client_returning(_resp(200, events)))
    assert len(markets.top_categories(limit=2)) == 2


def test_top_categories_slug_fallback_from_label(monkeypatch):
    # tag with no slug → slug derived from label.lower().replace(" ","-").
    monkeypatch.setattr(markets.settings, "markets_cache_ttl_seconds", 0)
    events = [{"volume24hr": "5", "tags": [{"label": "World Cup", "id": 7}]}]
    monkeypatch.setattr(markets, "_client", _client_returning(_resp(200, events)))
    cats = markets.top_categories(limit=5)
    assert cats[0]["slug"] == "world-cup"


# ── category_markets ────────────────────────────────────────────────────────────

def test_category_markets_normalizes_dedups_sorts_limits(monkeypatch):
    monkeypatch.setattr(markets.settings, "markets_cache_ttl_seconds", 0)
    events = [
        {"title": "Event A", "markets": [
            _gamma(cond="0xLOW", vol="10"),
            _gamma(cond="0xHIGH", vol="500"),
            _gamma(cond="0xCLOSED", closed=True),   # dropped by _normalize_market
        ]},
        {"title": "Event B", "markets": [
            _gamma(cond="0xHIGH", vol="999"),        # dup id, higher volume kept
        ]},
    ]
    monkeypatch.setattr(markets, "_client", _client_returning(_resp(200, events)))
    out = markets.category_markets("sports", limit=10)
    ids = [m["id"] for m in out]
    assert ids == ["0xHIGH", "0xLOW"]               # sorted desc, closed dropped
    high = out[0]
    assert high["volume"] == 999.0                  # dedup kept the highest-volume copy
    assert high["event_title"] == "Event B"         # event_title stamped from owning event


def test_category_markets_respects_limit(monkeypatch):
    monkeypatch.setattr(markets.settings, "markets_cache_ttl_seconds", 0)
    events = [{"title": "E", "markets": [
        _gamma(cond=f"0x{i}", vol=str(i)) for i in range(5)]}]
    monkeypatch.setattr(markets, "_client", _client_returning(_resp(200, events)))
    assert len(markets.category_markets("sports", limit=2)) == 2


def test_category_markets_empty_when_no_events(monkeypatch):
    monkeypatch.setattr(markets.settings, "markets_cache_ttl_seconds", 0)
    monkeypatch.setattr(markets, "_client", _client_returning(_resp(200, [])))
    assert markets.category_markets("sports") == []


# ── trending_markets ────────────────────────────────────────────────────────────

def test_trending_dedups_sorts_limits(monkeypatch):
    monkeypatch.setattr(markets.settings, "markets_cache_ttl_seconds", 0)
    rows = [
        _gamma(cond="0xA", vol="10"),
        _gamma(cond="0xB", vol="900"),
        _gamma(cond="0xA", vol="999"),   # dup id → ignored (first-seen kept)
        _gamma(cond="0xC", vol="500"),
    ]
    monkeypatch.setattr(markets, "_client", _client_returning(_resp(200, rows)))
    out = markets.trending_markets(limit=2)
    assert [m["id"] for m in out] == ["0xB", "0xC"]  # top 2 by volume desc
    # de-dup keeps FIRST occurrence (vol 10), then sort places it below the cut.
    full = markets.trending_markets(limit=10)
    a = next(m for m in full if m["id"] == "0xA")
    assert a["volume"] == 10.0


# ── search_markets ────────────────────────────────────────────────────────────

def test_search_uses_public_search_dedups_limits(monkeypatch):
    # search_markets now uses Gamma /public-search → {"events":[{"markets":[...]}]},
    # returning binary markets in RELEVANCE (insertion) order, deduped, capped.
    monkeypatch.setattr(markets.settings, "markets_cache_ttl_seconds", 0)
    body = {"events": [
        {"title": "E1", "markets": [_gamma(cond="0xX", vol="1"), _gamma(cond="0xY", vol="80")]},
        {"title": "E2", "markets": [_gamma(cond="0xY", vol="999"),   # dup id ignored
                                    _gamma(cond="0xZ", vol="40")]},
    ]}
    monkeypatch.setattr(markets, "_client", _client_returning(_resp(200, body)))
    out = markets.search_markets("rain", limit=2)
    assert [m["id"] for m in out] == ["0xX", "0xY"]   # relevance order, not volume
    assert out[0]["event_title"] == "E1"


def test_normalize_market_is_outcome_order_aware():
    # ["No","Yes"] markets must NOT invert the side: yes_token/yes_price come from
    # the "Yes" leg by LABEL, not position (else a Yes bet buys the NO token).
    import json
    m = {"conditionId": "0xZ", "question": "Q?", "outcomes": json.dumps(["No", "Yes"]),
         "clobTokenIds": json.dumps(["NO_TOK", "YES_TOK"]), "outcomePrices": json.dumps(["0.3", "0.7"]),
         "closed": False, "active": True, "volume24hr": "1"}
    nm = markets._normalize_market(m)
    assert nm["yes_token"] == "YES_TOK" and nm["no_token"] == "NO_TOK"
    assert nm["yes_price"] == 0.7 and nm["no_price"] == 0.3
    # the usual ["Yes","No"] order is unchanged
    m2 = dict(m, outcomes=json.dumps(["Yes", "No"]), clobTokenIds=json.dumps(["YES_TOK", "NO_TOK"]),
              outcomePrices=json.dumps(["0.7", "0.3"]))
    nm2 = markets._normalize_market(m2)
    assert nm2["yes_token"] == "YES_TOK" and nm2["yes_price"] == 0.7


def test_search_non_200_returns_empty(monkeypatch):
    monkeypatch.setattr(markets.settings, "markets_cache_ttl_seconds", 0)
    monkeypatch.setattr(markets, "_client", _client_returning(_resp(503, {})))
    assert markets.search_markets("rain") == []


# ── get_market ────────────────────────────────────────────────────────────────

def test_get_market_200_normalized(monkeypatch):
    monkeypatch.setattr(markets.settings, "markets_cache_ttl_seconds", 0)
    monkeypatch.setattr(markets, "_client", _client_returning(_resp(200, [_gamma()])))
    nm = markets.get_market("0xCOND")
    assert nm is not None and nm["id"] == "0xCOND" and nm["yes_token"] == "tokYES"


def test_get_market_non_200_returns_none(monkeypatch):
    monkeypatch.setattr(markets.settings, "markets_cache_ttl_seconds", 0)
    monkeypatch.setattr(markets, "_client", _client_returning(_resp(503, [_gamma()])))
    assert markets.get_market("0xCOND") is None


def test_get_market_empty_rows_returns_none(monkeypatch):
    monkeypatch.setattr(markets.settings, "markets_cache_ttl_seconds", 0)
    monkeypatch.setattr(markets, "_client", _client_returning(_resp(200, [])))
    assert markets.get_market("0xCOND") is None


# ── market_resolution ───────────────────────────────────────────────────────────

def test_market_resolution_resolved(monkeypatch):
    monkeypatch.setattr(markets.settings, "markets_cache_ttl_seconds", 0)
    resolved = {"closed": True, "umaResolutionStatus": "resolved",
                "outcomePrices": '["1","0"]', "clobTokenIds": '["TA","TB"]'}
    monkeypatch.setattr(markets, "_client", _client_returning(_resp(200, [resolved])))
    assert markets.market_resolution("0xCOND") == {
        "resolved": True, "winning_token": "TA", "void": False}


def test_market_resolution_raise_returns_unresolved(monkeypatch):
    monkeypatch.setattr(markets.settings, "markets_cache_ttl_seconds", 0)
    monkeypatch.setattr(markets, "_client",
                        _client_returning(RuntimeError("egress blocked")))
    assert markets.market_resolution("0xCOND") == {
        "resolved": False, "winning_token": None, "void": False}


def test_market_resolution_empty_rows_returns_unresolved(monkeypatch):
    monkeypatch.setattr(markets.settings, "markets_cache_ttl_seconds", 0)
    monkeypatch.setattr(markets, "_client", _client_returning(_resp(200, [])))
    assert markets.market_resolution("0xCOND") == {
        "resolved": False, "winning_token": None, "void": False}


# ── cache: clear / deep-copy / expiry drop ────────────────────────────────────

def test_clear_cache_empties(monkeypatch):
    monkeypatch.setattr(markets.settings, "markets_cache_ttl_seconds", 30.0)
    markets._cache_put("k", [1, 2, 3])
    assert markets._CACHE  # something is cached
    markets.clear_cache()
    assert markets._CACHE == {}


def test_cache_get_returns_deep_copy(monkeypatch):
    # Mutating the returned object must NOT corrupt the cached value for a 2nd read.
    monkeypatch.setattr(markets.settings, "markets_cache_ttl_seconds", 30.0)
    markets.clear_cache()
    markets._cache_put("k", [{"v": 1}])
    first = markets._cache_get("k")
    first[0]["v"] = 999          # mutate the handed-back copy
    first.append({"v": 2})
    second = markets._cache_get("k")
    assert second == [{"v": 1}]  # pristine — shared object was not corrupted


def test_cache_get_drops_expired_entry(monkeypatch):
    monkeypatch.setattr(markets.settings, "markets_cache_ttl_seconds", 30.0)
    markets.clear_cache()
    # Insert an already-expired entry directly to exercise the pop path.
    markets._CACHE["stale"] = (time.monotonic() - 1, ["old"])
    assert markets._cache_get("stale") is None      # expired → returns None
    assert "stale" not in markets._CACHE            # ...and was popped


def test_cache_get_none_when_ttl_zero(monkeypatch):
    # TTL <= 0 short-circuits before touching the dict.
    monkeypatch.setattr(markets.settings, "markets_cache_ttl_seconds", 0)
    markets._CACHE["live"] = (time.monotonic() + 999, ["x"])
    assert markets._cache_get("live") is None
    markets.clear_cache()


def test_cache_get_scalar_value_not_copied(monkeypatch):
    # Non-container cached values are returned as-is (no deepcopy branch).
    monkeypatch.setattr(markets.settings, "markets_cache_ttl_seconds", 30.0)
    markets.clear_cache()
    markets._cache_put("scal", "open")
    assert markets._cache_get("scal") == "open"
    markets.clear_cache()
