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

import pytest  # noqa: E402

from db.engine import SessionLocal, create_all  # noqa: E402
from db.models import Base  # noqa: E402
from db.engine import engine  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _schema():
    create_all()
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def session():
    s = SessionLocal()
    try:
        yield s
        s.rollback()
    finally:
        s.close()
