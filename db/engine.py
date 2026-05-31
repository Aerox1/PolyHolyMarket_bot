"""Database engines & sessions.

* **Sync** engine/session — used by the dashboard (FastAPI), alembic and tests.
* **Async** engine/session — used by the Telegram bot (PTB 22.x is asyncio).

Both bind to the same ``db.models.Base.metadata``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker

from core.config import settings
from db.models import Base

# ── Sync (dashboard / worker / alembic / tests) ──────────────────────────────

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    future=True,
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


@contextmanager
def session_scope() -> Iterator[Session]:
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


# ── Async (bot) ──────────────────────────────────────────────────────────────

_async_engine = None
_AsyncSessionLocal: async_sessionmaker[AsyncSession] | None = None


def async_engine():
    global _async_engine
    if _async_engine is None:
        _async_engine = create_async_engine(settings.async_database_url, pool_pre_ping=True, future=True)
    return _async_engine


def async_session_factory() -> async_sessionmaker[AsyncSession]:
    global _AsyncSessionLocal
    if _AsyncSessionLocal is None:
        _AsyncSessionLocal = async_sessionmaker(bind=async_engine(), expire_on_commit=False, class_=AsyncSession)
    return _AsyncSessionLocal


@asynccontextmanager
async def async_session_scope() -> AsyncIterator[AsyncSession]:
    s = async_session_factory()()
    try:
        yield s
        await s.commit()
    except Exception:
        await s.rollback()
        raise
    finally:
        await s.close()


def create_all() -> None:
    """Create all tables on the sync engine. For dev/test bootstrap only;
    production uses ``alembic upgrade head``."""
    Base.metadata.create_all(engine)
