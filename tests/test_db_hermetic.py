"""Guard: the test suite must NOT bind either engine to a developer's persistent
dev DB (./pmbot.db). conftest pins both DATABASE_URL and DATABASE_URL_ASYNC to a
temp file; this fails loudly if that pin ever regresses (e.g. import-order change
freezes the lru_cached settings against a real .env)."""

from core.config import settings
from db.engine import engine


def test_engines_use_temp_db_not_dev_db():
    sync_url = str(engine.url)
    async_url = settings.async_database_url
    assert "pmbot.db" not in sync_url, f"sync engine leaked to dev DB: {sync_url}"
    assert "pmbot.db" not in async_url, f"async engine leaked to dev DB: {async_url}"
    assert async_url.startswith("sqlite+aiosqlite:")
    # sync + async must address the SAME physical file
    assert sync_url.rsplit("/", 1)[-1] == async_url.rsplit("/", 1)[-1]
