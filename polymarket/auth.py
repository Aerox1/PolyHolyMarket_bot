"""Connect-account validation — derive address, verify ownership, derive API
creds, read balance.

Synchronous (the bot runs it via ``asyncio.to_thread``). Ported from Polygen's
``scripts/setup_live.py``. NEVER logs the private key or includes it in raised
exceptions.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import BalanceAllowanceParams

from core.config import settings
from polymarket.credentials import PolymarketCreds, WalletMismatchError

logger = logging.getLogger(__name__)

_HEX_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_HEX_PRIVKEY = re.compile(r"^0x[0-9a-fA-F]{64}$")


class ConnectError(Exception):
    """Validation failed for a non-mismatch reason. Message is key-safe."""


@dataclass(frozen=True)
class ConnectResult:
    creds: PolymarketCreds   # includes derived API creds + plaintext key (in memory)
    balance_usdc: float


def is_valid_address(value: str) -> bool:
    return bool(_HEX_ADDRESS.match(value.strip()))


def normalize_private_key(raw: str) -> str | None:
    """Return a 0x-prefixed 64-hex key, or None if it doesn't look valid.
    Does not log the value."""
    k = raw.strip()
    if not k.startswith("0x"):
        k = "0x" + k
    return k if _HEX_PRIVKEY.match(k) else None


def _parse_usdc(raw) -> float:
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return val / 1e6 if val > 1_000_000 else val  # atomic units → USDC


def validate_and_derive(
    *,
    private_key: str,
    wallet_address: str,
    signature_type: int = 0,
    funder_address: str | None = None,
) -> ConnectResult:
    """Validate a key against a wallet, derive API creds, read balance.

    Raises ``WalletMismatchError`` if the key controls a different address, or
    ``ConnectError`` for any other failure (with a key-safe message).
    """
    try:
        kwargs: dict = {
            "host": settings.clob_url,
            "chain_id": settings.chain_id,
            "key": private_key,
            "signature_type": signature_type,
        }
        if funder_address:
            kwargs["funder"] = funder_address
        client = ClobClient(**kwargs)
    except Exception as exc:
        raise ConnectError(f"Invalid private key ({type(exc).__name__}).") from None

    # Derive the SIGNER address from the key. We no longer require the user to
    # type it — the key is the source of truth (a wallet_address arg, if given,
    # is still cross-checked for backward compatibility).
    try:
        derived = client.get_address()
    except Exception as exc:
        raise ConnectError(f"Could not derive address ({type(exc).__name__}).") from None
    if wallet_address and derived and derived.lower() != wallet_address.lower():
        raise WalletMismatchError(derived, wallet_address)
    # The ACCOUNT address (where positions/balance live, shown to the user) is the
    # funder for proxy/Safe accounts, else the signer EOA.
    resolved_wallet = wallet_address or funder_address or derived

    # Derive L2 API creds.
    api_key = api_secret = api_passphrase = None
    try:
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
        api_key, api_secret, api_passphrase = creds.api_key, creds.api_secret, creds.api_passphrase
    except Exception as exc:
        raise ConnectError(f"Could not derive API credentials ({type(exc).__name__}).") from None

    # Read balance (best-effort — 0 is fine, e.g. unfunded account).
    balance = 0.0
    try:
        bal = client.get_balance_allowance(BalanceAllowanceParams(asset_type="COLLATERAL"))
        if isinstance(bal, dict):
            balance = _parse_usdc(bal.get("balance", 0))
    except Exception as exc:
        logger.warning("Balance check failed during connect: %s", type(exc).__name__)

    return ConnectResult(
        creds=PolymarketCreds(
            wallet_address=resolved_wallet,
            signature_type=signature_type,
            private_key=private_key,
            funder_address=funder_address,
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase,
        ),
        balance_usdc=balance,
    )
