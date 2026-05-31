"""End-to-end: upsert_account encrypts; DbCredentialStore decrypts. Uses async
in-memory SQLite (aiosqlite) so the real async DB path is exercised."""

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from db.models import Base
from db.repositories import accounts as ar
from db.repositories import users as ur
from polymarket.credentials import NoAccountConnected, PolymarketCreds


@pytest.fixture
async def session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_upsert_encrypts_and_store_decrypts(session_factory):
    real_key = "0x" + "d" * 64
    async with session_factory() as s:
        user = await ur.get_or_create_user(s, telegram_id=555, username="x")
        uid = user.id
        creds = PolymarketCreds(
            wallet_address="0x" + "a" * 40,
            private_key=real_key,
            api_key="api-key-xyz", api_secret="super-secret-abc", api_passphrase="passphrase-qrs",
        )
        acc = await ar.upsert_account(s, uid, creds, label="Main")
        # ciphertext, not plaintext
        assert "d" * 64 not in acc.encrypted_private_key
        assert acc.encrypted_api_creds and "super-secret-abc" not in acc.encrypted_api_creds
        await s.commit()

    store = ar.DbCredentialStore(session_factory)
    loaded = await store.load_decrypted_creds(uid)
    assert loaded.private_key == real_key
    assert loaded.api_key == "api-key-xyz" and loaded.api_passphrase == "passphrase-qrs"
    assert loaded.wallet_address == "0x" + "a" * 40

    metas = await store.list_accounts(uid)
    assert len(metas) == 1 and metas[0].label == "Main"
    assert await store.get_wallet_address(uid) == "0x" + "a" * 40


async def test_reconnect_same_wallet_updates_in_place(session_factory):
    async with session_factory() as s:
        user = await ur.get_or_create_user(s, telegram_id=556)
        uid = user.id
        await ar.upsert_account(s, uid, PolymarketCreds(wallet_address="0xW", private_key="0x" + "1" * 64))
        await ar.upsert_account(s, uid, PolymarketCreds(wallet_address="0xW", private_key="0x" + "2" * 64))
        await s.commit()
        accs = await ar.list_accounts(s, uid)
    assert len(accs) == 1  # same wallet → updated, not duplicated


async def test_delete_account(session_factory):
    async with session_factory() as s:
        user = await ur.get_or_create_user(s, telegram_id=557)
        uid = user.id
        acc = await ar.upsert_account(s, uid, PolymarketCreds(wallet_address="0xZ", private_key="0x" + "3" * 64))
        await s.commit()
        ok = await ar.delete_account(s, uid, acc.id)
        await s.commit()
    assert ok
    store = ar.DbCredentialStore(session_factory)
    with pytest.raises(NoAccountConnected):
        await store.load_decrypted_creds(uid)
