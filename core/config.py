"""Centralised configuration loaded from environment / .env.

Replaces Polygen's ``bot/config.py`` module-globals approach. NO per-user
secrets live here — only process-level config. Per-user wallet credentials are
stored encrypted in the database (see ``db.models.Account``).
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SUPPORTED_LANGUAGES = ("en", "fa", "ru", "zh")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── Telegram ──────────────────────────────────────────
    telegram_bot_token: str = Field("", alias="TELEGRAM_BOT_TOKEN")
    telegram_allowed_users: str = Field("", alias="TELEGRAM_ALLOWED_USERS")

    # ── Encryption ────────────────────────────────────────
    encryption_key: str = Field("", alias="ENCRYPTION_KEY")
    encryption_key_old: str = Field("", alias="ENCRYPTION_KEY_OLD")

    # ── Database ──────────────────────────────────────────
    database_url: str = Field(
        "postgresql+psycopg://pmbot:pmbot@localhost:5432/pmbot",
        alias="DATABASE_URL",
    )
    database_url_async: str = Field("", alias="DATABASE_URL_ASYNC")

    # ── Dashboard ─────────────────────────────────────────
    dashboard_host: str = Field("0.0.0.0", alias="DASHBOARD_HOST")
    dashboard_port: int = Field(8877, alias="DASHBOARD_PORT")
    session_secret: str = Field("", alias="SESSION_SECRET")
    admin_bootstrap_user: str = Field("admin", alias="ADMIN_BOOTSTRAP_USER")
    admin_bootstrap_password_hash: str = Field("", alias="ADMIN_BOOTSTRAP_PASSWORD_HASH")

    # ── Polymarket ────────────────────────────────────────
    clob_url: str = Field("https://clob.polymarket.com", alias="POLYMARKET_CLOB_URL")
    gamma_url: str = Field("https://gamma-api.polymarket.com", alias="POLYMARKET_GAMMA_URL")
    data_url: str = Field("https://data-api.polymarket.com", alias="POLYMARKET_DATA_URL")
    chain_id: int = Field(137, alias="CHAIN_ID")
    polymarket_signup_url: str = Field("https://polymarket.com", alias="POLYMARKET_SIGNUP_URL")

    # ── Behaviour ─────────────────────────────────────────
    default_language: str = Field("en", alias="DEFAULT_LANGUAGE")
    log_level: str = Field("INFO", alias="LOG_LEVEL")

    # ── Derived helpers ───────────────────────────────────
    @field_validator("default_language")
    @classmethod
    def _valid_lang(cls, v: str) -> str:
        return v if v in SUPPORTED_LANGUAGES else "en"

    @property
    def async_database_url(self) -> str:
        """Async URL — explicit value, or derived from the sync URL."""
        if self.database_url_async:
            return self.database_url_async
        url = self.database_url
        if url.startswith("postgresql+psycopg://"):
            return url.replace("postgresql+psycopg://", "postgresql+asyncpg://", 1)
        if url.startswith("postgresql://"):
            return url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return url

    @property
    def allowed_user_ids(self) -> set[int]:
        raw = self.telegram_allowed_users.strip()
        if not raw:
            return set()
        return {int(x.strip()) for x in raw.split(",") if x.strip()}

    @property
    def encryption_keys(self) -> list[str]:
        """All Fernet keys, newest first (current key + rotation fallbacks)."""
        keys = [self.encryption_key] if self.encryption_key else []
        if self.encryption_key_old:
            keys += [k.strip() for k in self.encryption_key_old.split(",") if k.strip()]
        return keys


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


settings = get_settings()
