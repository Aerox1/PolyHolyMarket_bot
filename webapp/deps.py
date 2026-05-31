"""Webapp dependencies: async DB session, AccountManager, and the Telegram
Mini App auth dependency (validate initData → resolve our user → status gate).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.status import HTTP_401_UNAUTHORIZED, HTTP_403_FORBIDDEN

from core.config import settings
from db.engine import async_session_factory
from db.models import User, UserStatus
from db.repositories import users as users_repo
from polymarket.account_manager import AccountManager
from webapp.initdata import InitDataError, validate


async def get_db() -> AsyncIterator[AsyncSession]:
    session = async_session_factory()()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


def manager(request: Request) -> AccountManager:
    return request.app.state.account_manager


async def current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_telegram_init_data: str | None = Header(None, alias="X-Telegram-Init-Data"),
) -> User:
    try:
        tg = validate(x_telegram_init_data or "")
    except InitDataError as exc:
        raise HTTPException(status_code=HTTP_401_UNAUTHORIZED, detail=str(exc))

    user = await users_repo.get_or_create_user(
        db,
        telegram_id=tg.id,
        username=tg.username,
        first_name=tg.first_name,
        default_language=(tg.language_code or settings.default_language),
    )
    if user.status in (UserStatus.SUSPENDED.value, UserStatus.BANNED.value):
        raise HTTPException(status_code=HTTP_403_FORBIDDEN, detail=user.status)
    return user
