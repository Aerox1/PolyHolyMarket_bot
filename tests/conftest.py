"""Test configuration — set env BEFORE any app import, then provide a DB fixture.

``core.config.settings`` and ``db.engine.engine`` are built at import time from
the environment, so these must be set here at the very top.
"""

import os
import tempfile

from cryptography.fernet import Fernet

_TMPDIR = tempfile.mkdtemp(prefix="pmbot-test-")

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDIR}/test.db"
# Pin the ASYNC url to the SAME temp DB. Without this, a developer's .env
# DATABASE_URL_ASYNC (e.g. the persistent ./pmbot.db) leaks into the async engine
# while the sync engine uses the temp DB — divergent schemas across the two.
os.environ["DATABASE_URL_ASYNC"] = f"sqlite+aiosqlite:///{_TMPDIR}/test.db"
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("SESSION_SECRET", "test-session-secret")
# Isolate the suite from a developer's local .env (which may enable dev-auth or
# carry a real bot token / gemini key). Force the test-relevant flags here.
os.environ["WEBAPP_DEV_AUTH"] = "false"
os.environ["GEMINI_API_KEY"] = ""
os.environ["TELEGRAM_BOT_TOKEN"] = "test-token"
# Disable SQLite WAL in tests: WAL gives the sync + async engines divergent read
# snapshots of the same file, which broke cross-engine test isolation.
os.environ["SQLITE_WAL"] = "0"

import pytest  # noqa: E402

from db.engine import SessionLocal, create_all  # noqa: E402
from db.models import Base  # noqa: E402
from db.engine import engine  # noqa: E402

# Pin the ASYNC engine to a NullPool in tests. asyncio_mode=auto gives every async
# test its own event loop, but aiosqlite binds each connection's worker thread to
# the loop that opened it. A pooled connection that survives into a later test (a
# new loop) — then disposed by the sync _clean_db teardown — makes that thread fire
# on a now-closed loop ("RuntimeError: Event loop is closed"). NullPool opens+closes
# a fresh connection per session within its own loop, so nothing crosses loops.
# Test-only; production keeps the default pool (one long-lived loop, no issue).
import db.engine as _dbe  # noqa: E402
from sqlalchemy import event as _event  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession as _AsyncSession,
    async_sessionmaker as _async_sessionmaker,
    create_async_engine as _create_async_engine,
)
from sqlalchemy.pool import NullPool  # noqa: E402

_dbe._async_engine = _create_async_engine(
    os.environ["DATABASE_URL_ASYNC"], poolclass=NullPool, future=True
)
_event.listen(_dbe._async_engine.sync_engine, "connect", _dbe._sqlite_pragmas)
_dbe._AsyncSessionLocal = _async_sessionmaker(
    bind=_dbe._async_engine, expire_on_commit=False, class_=_AsyncSession
)


@pytest.fixture(scope="session", autouse=True)
def _schema():
    create_all()
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture(autouse=True)
def _clean_db(_schema):
    """Give every test a pristine DB.

    The bot/webapp use a SEPARATE async engine on the same sqlite file, so a plain
    DELETE under WAL isn't reliably visible across engines (it left orphaned rows
    + reused ids). Dropping+recreating the schema resets rowids and leaves no
    orphans; disposing both pools forces fresh connections to the clean state."""
    import db.engine as dbe
    if dbe._async_engine is not None:
        dbe._async_engine.sync_engine.dispose()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    if dbe._async_engine is not None:
        dbe._async_engine.sync_engine.dispose()
    engine.dispose()


@pytest.fixture(autouse=True)
def _clear_markets_cache():
    """The markets TTL cache is process-global; clear it around each test so a
    cached read can't leak across tests."""
    from polymarket import markets
    markets.clear_cache()
    yield
    markets.clear_cache()


@pytest.fixture
def session():
    s = SessionLocal()
    try:
        yield s
        s.rollback()
    finally:
        s.close()
