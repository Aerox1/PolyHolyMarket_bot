"""AccountManager — builds & caches per-(user, account) Polymarket clients.

There is **no global client**. Signing clients are built on demand from the
CredentialStore (which decrypts the key), cached with TTL + LRU, and protected
by a per-key lock so concurrent updates from the same user don't build
duplicates. Blocking py-clob-client calls are pushed to a thread by callers via
``asyncio.to_thread``; construction itself is also offloaded.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass

from polymarket.client import Polymarket
from polymarket.credentials import (
    AccountMeta,
    CredentialStore,
    NoAccountConnected,
    PolymarketCreds,
)

logger = logging.getLogger(__name__)


@dataclass
class _Entry:
    client: Polymarket
    created: float

    def expired(self, ttl: float) -> bool:
        return (time.monotonic() - self.created) > ttl

    def dispose(self) -> None:
        try:
            self.client.close()
        except Exception:
            pass


class AccountManager:
    def __init__(
        self,
        store: CredentialStore,
        *,
        ttl: float = 600.0,
        max_clients: int = 256,
    ) -> None:
        self._store = store
        self._ttl = ttl
        self._max = max_clients
        self._cache: "OrderedDict[tuple[int, int], _Entry]" = OrderedDict()
        self._locks: dict[tuple[int, int], asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()

    # ── public API ───────────────────────────────────────────────────────────

    async def get_trading_client(self, user_id: int, account_id: int | None = None) -> Polymarket:
        """Signing-capable client (decrypts the key). For balance/orders/trade."""
        account_id = account_id if account_id is not None else await self._store.default_account_id(user_id)
        if account_id is None:
            raise NoAccountConnected(user_id)
        key = (user_id, account_id)
        async with await self._lock_for(key):
            entry = self._cache.get(key)
            if entry and not entry.expired(self._ttl):
                self._cache.move_to_end(key)
                return entry.client
            if entry:  # expired
                entry.dispose()
                self._cache.pop(key, None)
            creds = await self._store.load_decrypted_creds(user_id, account_id)
            client = await asyncio.to_thread(Polymarket.from_creds, creds)
            self._put(key, client)
            return client

    async def get_readonly_client(self, user_id: int, account_id: int | None = None) -> Polymarket:
        """Address-only client (NO key decryption). For positions/markets."""
        address = await self._store.get_wallet_address(user_id, account_id)
        if address is None:
            raise NoAccountConnected(user_id)
        return await asyncio.to_thread(Polymarket.from_creds, PolymarketCreds.read_only(address))

    async def list_accounts(self, user_id: int) -> list[AccountMeta]:
        return await self._store.list_accounts(user_id)

    async def default_account_id(self, user_id: int, account_id: int | None = None) -> int | None:
        """Resolve which account a trade/log should target."""
        if account_id is not None:
            return account_id
        return await self._store.default_account_id(user_id)

    def invalidate(self, user_id: int, account_id: int | None = None) -> None:
        """Drop cached client(s) on connect/disconnect/error."""
        if account_id is not None:
            entry = self._cache.pop((user_id, account_id), None)
            if entry:
                entry.dispose()
            return
        for key in [k for k in self._cache if k[0] == user_id]:
            self._cache.pop(key).dispose()

    def clear(self) -> None:
        for entry in self._cache.values():
            entry.dispose()
        self._cache.clear()

    # ── internals ─────────────────────────────────────────────────────────────

    async def _lock_for(self, key: tuple[int, int]) -> asyncio.Lock:
        async with self._global_lock:
            return self._locks.setdefault(key, asyncio.Lock())

    def _put(self, key: tuple[int, int], client: Polymarket) -> None:
        self._cache[key] = _Entry(client=client, created=time.monotonic())
        self._cache.move_to_end(key)
        while len(self._cache) > self._max:
            _, evicted = self._cache.popitem(last=False)
            evicted.dispose()
