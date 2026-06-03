"""Per-user Polymarket client — CLOB SDK + Data/Gamma REST.

Refactor of Polygen's ``bot/polymarket.py``: credentials come from a
``PolymarketCreds`` instance (per user) instead of module globals, and Data-API
calls are keyed by ``self._wallet_address``. All trading/read method bodies are
preserved.

Credential levels (verified against py-clob-client):
  * Data API (positions/trades/portfolio/activity) + Gamma + public CLOB →
    no key, only the wallet address.
  * get_balance / get_open_orders / place_* / cancel_* → require the private-key
    signer (L1) plus API creds (L2). API creds alone cannot trade.
"""

from __future__ import annotations

import logging

import httpx
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds,
    BalanceAllowanceParams,
    MarketOrderArgs,
    OpenOrderParams,
    OrderArgs,
    OrderType,
    PartialCreateOrderOptions,
    TradeParams,
)
from py_clob_client.order_builder.constants import BUY, SELL

from core.config import settings
from polymarket.credentials import PolymarketCreds, TradingUnavailable, WalletMismatchError

logger = logging.getLogger(__name__)

SIDE_MAP = {"buy": BUY, "sell": SELL}


class Polymarket:
    """Unified per-user Polymarket client."""

    def __init__(self, creds: PolymarketCreds) -> None:
        self._creds = creds
        self._wallet_address = creds.wallet_address
        self._http = httpx.Client(timeout=15)
        self._clob: ClobClient | None = None
        self._trading_ready = False
        self._order_signing_ready = False
        self._init_clob()

    # ── construction ─────────────────────────────────────────────────────────

    @classmethod
    def from_creds(cls, creds: PolymarketCreds) -> "Polymarket":
        """Build a client and, when a key is present, verify it controls the
        claimed wallet address."""
        pm = cls(creds)
        if creds.has_private_key and pm._clob is not None:
            try:
                derived = pm._clob.get_address()
            except Exception:
                derived = None
            if derived and creds.wallet_address and derived.lower() != creds.wallet_address.lower():
                raise WalletMismatchError(derived, creds.wallet_address)
        return pm

    def _init_clob(self) -> None:
        """Initialise the CLOB client at the right auth level.

        L0 no key · L1 key only (sign) · L2 key + API creds (full). The private
        key is required for L1+. API creds alone are insufficient — the SDK
        needs the signer for HMAC headers even on L2 calls.
        """
        creds = self._creds
        if not creds.has_private_key:
            # Read-only: public endpoints + Data API by address.
            self._clob = ClobClient(host=settings.clob_url, chain_id=settings.chain_id)
            return

        kwargs: dict = {
            "host": settings.clob_url,
            "chain_id": settings.chain_id,
            "key": creds.private_key,
            "signature_type": creds.signature_type,
        }
        if creds.funder_address:
            kwargs["funder"] = creds.funder_address
        self._clob = ClobClient(**kwargs)

        if creds.has_api_creds:
            self._clob.set_api_creds(
                ApiCreds(
                    api_key=creds.api_key,
                    api_secret=creds.api_secret,
                    api_passphrase=creds.api_passphrase,
                )
            )
            self._trading_ready = True
            self._order_signing_ready = True
        else:
            try:
                self._clob.set_api_creds(self._clob.create_or_derive_api_creds())
                self._trading_ready = True
                self._order_signing_ready = True
            except Exception:
                logger.warning("Could not derive API creds; client stays at L1 (sign only).")
                self._order_signing_ready = True

    # ── status ─────────────────────────────────────────────────────────────

    @property
    def wallet_address(self) -> str:
        return self._wallet_address

    @property
    def trading_ready(self) -> bool:
        return self._trading_ready

    @property
    def order_signing_ready(self) -> bool:
        return self._order_signing_ready

    def get_address(self) -> str | None:
        return self._clob.get_address() if self._clob else None

    def close(self) -> None:
        try:
            self._http.close()
        except Exception:
            pass

    # ── Data API (public, keyed by wallet address) ───────────────────────────

    def get_positions(self, limit: int = 50, offset: int = 0) -> dict:
        r = self._http.get(
            f"{settings.data_url}/positions",
            params={"user": self._wallet_address, "limit": limit, "offset": offset,
                    "sortBy": "CURRENT", "sortDirection": "DESC"},
        )
        r.raise_for_status()
        return r.json()

    def get_trades(self, limit: int = 20, offset: int = 0) -> dict:
        r = self._http.get(
            f"{settings.data_url}/trades",
            params={"user": self._wallet_address, "limit": limit, "offset": offset},
        )
        r.raise_for_status()
        return r.json()

    def get_portfolio_value(self) -> dict:
        r = self._http.get(f"{settings.data_url}/value", params={"user": self._wallet_address})
        r.raise_for_status()
        return r.json()

    def get_activity(self, limit: int = 20) -> dict:
        r = self._http.get(
            f"{settings.data_url}/activity",
            params={"user": self._wallet_address, "limit": limit},
        )
        r.raise_for_status()
        return r.json()

    # ── Gamma API (public market data) ───────────────────────────────────────

    def search_markets(self, query: str, limit: int = 10) -> list[dict]:
        r = self._http.get(
            f"{settings.gamma_url}/markets",
            params={"_limit": limit, "title_like": query, "active": True, "closed": False},
        )
        r.raise_for_status()
        return r.json()

    def search_events(self, query: str, limit: int = 10) -> list[dict]:
        r = self._http.get(
            f"{settings.gamma_url}/events",
            params={"_limit": limit, "title_like": query, "active": True, "closed": False},
        )
        r.raise_for_status()
        return r.json()

    def get_event(self, event_id: str) -> dict:
        r = self._http.get(f"{settings.gamma_url}/events/{event_id}")
        r.raise_for_status()
        return r.json()

    def get_market(self, condition_id: str) -> dict:
        r = self._http.get(f"{settings.gamma_url}/markets/{condition_id}")
        r.raise_for_status()
        return r.json()

    # ── CLOB public endpoints ────────────────────────────────────────────────

    def get_price(self, token_id: str, side: str = "buy") -> dict:
        r = self._http.get(f"{settings.clob_url}/price", params={"token_id": token_id, "side": side.upper()})
        r.raise_for_status()
        return r.json()

    def get_orderbook(self, token_id: str) -> dict:
        r = self._http.get(f"{settings.clob_url}/book", params={"token_id": token_id})
        r.raise_for_status()
        return r.json()

    def get_midpoint(self, token_id: str) -> dict:
        r = self._http.get(f"{settings.clob_url}/midpoint", params={"token_id": token_id})
        r.raise_for_status()
        return r.json()

    def get_spread(self, token_id: str) -> dict:
        r = self._http.get(f"{settings.clob_url}/spread", params={"token_id": token_id})
        r.raise_for_status()
        return r.json()

    # ── CLOB authenticated (read) ────────────────────────────────────────────

    def _require_trading(self) -> ClobClient:
        if not self._trading_ready or self._clob is None:
            raise TradingUnavailable("CLOB L2 auth not configured for this account.")
        return self._clob

    def _require_signing(self) -> ClobClient:
        if not self._order_signing_ready or self._clob is None:
            raise TradingUnavailable("Order signing unavailable — no private key for this account.")
        return self._clob

    def get_open_orders(self, market: str | None = None) -> list:
        clob = self._require_trading()
        params = OpenOrderParams(market=market) if market else None
        return clob.get_orders(params)

    def get_my_trades(self) -> list:
        return self._require_trading().get_trades(TradeParams())

    def get_balance(self) -> dict:
        """Raw USDC balance/allowance (atomic units, 6 decimals). L2 required."""
        clob = self._require_trading()
        return clob.get_balance_allowance(BalanceAllowanceParams(asset_type="COLLATERAL"))

    def get_order(self, order_id: str) -> dict:
        return self._require_trading().get_order(order_id)

    def get_open_orders_for_token(self, token_id: str) -> list:
        return self._require_trading().get_orders(OpenOrderParams(market=token_id))

    # ── CLOB authenticated (trading — requires private key) ──────────────────

    def place_limit_order(
        self,
        token_id: str,
        price: float,
        size: float,
        side: str,
        neg_risk: bool | None = None,
    ) -> dict:
        clob = self._require_signing()
        order_args = OrderArgs(token_id=token_id, price=price, size=size, side=SIDE_MAP[side.lower()])
        options = PartialCreateOrderOptions(neg_risk=neg_risk) if neg_risk is not None else None
        signed = clob.create_order(order_args, options)
        return clob.post_order(signed, OrderType.GTC)

    def place_market_order(self, token_id: str, amount: float, side: str) -> dict:
        clob = self._require_signing()
        order_args = MarketOrderArgs(token_id=token_id, amount=amount, side=SIDE_MAP[side.lower()])
        signed = clob.create_market_order(order_args)
        return clob.post_order(signed, OrderType.FOK)

    def place_capped_buy(
        self, token_id: str, amount: float, max_price: float, neg_risk: bool | None = None
    ) -> dict:
        """A slippage-guarded BUY: a FOK *limit* at ``max_price`` sized so the full
        USD ``amount`` is spent at that ceiling. Fills only at ≤ ``max_price`` (or
        not at all), unlike ``place_market_order`` whose FOK caps fill *size*, not
        *price* — a bare market buy can still fill at a materially worse average
        within available liquidity. Used for news-channel "Bet" CTAs, where the
        tap comes from a public message and the price may have moved since posting.
        """
        clob = self._require_signing()
        price = min(max(float(max_price), 0.01), 0.99)  # valid CLOB tick range
        size = round(float(amount) / price, 2)          # shares; cost ≤ amount at the ceiling
        order_args = OrderArgs(token_id=token_id, price=price, size=size, side=BUY)
        options = PartialCreateOrderOptions(neg_risk=neg_risk) if neg_risk is not None else None
        signed = clob.create_order(order_args, options)
        return clob.post_order(signed, OrderType.FOK)

    def cancel_order(self, order_id: str) -> dict:
        return self._require_signing().cancel(order_id)

    def cancel_all_orders(self) -> dict:
        return self._require_signing().cancel_all()

    def cancel_market_orders(self, market: str) -> dict:
        return self._require_signing().cancel_market_orders(market=market)
