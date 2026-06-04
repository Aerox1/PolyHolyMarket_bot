"""AccountManager caching/lock/invalidate. Polymarket.from_creds is mocked so
no network/crypto/real client is built — we test the manager's logic only."""

import pytest

import polymarket.account_manager as am_mod
from polymarket.account_manager import AccountManager
from polymarket.credentials import AccountMeta, NoAccountConnected, PolymarketCreds


class FakeClient:
    def __init__(self, creds: PolymarketCreds):
        self.creds = creds
        self.order_signing_ready = creds.has_private_key
        self.closed = False

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _mock_from_creds(monkeypatch):
    """Replace the real client builder with FakeClient."""
    monkeypatch.setattr(am_mod.Polymarket, "from_creds", staticmethod(lambda creds: FakeClient(creds)))


class FakeStore:
    def __init__(self):
        self.decrypt_calls = 0
        self._accounts = {
            1: PolymarketCreds(wallet_address="0x" + "a" * 40, private_key="0x" + "b" * 64),
        }

    async def default_account_id(self, user_id):
        return 7 if user_id in self._accounts else None

    async def get_wallet_address(self, user_id, account_id=None):
        creds = self._accounts.get(user_id)
        return creds.wallet_address if creds else None

    async def load_decrypted_creds(self, user_id, account_id=None):
        if user_id not in self._accounts:
            raise NoAccountConnected(user_id)
        self.decrypt_calls += 1
        return self._accounts[user_id]

    async def list_accounts(self, user_id):
        if user_id not in self._accounts:
            return []
        return [AccountMeta(7, "Main", self._accounts[user_id].wallet_address, 0, "live", "active", True)]


async def test_trading_client_is_cached():
    store = FakeStore()
    mgr = AccountManager(store)
    c1 = await mgr.get_trading_client(1)
    c2 = await mgr.get_trading_client(1)
    assert c1 is c2
    assert store.decrypt_calls == 1


async def test_invalidate_forces_rebuild():
    store = FakeStore()
    mgr = AccountManager(store)
    await mgr.get_trading_client(1)
    mgr.invalidate(1)
    await mgr.get_trading_client(1)
    assert store.decrypt_calls == 2


async def test_invalidate_prunes_idle_lock():
    # The per-key lock must be reclaimed with the cached client, so the lock map
    # doesn't grow unbounded over the process lifetime.
    store = FakeStore()
    mgr = AccountManager(store)
    await mgr.get_trading_client(1)
    assert (1, 7) in mgr._locks
    mgr.invalidate(1)
    assert (1, 7) not in mgr._locks


async def test_clear_drops_idle_locks():
    store = FakeStore()
    mgr = AccountManager(store)
    await mgr.get_trading_client(1)
    assert mgr._locks
    mgr.clear()
    assert mgr._locks == {}


async def test_no_account_raises():
    mgr = AccountManager(FakeStore())
    with pytest.raises(NoAccountConnected):
        await mgr.get_trading_client(2)
    with pytest.raises(NoAccountConnected):
        await mgr.get_readonly_client(2)


async def test_readonly_client_does_not_decrypt():
    store = FakeStore()
    mgr = AccountManager(store)
    ro = await mgr.get_readonly_client(1)
    assert ro.order_signing_ready is False  # read-only creds have no key
    assert store.decrypt_calls == 0
    ro.close()


async def test_lru_eviction_bounds_cache():
    store = FakeStore()
    for uid in range(2, 60):
        store._accounts[uid] = PolymarketCreds(wallet_address=f"0x{uid:040x}", private_key="0x" + "c" * 64)
    mgr = AccountManager(store, max_clients=4)
    clients = [await mgr.get_trading_client(uid) for uid in range(1, 20)]
    assert len(mgr._cache) <= 4
    # evicted clients were disposed
    assert any(c.closed for c in clients)
