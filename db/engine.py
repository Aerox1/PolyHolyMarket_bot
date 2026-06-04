"""Database engines & sessions.

* **Sync** engine/session — used by the dashboard (FastAPI), alembic and tests.
* **Async** engine/session — used by the Telegram bot (PTB 22.x is asyncio).

Both bind to the same ``db.models.Base.metadata``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker

from core.config import settings
from db.models import Base


def _sqlite_pragmas(dbapi_conn, _record) -> None:
    """WAL + busy_timeout so multiple processes (bot/webapp/dashboard) can share
    a single SQLite file in local dev without 'database is locked' errors.

    WAL can be disabled (``SQLITE_WAL=0``) — the test suite does this because WAL
    gives the sync + async engines divergent read snapshots of the same file."""
    import os

    cur = dbapi_conn.cursor()
    if os.environ.get("SQLITE_WAL", "1") != "0":
        cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA foreign_keys=ON")  # enforce FKs/CASCADE like Postgres
    cur.close()


_IS_SQLITE = settings.database_url.startswith("sqlite")

# ── Sync (dashboard / worker / alembic / tests) ──────────────────────────────

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    future=True,
)
if _IS_SQLITE:
    event.listen(engine, "connect", _sqlite_pragmas)
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
        kwargs: dict = {"pool_pre_ping": True, "future": True}
        if not _IS_SQLITE:  # QueuePool sizing applies to Postgres, not the sqlite dev/test engine
            kwargs["pool_size"] = settings.db_pool_size
            kwargs["max_overflow"] = settings.db_max_overflow
        _async_engine = create_async_engine(settings.async_database_url, **kwargs)
        if _IS_SQLITE:
            event.listen(_async_engine.sync_engine, "connect", _sqlite_pragmas)
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
