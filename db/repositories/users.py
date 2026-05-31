"""User & settings repository (async — used by the bot).

All functions take an ``AsyncSession``; the caller manages the transaction
(see ``db.engine.async_session_scope``).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.i18n import normalize_lang
from db.models import User, UserSettings, UserStatus


async def get_or_create_user(
    session: AsyncSession,
    telegram_id: int,
    *,
    username: str | None = None,
    first_name: str | None = None,
    default_language: str = "en",
) -> User:
    user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
    if user is None:
        user = User(
            telegram_id=telegram_id,
            username=username,
            first_name=first_name,
            language=normalize_lang(default_language),
        )
        session.add(user)
        await session.flush()
        session.add(UserSettings(user_id=user.id))
        await session.flush()
    else:
        # keep username/first_name fresh
        if username and user.username != username:
            user.username = username
        if first_name and user.first_name != first_name:
            user.first_name = first_name
    return user


async def get_user(session: AsyncSession, telegram_id: int) -> User | None:
    return await session.scalar(select(User).where(User.telegram_id == telegram_id))


async def set_language(session: AsyncSession, telegram_id: int, language: str) -> None:
    user = await get_user(session, telegram_id)
    if user:
        user.language = normalize_lang(language)


async def set_active_account(session: AsyncSession, telegram_id: int, account_id: int | None) -> None:
    user = await get_user(session, telegram_id)
    if user:
        user.active_account_id = account_id


async def get_settings(session: AsyncSession, user_id: int) -> UserSettings | None:
    return await session.get(UserSettings, user_id)


async def is_blocked(session: AsyncSession, telegram_id: int) -> bool:
    """True if the user is suspended or banned."""
    user = await get_user(session, telegram_id)
    return bool(user and user.status in (UserStatus.SUSPENDED.value, UserStatus.BANNED.value))
