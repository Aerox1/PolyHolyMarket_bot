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


@pytest.fixture
def session():
    s = SessionLocal()
    try:
        yield s
        s.rollback()
    finally:
        s.close()
