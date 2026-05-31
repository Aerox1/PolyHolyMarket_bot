"""Account repository + DbCredentialStore.

This module is the **encryption boundary**: it is the only place that turns a
plaintext private key into ciphertext (on save) and back (on load for signing),
via ``core.crypto``. The dashboard process — which has no ``ENCRYPTION_KEY`` —
can call the non-decrypting helpers but will raise if it attempts to decrypt.
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core import crypto
from db.models import Account, AccountStatus, User
from polymarket.credentials import (
    AccountMeta,
    CredentialStore,
    NoAccountConnected,
    PolymarketCreds,
)

logger = logging.getLogger(__name__)


# ── query helpers (no decryption) ────────────────────────────────────────────

async def get_account(session: AsyncSession, account_id: int) -> Account | None:
    return await session.get(Account, account_id)


async def list_accounts(session: AsyncSession, user_id: int) -> list[Account]:
    return list(
        await session.scalars(
            select(Account).where(Account.user_id == user_id).order_by(Account.created_at)
        )
    )


async def resolve_account(
    session: AsyncSession, user_id: int, account_id: int | None = None
) -> Account | None:
    """Pick the requested account, else the user's active one, else the first."""
    if account_id is not None:
        acc = await get_account(session, account_id)
        return acc if acc and acc.user_id == user_id else None
    user = await session.get(User, user_id)
    if user and user.active_account_id:
        acc = await get_account(session, user.active_account_id)
        if acc and acc.user_id == user_id:
            return acc
    return await session.scalar(
        select(Account).where(Account.user_id == user_id).order_by(Account.created_at).limit(1)
    )


# ── encryption boundary ──────────────────────────────────────────────────────

def _encrypt_api_creds(creds: PolymarketCreds) -> str | None:
    if not creds.has_api_creds:
        return None
    blob = json.dumps(
        {"api_key": creds.api_key, "api_secret": creds.api_secret, "api_passphrase": creds.api_passphrase}
    )
    return crypto.encrypt(blob)


def _decrypt_api_creds(ciphertext: str | None) -> dict:
    if not ciphertext:
        return {}
    try:
        return json.loads(crypto.decrypt(ciphertext))
    except Exception:
        return {}


async def upsert_account(
    session: AsyncSession,
    user_id: int,
    creds: PolymarketCreds,
    *,
    label: str = "Main",
    mode: str = "live",
) -> Account:
    """Create or update a user's account, encrypting the private key + API creds.

    Matches on (user_id, wallet_address) so reconnecting the same wallet updates
    in place. The plaintext key in ``creds`` is encrypted here and not retained.
    """
    if not creds.has_private_key:
        raise ValueError("upsert_account requires a private key to encrypt")

    acc = await session.scalar(
        select(Account).where(Account.user_id == user_id, Account.wallet_address == creds.wallet_address)
    )
    enc_key = crypto.encrypt(creds.private_key)  # type: ignore[arg-type]
    enc_api = _encrypt_api_creds(creds)
    if acc is None:
        acc = Account(
            user_id=user_id,
            label=label,
            wallet_address=creds.wallet_address,
            signature_type=creds.signature_type,
            funder_address=creds.funder_address,
            encrypted_private_key=enc_key,
            encrypted_api_creds=enc_api,
            mode=mode,
            status=AccountStatus.ACTIVE.value,
        )
        session.add(acc)
    else:
        acc.signature_type = creds.signature_type
        acc.funder_address = creds.funder_address
        acc.encrypted_private_key = enc_key
        acc.encrypted_api_creds = enc_api
        acc.status = AccountStatus.ACTIVE.value
        acc.last_sync_error = None
    await session.flush()
    return acc


async def delete_account(session: AsyncSession, user_id: int, account_id: int) -> bool:
    acc = await get_account(session, account_id)
    if acc and acc.user_id == user_id:
        await session.delete(acc)
        return True
    return False


def account_to_creds(acc: Account) -> PolymarketCreds:
    """Decrypt an Account row into signing credentials (in-memory only)."""
    api = _decrypt_api_creds(acc.encrypted_api_creds)
    return PolymarketCreds(
        wallet_address=acc.wallet_address,
        signature_type=acc.signature_type,
        private_key=crypto.decrypt(acc.encrypted_private_key),
        funder_address=acc.funder_address,
        api_key=api.get("api_key"),
        api_secret=api.get("api_secret"),
        api_passphrase=api.get("api_passphrase"),
    )


# ── CredentialStore implementation (consumed by AccountManager) ───────────────

class DbCredentialStore(CredentialStore):
    """Async credential store backed by the DB. Opens its own sessions so the
    AccountManager can call it without holding a session."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._sf = session_factory

    async def default_account_id(self, user_id: int) -> int | None:
        async with self._sf() as s:
            acc = await resolve_account(s, user_id)
            return acc.id if acc else None

    async def get_wallet_address(self, user_id: int, account_id: int | None = None) -> str | None:
        async with self._sf() as s:
            acc = await resolve_account(s, user_id, account_id)
            return acc.wallet_address if acc else None

    async def load_decrypted_creds(self, user_id: int, account_id: int | None = None) -> PolymarketCreds:
        async with self._sf() as s:
            acc = await resolve_account(s, user_id, account_id)
            if acc is None:
                raise NoAccountConnected(user_id)
            return account_to_creds(acc)  # decryption happens here only

    async def list_accounts(self, user_id: int) -> list[AccountMeta]:
        async with self._sf() as s:
            user = await s.get(User, user_id)
            active = user.active_account_id if user else None
            rows = await list_accounts(s, user_id)
            return [
                AccountMeta(
                    account_id=a.id,
                    label=a.label,
                    wallet_address=a.wallet_address,
                    signature_type=a.signature_type,
                    mode=a.mode,
                    status=a.status,
                    is_active=(a.id == active),
                )
                for a in rows
            ]
