"""Credential contract shared by the Polymarket client, AccountManager and the
DB credential store.

``PolymarketCreds`` carries a plaintext private key ONLY for the in-memory
lifetime of a signing client. It is never persisted or logged in this form —
the DB stores ciphertext (see ``db.repositories.accounts``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class PolymarketCreds:
    """Everything needed to build a per-user Polymarket client.

    - ``private_key is None`` → read-only client (Data API + public CLOB/Gamma),
      usable for positions/portfolio/market data with just the wallet address.
    - ``private_key`` set → signing client (L1+), can place/cancel orders.

    Secret fields are ``repr=False`` so an accidental ``repr(creds)`` /
    ``f"{creds}"`` / ``logger.debug(creds)`` can never leak the key.
    """

    wallet_address: str
    signature_type: int = 0            # 0=EOA, 1=POLY_PROXY, 2=GNOSIS_SAFE
    private_key: str | None = field(default=None, repr=False)
    funder_address: str | None = None
    api_key: str | None = None
    api_secret: str | None = field(default=None, repr=False)
    api_passphrase: str | None = field(default=None, repr=False)

    @property
    def has_private_key(self) -> bool:
        return bool(self.private_key)

    @property
    def has_api_creds(self) -> bool:
        return bool(self.api_key and self.api_secret and self.api_passphrase)

    @classmethod
    def read_only(cls, wallet_address: str) -> "PolymarketCreds":
        return cls(wallet_address=wallet_address)


@dataclass(frozen=True)
class AccountMeta:
    """Non-secret account metadata for listing/selection."""

    account_id: int
    label: str
    wallet_address: str
    signature_type: int
    mode: str
    status: str
    is_active: bool


# ── Errors ───────────────────────────────────────────────────────────────────

class NoAccountConnected(Exception):
    """User has no (matching) connected Polymarket account."""

    def __init__(self, user_id: int):
        super().__init__(f"user {user_id} has no connected account")
        self.user_id = user_id


class WalletMismatchError(Exception):
    """The private key does not control the claimed wallet address."""

    def __init__(self, derived: str, claimed: str):
        super().__init__("private key does not match the provided wallet address")
        self.derived = derived
        self.claimed = claimed


class TradingUnavailable(Exception):
    """Account exists but cannot sign orders (no usable private key / L2)."""


# ── Credential store contract (implemented by db.repositories.accounts) ───────

@runtime_checkable
class CredentialStore(Protocol):
    """Source of per-user credentials for the AccountManager.

    The implementation owns DB access and decryption; the AccountManager only
    consumes this interface, so it never touches ciphertext or the DB directly.
    """

    async def default_account_id(self, user_id: int) -> int | None: ...

    async def get_wallet_address(self, user_id: int, account_id: int | None = None) -> str | None: ...

    async def load_decrypted_creds(self, user_id: int, account_id: int | None = None) -> PolymarketCreds:
        """Decrypt and return full signing credentials. Raises NoAccountConnected."""
        ...

    async def list_accounts(self, user_id: int) -> list[AccountMeta]: ...
