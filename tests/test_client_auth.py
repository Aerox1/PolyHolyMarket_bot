"""polymarket.client / .auth / .credentials — built without network.

Client read methods use a fake httpx client that records (url, params) and
returns a sentinel json; trading methods use a FakeClob recording calls. auth's
pure validators + error branches are exercised with ClobClient mocked.

Does NOT duplicate test_news_bet.py (place_capped_buy) or test_account_manager.py
(AccountManager). For validate_and_derive happy/proxy/mismatch see
test_connect_auth.py — here we cover its ERROR branches + the pure helpers."""

from types import SimpleNamespace

import pytest

from py_clob_client.clob_types import OrderType
from py_clob_client.order_builder.constants import BUY, SELL

from core.config import settings
from polymarket import auth
from polymarket.client import Polymarket
from polymarket.credentials import (
    AccountMeta,
    NoAccountConnected,
    PolymarketCreds,
    TradingUnavailable,
    WalletMismatchError,
)


# ── fakes ───────────────────────────────────────────────────────────────────────

class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):  # no-op (no HTTP error)
        return None

    def json(self):
        return self._payload


class _FakeHttp:
    """Records every GET as (url, params) and returns a fixed sentinel payload."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, params=None):
        self.calls.append((url, params))
        return _FakeResp(self.payload)

    def close(self):
        pass


def _pm_reads(payload=None):
    """A Polymarket wired only for public REST reads (no clob/network)."""
    pm = Polymarket.__new__(Polymarket)
    pm._http = _FakeHttp({"sentinel": payload if payload is not None else "X"})
    pm._wallet_address = "0xWALLET"
    return pm


class _FakeClob:
    """Records trading-method calls; returns tagged sentinels."""

    def __init__(self):
        self.calls = []

    # ── reads ──
    def get_orders(self, params):
        self.calls.append(("get_orders", params))
        return ["order"]

    def get_balance_allowance(self, params):
        self.calls.append(("get_balance_allowance", params))
        return {"balance": "1"}

    def get_order(self, order_id):
        self.calls.append(("get_order", order_id))
        return {"id": order_id}

    def get_trades(self, params):
        self.calls.append(("get_trades", params))
        return ["trade"]

    # ── order build/post ──
    def create_order(self, order_args, options):
        self.calls.append(("create_order", order_args, options))
        return "signed-limit"

    def create_market_order(self, order_args):
        self.calls.append(("create_market_order", order_args))
        return "signed-market"

    def post_order(self, signed, order_type):
        self.calls.append(("post_order", signed, order_type))
        return {"orderID": "OID"}

    # ── cancels ──
    def cancel(self, order_id):
        self.calls.append(("cancel", order_id))
        return {"cancelled": order_id}

    def cancel_all(self):
        self.calls.append(("cancel_all",))
        return {"cancelled": "all"}

    def cancel_market_orders(self, market):
        self.calls.append(("cancel_market_orders", market))
        return {"cancelled_market": market}


def _pm_trading():
    pm = Polymarket.__new__(Polymarket)
    pm._clob = _FakeClob()
    pm._trading_ready = True
    pm._order_signing_ready = True
    return pm


# ── Data API (keyed by wallet address, under data_url) ──────────────────────────

def test_get_positions_url_params_and_payload():
    pm = _pm_reads("positions")
    out = pm.get_positions(limit=7, offset=3)
    url, params = pm._http.calls[0]
    assert url == f"{settings.data_url}/positions"
    assert params["user"] == "0xWALLET" and params["limit"] == 7 and params["offset"] == 3
    assert params["sortBy"] == "CURRENT" and params["sortDirection"] == "DESC"
    assert out == {"sentinel": "positions"}  # returns the json payload verbatim


def test_get_trades_url_and_params():
    pm = _pm_reads()
    pm.get_trades(limit=5, offset=2)
    url, params = pm._http.calls[0]
    assert url == f"{settings.data_url}/trades"
    assert params == {"user": "0xWALLET", "limit": 5, "offset": 2}


def test_get_portfolio_value_url_and_params():
    pm = _pm_reads()
    pm.get_portfolio_value()
    url, params = pm._http.calls[0]
    assert url == f"{settings.data_url}/value" and params == {"user": "0xWALLET"}


def test_get_activity_url_and_params():
    pm = _pm_reads()
    pm.get_activity(limit=9)
    url, params = pm._http.calls[0]
    assert url == f"{settings.data_url}/activity"
    assert params == {"user": "0xWALLET", "limit": 9}


# ── Gamma API (under gamma_url) ─────────────────────────────────────────────────

def test_search_markets_url_and_filter_params():
    pm = _pm_reads()
    pm.search_markets("rain", limit=4)
    url, params = pm._http.calls[0]
    assert url == f"{settings.gamma_url}/markets"
    assert params["_limit"] == 4 and params["title_like"] == "rain"
    assert params["active"] is True and params["closed"] is False


def test_search_events_url_and_filter_params():
    pm = _pm_reads()
    pm.search_events("election", limit=2)
    url, params = pm._http.calls[0]
    assert url == f"{settings.gamma_url}/events"
    assert params["title_like"] == "election" and params["_limit"] == 2


def test_get_event_url_path():
    pm = _pm_reads()
    pm.get_event("E123")
    url, params = pm._http.calls[0]
    assert url == f"{settings.gamma_url}/events/E123" and params is None


def test_get_market_url_path():
    pm = _pm_reads()
    pm.get_market("0xCOND")
    url, params = pm._http.calls[0]
    assert url == f"{settings.gamma_url}/markets/0xCOND" and params is None


# ── CLOB public reads (under clob_url, by token_id) ─────────────────────────────

def test_get_price_uppercases_side():
    pm = _pm_reads()
    pm.get_price("tok", side="buy")
    url, params = pm._http.calls[0]
    assert url == f"{settings.clob_url}/price"
    assert params == {"token_id": "tok", "side": "BUY"}  # side uppercased


def test_get_price_default_side_is_buy():
    pm = _pm_reads()
    pm.get_price("tok")
    assert pm._http.calls[0][1]["side"] == "BUY"


def test_get_orderbook_midpoint_spread_urls():
    pm = _pm_reads()
    pm.get_orderbook("tok")
    pm.get_midpoint("tok")
    pm.get_spread("tok")
    urls = [c[0] for c in pm._http.calls]
    assert urls == [
        f"{settings.clob_url}/book",
        f"{settings.clob_url}/midpoint",
        f"{settings.clob_url}/spread",
    ]
    for _url, params in pm._http.calls:
        assert params == {"token_id": "tok"}


# ── trading guards: not-ready → TradingUnavailable ──────────────────────────────

def _pm_unready():
    pm = Polymarket.__new__(Polymarket)
    pm._clob = _FakeClob()
    pm._trading_ready = False
    pm._order_signing_ready = False
    return pm


def test_require_trading_methods_raise_when_not_ready():
    pm = _pm_unready()
    with pytest.raises(TradingUnavailable):
        pm.get_balance()
    with pytest.raises(TradingUnavailable):
        pm.get_open_orders()
    with pytest.raises(TradingUnavailable):
        pm.get_order("OID")
    with pytest.raises(TradingUnavailable):
        pm.get_my_trades()
    with pytest.raises(TradingUnavailable):
        pm.get_open_orders_for_token("tok")


def test_require_signing_methods_raise_when_not_ready():
    pm = _pm_unready()
    with pytest.raises(TradingUnavailable):
        pm.place_limit_order("tok", 0.5, 2, "buy")
    with pytest.raises(TradingUnavailable):
        pm.place_market_order("tok", 10, "buy")
    with pytest.raises(TradingUnavailable):
        pm.cancel_order("OID")
    with pytest.raises(TradingUnavailable):
        pm.cancel_all_orders()
    with pytest.raises(TradingUnavailable):
        pm.cancel_market_orders("0xCOND")


def test_require_trading_raises_when_clob_is_none():
    # _trading_ready True but no clob object → still guarded
    pm = Polymarket.__new__(Polymarket)
    pm._clob = None
    pm._trading_ready = True
    pm._order_signing_ready = True
    with pytest.raises(TradingUnavailable):
        pm.get_balance()


# ── CLOB authenticated reads (delegate to clob) ─────────────────────────────────

def test_get_open_orders_none_passes_none_params():
    pm = _pm_trading()
    assert pm.get_open_orders(None) == ["order"]
    name, params = pm._clob.calls[0]
    assert name == "get_orders" and params is None  # no market → None params


def test_get_open_orders_with_market_wraps_in_params():
    pm = _pm_trading()
    pm.get_open_orders("0xCOND")
    name, params = pm._clob.calls[0]
    assert name == "get_orders" and params is not None and params.market == "0xCOND"


def test_get_open_orders_for_token_wraps_token_as_market():
    pm = _pm_trading()
    pm.get_open_orders_for_token("tokYES")
    name, params = pm._clob.calls[0]
    assert name == "get_orders" and params.market == "tokYES"


def test_get_balance_uses_collateral_asset_type():
    pm = _pm_trading()
    out = pm.get_balance()
    name, params = pm._clob.calls[0]
    assert name == "get_balance_allowance" and params.asset_type == "COLLATERAL"
    assert out == {"balance": "1"}


def test_get_order_delegates_with_id():
    pm = _pm_trading()
    assert pm.get_order("OID") == {"id": "OID"}
    assert pm._clob.calls[0] == ("get_order", "OID")


def test_get_my_trades_passes_tradeparams():
    pm = _pm_trading()
    assert pm.get_my_trades() == ["trade"]
    assert pm._clob.calls[0][0] == "get_trades"


# ── place orders (build args + post correct OrderType) ──────────────────────────

def test_place_limit_order_builds_gtc_with_mapped_side():
    pm = _pm_trading()
    out = pm.place_limit_order("tok", 0.42, 3.0, "SELL", neg_risk=True)
    assert out == {"orderID": "OID"}
    create = next(c for c in pm._clob.calls if c[0] == "create_order")
    _name, args, options = create
    assert args.token_id == "tok" and args.price == 0.42 and args.size == 3.0
    assert args.side == SELL  # "SELL" lowercased then mapped via SIDE_MAP
    assert options.neg_risk is True  # neg_risk threaded into PartialCreateOrderOptions
    post = next(c for c in pm._clob.calls if c[0] == "post_order")
    assert post[1] == "signed-limit" and post[2] == OrderType.GTC


def test_place_limit_order_no_neg_risk_passes_none_options():
    pm = _pm_trading()
    pm.place_limit_order("tok", 0.5, 1.0, "buy")  # neg_risk default None
    create = next(c for c in pm._clob.calls if c[0] == "create_order")
    assert create[2] is None  # options None when neg_risk omitted
    assert create[1].side == BUY


def test_place_market_order_builds_fok():
    pm = _pm_trading()
    out = pm.place_market_order("tok", 25.0, "buy")
    assert out == {"orderID": "OID"}
    create = next(c for c in pm._clob.calls if c[0] == "create_market_order")
    _name, args = create
    assert args.token_id == "tok" and args.amount == 25.0 and args.side == BUY
    post = next(c for c in pm._clob.calls if c[0] == "post_order")
    assert post[1] == "signed-market" and post[2] == OrderType.FOK


# ── cancels (delegate to matching clob method) ──────────────────────────────────

def test_cancel_order():
    pm = _pm_trading()
    assert pm.cancel_order("OID") == {"cancelled": "OID"}
    assert pm._clob.calls[0] == ("cancel", "OID")


def test_cancel_all_orders():
    pm = _pm_trading()
    assert pm.cancel_all_orders() == {"cancelled": "all"}
    assert pm._clob.calls[0] == ("cancel_all",)


def test_cancel_market_orders():
    pm = _pm_trading()
    assert pm.cancel_market_orders("0xCOND") == {"cancelled_market": "0xCOND"}
    assert pm._clob.calls[0] == ("cancel_market_orders", "0xCOND")


# ── status props + helpers ──────────────────────────────────────────────────────

def test_status_properties_reflect_attrs():
    pm = _pm_trading()
    pm._wallet_address = "0xWALLET"
    assert pm.wallet_address == "0xWALLET"
    assert pm.trading_ready is True
    assert pm.order_signing_ready is True


def test_get_address_uses_clob_or_none():
    pm = _pm_trading()

    class _C:
        def get_address(self):
            return "0xADDR"

    pm._clob = _C()
    assert pm.get_address() == "0xADDR"
    pm._clob = None
    assert pm.get_address() is None  # no clob → None


def test_close_swallows_errors():
    pm = Polymarket.__new__(Polymarket)

    class _Boom:
        def close(self):
            raise RuntimeError("nope")

    pm._http = _Boom()
    pm.close()  # must not raise


# ── from_creds: key→wallet ownership check (mock _init_clob, no network) ─────────

class _DerivedClob:
    def __init__(self, addr):
        self._addr = addr

    def get_address(self):
        return self._addr


def _patch_init(monkeypatch, derived_addr):
    def fake_init(self):
        self._clob = _DerivedClob(derived_addr)
    monkeypatch.setattr(Polymarket, "_init_clob", fake_init)


def test_from_creds_match_case_insensitive_returns_client(monkeypatch):
    _patch_init(monkeypatch, "0x" + "A" * 40)  # uppercase derived
    creds = PolymarketCreds(wallet_address="0x" + "a" * 40, private_key="0x" + "b" * 64)
    pm = Polymarket.from_creds(creds)  # case-insensitive match → OK
    assert pm.wallet_address == "0x" + "a" * 40


def test_from_creds_mismatch_raises(monkeypatch):
    _patch_init(monkeypatch, "0x" + "c" * 40)  # different signer
    creds = PolymarketCreds(wallet_address="0x" + "a" * 40, private_key="0x" + "b" * 64)
    with pytest.raises(WalletMismatchError) as ei:
        Polymarket.from_creds(creds)
    assert ei.value.derived == "0x" + "c" * 40
    assert ei.value.claimed == "0x" + "a" * 40


def test_from_creds_read_only_skips_ownership_check(monkeypatch):
    # no private key → never verifies address, never raises even if derived differs
    _patch_init(monkeypatch, "0x" + "z" * 40)
    creds = PolymarketCreds(wallet_address="0x" + "a" * 40)  # read-only
    pm = Polymarket.from_creds(creds)
    assert pm.wallet_address == "0x" + "a" * 40


def test_from_creds_get_address_failure_skips_check(monkeypatch):
    # if get_address raises, derived stays None → no mismatch raised
    class _Bad:
        def get_address(self):
            raise RuntimeError("derive blew up")

    def fake_init(self):
        self._clob = _Bad()
    monkeypatch.setattr(Polymarket, "_init_clob", fake_init)
    creds = PolymarketCreds(wallet_address="0x" + "a" * 40, private_key="0x" + "b" * 64)
    pm = Polymarket.from_creds(creds)  # swallowed → returns client
    assert pm.wallet_address == "0x" + "a" * 40


# ── credentials.py ──────────────────────────────────────────────────────────────

def test_creds_has_private_key_present_absent():
    assert PolymarketCreds(wallet_address="0xW", private_key="0xpk").has_private_key is True
    assert PolymarketCreds(wallet_address="0xW").has_private_key is False
    assert PolymarketCreds(wallet_address="0xW", private_key="").has_private_key is False


def test_creds_has_api_creds_requires_all_three():
    full = PolymarketCreds(wallet_address="0xW", api_key="k", api_secret="s", api_passphrase="p")
    assert full.has_api_creds is True
    # any missing → False
    assert PolymarketCreds(wallet_address="0xW", api_key="k", api_secret="s").has_api_creds is False
    assert PolymarketCreds(wallet_address="0xW").has_api_creds is False


def test_creds_read_only_factory():
    c = PolymarketCreds.read_only("0xZ")
    assert c.wallet_address == "0xZ" and c.has_private_key is False and c.signature_type == 0


def test_creds_repr_hides_secrets():
    # repr=False fields must never appear in repr/str (key-leak guard)
    c = PolymarketCreds(wallet_address="0xW", private_key="0xSECRETKEY",
                        api_secret="0xSECRETSEC", api_passphrase="0xSECRETPASS")
    r = repr(c)
    assert "0xSECRETKEY" not in r and "0xSECRETSEC" not in r and "0xSECRETPASS" not in r
    assert "0xW" in r  # non-secret wallet_address still shown


def test_no_account_connected_message_and_attr():
    e = NoAccountConnected(42)
    assert e.user_id == 42 and "42" in str(e)


def test_wallet_mismatch_attrs_and_message():
    e = WalletMismatchError("0xDERIVED", "0xCLAIMED")
    assert e.derived == "0xDERIVED" and e.claimed == "0xCLAIMED"
    # message must NOT leak the addresses verbatim — generic safe text
    assert "does not match" in str(e)


def test_trading_unavailable_str():
    assert str(TradingUnavailable("L2 not configured")) == "L2 not configured"


def test_account_meta_fields():
    am = AccountMeta(account_id=3, label="Main", wallet_address="0xW",
                     signature_type=1, mode="live", status="active", is_active=True)
    assert (am.account_id, am.label, am.wallet_address) == (3, "Main", "0xW")
    assert am.signature_type == 1 and am.mode == "live"
    assert am.status == "active" and am.is_active is True


# ── auth.py pure helpers ────────────────────────────────────────────────────────

def test_is_valid_address():
    assert auth.is_valid_address("0x" + "a" * 40) is True
    assert auth.is_valid_address("  0x" + "F" * 40 + "  ") is True  # trims whitespace
    assert auth.is_valid_address("0x" + "a" * 39) is False  # too short
    assert auth.is_valid_address("zz" + "a" * 40) is False  # no 0x prefix
    assert auth.is_valid_address("0x" + "g" * 40) is False  # non-hex


def test_normalize_private_key_adds_prefix_and_validates():
    bare = "b" * 64
    assert auth.normalize_private_key(bare) == "0x" + bare  # 0x prepended
    assert auth.normalize_private_key("0x" + bare) == "0x" + bare  # already prefixed
    assert auth.normalize_private_key("  0x" + bare + "  ") == "0x" + bare  # trimmed
    assert auth.normalize_private_key("b" * 63) is None  # wrong length
    assert auth.normalize_private_key("z" * 64) is None  # non-hex


def test_parse_usdc_conversion_and_fallback():
    assert auth._parse_usdc("5") == 5.0  # small → as-is
    assert auth._parse_usdc("5000000") == 5.0  # > 1e6 atomic units → /1e6
    assert auth._parse_usdc(1_000_000) == 1_000_000.0  # boundary: not > 1e6 → as-is
    assert auth._parse_usdc("garbage") == 0.0  # unparsable → 0
    assert auth._parse_usdc(None) == 0.0


# ── auth.validate_and_derive error branches (ClobClient mocked) ─────────────────

_KEY = "0x" + "b" * 64
_SIGNER = "0x" + "a" * 40


def _good_clob(**kw):
    """A clob that derives _SIGNER, derives api creds, and reports a balance."""
    class _C:
        def get_address(self):
            return _SIGNER

        def create_or_derive_api_creds(self):
            return SimpleNamespace(api_key="k", api_secret="s", api_passphrase="p")

        def set_api_creds(self, creds):
            pass

        def get_balance_allowance(self, params):
            return {"balance": "2000000"}  # 2 USDC atomic
    return _C()


def test_validate_clob_construction_failure_raises_connecterror(monkeypatch):
    def boom(**kw):
        raise ValueError("bad key")
    monkeypatch.setattr(auth, "ClobClient", boom)
    with pytest.raises(auth.ConnectError) as ei:
        auth.validate_and_derive(private_key=_KEY, wallet_address=None)
    # key-safe message: type name only, never the key
    assert "ValueError" in str(ei.value) and _KEY not in str(ei.value)


def test_validate_get_address_failure_raises_connecterror(monkeypatch):
    class _C:
        def get_address(self):
            raise RuntimeError("no addr")
    monkeypatch.setattr(auth, "ClobClient", lambda **kw: _C())
    with pytest.raises(auth.ConnectError) as ei:
        auth.validate_and_derive(private_key=_KEY, wallet_address=None)
    assert "RuntimeError" in str(ei.value)


def test_validate_api_creds_derive_failure_raises_connecterror(monkeypatch):
    class _C:
        def get_address(self):
            return _SIGNER

        def create_or_derive_api_creds(self):
            raise RuntimeError("derive fail")
    monkeypatch.setattr(auth, "ClobClient", lambda **kw: _C())
    with pytest.raises(auth.ConnectError) as ei:
        auth.validate_and_derive(private_key=_KEY, wallet_address=None)
    assert "RuntimeError" in str(ei.value)


def test_validate_balance_failure_is_best_effort_zero(monkeypatch):
    class _C:
        def get_address(self):
            return _SIGNER

        def create_or_derive_api_creds(self):
            return SimpleNamespace(api_key="k", api_secret="s", api_passphrase="p")

        def set_api_creds(self, creds):
            pass

        def get_balance_allowance(self, params):
            raise RuntimeError("balance read blew up")
    monkeypatch.setattr(auth, "ClobClient", lambda **kw: _C())
    res = auth.validate_and_derive(private_key=_KEY, wallet_address=None)
    assert res.balance_usdc == 0.0  # best-effort: failure → 0, still returns creds
    assert res.creds.has_api_creds


def test_validate_balance_non_dict_yields_zero(monkeypatch):
    class _C:
        def get_address(self):
            return _SIGNER

        def create_or_derive_api_creds(self):
            return SimpleNamespace(api_key="k", api_secret="s", api_passphrase="p")

        def set_api_creds(self, creds):
            pass

        def get_balance_allowance(self, params):
            return ["not", "a", "dict"]  # unexpected shape → balance stays 0
    monkeypatch.setattr(auth, "ClobClient", lambda **kw: _C())
    res = auth.validate_and_derive(private_key=_KEY, wallet_address=None)
    assert res.balance_usdc == 0.0


def test_validate_happy_with_funder_resolves_account_address(monkeypatch):
    funder = "0x" + "c" * 40
    monkeypatch.setattr(auth, "ClobClient", _good_clob)
    res = auth.validate_and_derive(private_key=_KEY, wallet_address=None,
                                   signature_type=1, funder_address=funder)
    # no wallet_address given → resolved_wallet = funder; balance 2 USDC
    assert res.creds.wallet_address == funder and res.creds.funder_address == funder
    assert res.balance_usdc == 2.0
    assert res.creds.private_key == _KEY  # key carried in-memory on the result


def test_validate_returns_connectresult_with_creds(monkeypatch):
    monkeypatch.setattr(auth, "ClobClient", _good_clob)
    res = auth.validate_and_derive(private_key=_KEY, wallet_address=None)
    assert isinstance(res, auth.ConnectResult)
    assert res.creds.wallet_address == _SIGNER  # EOA: account = derived signer
    assert (res.creds.api_key, res.creds.api_secret, res.creds.api_passphrase) == ("k", "s", "p")
