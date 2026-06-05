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
    # Ignore system/env proxies for Telegram (a macOS system proxy / VPN otherwise
    # drops PTB's connections). Set true only if you MUST route via a proxy.
    telegram_trust_env: bool = Field(False, alias="TELEGRAM_TRUST_ENV")

    # ── Encryption ────────────────────────────────────────
    encryption_key: str = Field("", alias="ENCRYPTION_KEY")
    encryption_key_old: str = Field("", alias="ENCRYPTION_KEY_OLD")

    # ── Database ──────────────────────────────────────────
    database_url: str = Field(
        "postgresql+psycopg://pmbot:pmbot@localhost:5432/pmbot",
        alias="DATABASE_URL",
    )
    database_url_async: str = Field("", alias="DATABASE_URL_ASYNC")
    # Connection-pool sizing (applied to the Postgres engines). The bot's async
    # engine is the most concurrency-sensitive process, so it gets the same headroom
    # as the sync (dashboard/worker) engine instead of SQLAlchemy's tiny 5+10 default.
    db_pool_size: int = Field(10, alias="DB_POOL_SIZE")
    db_max_overflow: int = Field(20, alias="DB_MAX_OVERFLOW")

    # ── Dashboard ─────────────────────────────────────────
    dashboard_host: str = Field("0.0.0.0", alias="DASHBOARD_HOST")
    dashboard_port: int = Field(8877, alias="DASHBOARD_PORT")
    session_secret: str = Field("", alias="SESSION_SECRET")
    # Default True (fail-safe): the admin session cookie is Secure-only, so it is
    # never sent over plaintext HTTP. Set false ONLY for local dev without TLS.
    dashboard_cookie_secure: bool = Field(True, alias="DASHBOARD_COOKIE_SECURE")
    # Safety: the dashboard refuses to boot if a usable ENCRYPTION_KEY is present in
    # its process (it must never be able to decrypt wallet keys). The test harness —
    # which shares one process/key with the bot/webapp suites — sets this to opt out.
    dashboard_allow_encryption_key: bool = Field(False, alias="DASHBOARD_ALLOW_ENCRYPTION_KEY")
    admin_bootstrap_user: str = Field("admin", alias="ADMIN_BOOTSTRAP_USER")
    admin_bootstrap_password_hash: str = Field("", alias="ADMIN_BOOTSTRAP_PASSWORD_HASH")

    # ── Polymarket ────────────────────────────────────────
    clob_url: str = Field("https://clob.polymarket.com", alias="POLYMARKET_CLOB_URL")
    gamma_url: str = Field("https://gamma-api.polymarket.com", alias="POLYMARKET_GAMMA_URL")
    data_url: str = Field("https://data-api.polymarket.com", alias="POLYMARKET_DATA_URL")
    chain_id: int = Field(137, alias="CHAIN_ID")
    polymarket_signup_url: str = Field("https://polymarket.com", alias="POLYMARKET_SIGNUP_URL")

    # ── Mini App (webapp) ─────────────────────────────────
    webapp_host: str = Field("0.0.0.0", alias="WEBAPP_HOST")
    webapp_port: int = Field(8888, alias="WEBAPP_PORT")
    # Public HTTPS base URL of the Mini App (set as the Web App URL in BotFather).
    webapp_base_url: str = Field("", alias="WEBAPP_BASE_URL")
    # initData freshness window (seconds) — reject replays older than this.
    initdata_max_age_seconds: int = Field(3600, alias="INITDATA_MAX_AGE_SECONDS")
    # LOCAL DEV ONLY: when true, requests without valid Telegram initData are
    # authenticated as a fixed test user, so the Mini App works in a plain
    # browser. MUST be false in production.
    webapp_dev_auth: bool = Field(False, alias="WEBAPP_DEV_AUTH")
    webapp_dev_telegram_id: int = Field(999000001, alias="WEBAPP_DEV_TELEGRAM_ID")

    # ── Gemini (category card images) ─────────────────────
    gemini_api_key: str = Field("", alias="GEMINI_API_KEY")
    gemini_image_model: str = Field("gemini-2.5-flash-image", alias="GEMINI_IMAGE_MODEL")
    # Text model — used by the news pipeline (translate + summarize).
    gemini_text_model: str = Field("gemini-2.5-flash", alias="GEMINI_TEXT_MODEL")
    # Ignore system/env proxies for the Gemini call (most deploys have direct
    # egress to Google; a macOS system proxy / VPN otherwise breaks it). Set true
    # only if you MUST route Gemini through a proxy.
    gemini_trust_env: bool = Field(False, alias="GEMINI_TRUST_ENV")
    # Estimated USD cost per generated image — used for budget accounting.
    gemini_image_cost_usd: float = Field(0.04, alias="GEMINI_IMAGE_COST_USD")
    # Estimated USD cost per text (translate/summarize) call. Text is ~20× cheaper
    # than an image; news + images share ONE weekly budget ledger.
    gemini_text_cost_usd: float = Field(0.002, alias="GEMINI_TEXT_COST_USD")
    # Default weekly budget (USD). The live value is editable in the admin
    # dashboard (stored in app_config); this is just the seed/fallback.
    gemini_weekly_budget_usd: float = Field(10.0, alias="GEMINI_WEEKLY_BUDGET_USD")
    # Separate weekly budget (USD) for news TEXT generation, independent of the image
    # budget above. 0 = UNLIMITED — the default, because the primary text provider is
    # Claude via subscription (no metered per-call cost), so it must not be throttled
    # by the paid-image dollar budget. Set >0 only to cap paid (Gemini) text usage.
    news_text_weekly_budget_usd: float = Field(0.0, alias="NEWS_TEXT_WEEKLY_BUDGET_USD")
    # Directory where generated category card images are cached (served at /cards).
    cards_dir: str = Field("data/cards", alias="CARDS_DIR")

    # ── Claude (news text via the Claude Agent SDK — NOT the Anthropic API) ──
    # Provider for news translate/summarize: "claude" (Claude Agent SDK, uses the
    # local Claude CLI + subscription auth — reaches Anthropic, so it works even
    # when the VPN blocks Gemini/Google) or "gemini" (legacy REST). Image
    # generation always stays on Gemini (Claude has no image model).
    news_text_provider: str = Field("claude", alias="NEWS_TEXT_PROVIDER")
    # Absolute path to the `claude` CLI the SDK drives. Empty → auto-detect
    # (CLAUDE_CODE_EXECPATH, else `claude` on PATH). Set this for a detached bot
    # launched outside Claude Code (e.g. the VS Code extension's bundled binary,
    # or a standalone `claude` install).
    claude_cli_path: str = Field("", alias="CLAUDE_CLI_PATH")
    # Optional model override for Claude text calls (empty → CLI default).
    claude_text_model: str = Field("", alias="CLAUDE_TEXT_MODEL")
    # Hard ceiling (seconds) on a single Claude CLI query. A hung subprocess would
    # otherwise wedge the whole news render pipeline (max_instances=1) indefinitely;
    # on timeout the item degrades to source-language passthrough. Above the SDK's
    # 60s init handshake so a normal slow call still completes.
    claude_text_timeout_seconds: float = Field(120.0, alias="CLAUDE_TEXT_TIMEOUT_SECONDS")

    # ── News pipeline ─────────────────────────────────────
    # Master switch — when false, the crawl/render/publish jobs are NOT registered.
    news_pipeline_enabled: bool = Field(False, alias="NEWS_PIPELINE_ENABLED")
    # Job cadences (seconds) on the bot JobQueue.
    news_crawl_interval_seconds: int = Field(900, alias="NEWS_CRAWL_INTERVAL_SECONDS")
    news_render_interval_seconds: int = Field(120, alias="NEWS_RENDER_INTERVAL_SECONDS")
    news_publish_interval_seconds: int = Field(60, alias="NEWS_PUBLISH_INTERVAL_SECONDS")
    # Max articles pulled per source per crawl tick.
    news_crawl_per_source_limit: int = Field(20, alias="NEWS_CRAWL_PER_SOURCE_LIMIT")
    # Ignore system/env proxies when crawling news sources (same VPN/proxy caveat
    # as Gemini/Telegram). Crawl reaches arbitrary admin-supplied hosts.
    news_crawl_trust_env: bool = Field(False, alias="NEWS_CRAWL_TRUST_ENV")
    # Per-request crawl timeout (seconds) and max body size (bytes, SSRF/DoS guard).
    news_crawl_timeout_seconds: float = Field(15.0, alias="NEWS_CRAWL_TIMEOUT_SECONDS")
    news_crawl_max_bytes: int = Field(5_000_000, alias="NEWS_CRAWL_MAX_BYTES")
    # Language the public news CHANNEL posts in (per-user DMs use each user's lang).
    news_channel_lang: str = Field("en", alias="NEWS_CHANNEL_LANG")
    # Max per-user sends per delivery tick (Telegram rate-limit discipline).
    news_per_tick_cap: int = Field(25, alias="NEWS_PER_TICK_CAP")
    # Max items bundled into a single real-time push.
    news_realtime_max: int = Field(3, alias="NEWS_REALTIME_MAX")
    # Optional logo composited onto rendered news cards (relative to repo root).
    news_logo_path: str = Field("", alias="NEWS_LOGO_PATH")
    # "Bet on this" channel CTA: max upward price tolerance for a news-originated
    # market BUY (placed as a FOK limit at entry*(1+slippage), capped at 0.99) so a
    # tap from a public channel can't fill at an arbitrarily worse price.
    news_bet_slippage: float = Field(0.05, alias="NEWS_BET_SLIPPAGE")
    # Fixed stake (USD) for a one-tap news-channel bet. The card shows this stake's
    # potential payout per outcome (stake ÷ slippage-capped price), and tapping a CTA
    # places exactly this amount (no amount picker on the news path).
    news_bet_amount_usd: float = Field(5.0, alias="NEWS_BET_AMOUNT_USD")
    # How long a pending bet intent (stored when a non-connected user taps a bet
    # CTA) stays resumable after onboarding before the cleanup tick expires it.
    news_intent_ttl_hours: int = Field(24, alias="NEWS_INTENT_TTL_HOURS")

    # ── Behaviour ─────────────────────────────────────────
    default_language: str = Field("en", alias="DEFAULT_LANGUAGE")
    log_level: str = Field("INFO", alias="LOG_LEVEL")
    # Optional rotating log file. Empty → log to stderr only (unchanged). When set,
    # logs ALSO go to this file with size-based rotation (so a long-running bot
    # can't grow an unbounded bot.log).
    log_file: str = Field("", alias="LOG_FILE")
    log_max_bytes: int = Field(10_000_000, alias="LOG_MAX_BYTES")
    log_backup_count: int = Field(5, alias="LOG_BACKUP_COUNT")
    # Latency: TTL (seconds) for cached Gamma market/trending/category reads — cuts
    # redundant egress on hot paths (discover funnel, bet taps). 0 disables.
    markets_cache_ttl_seconds: float = Field(30.0, alias="MARKETS_CACHE_TTL_SECONDS")
    # Min seconds between per-user middleware DB syncs (last_seen + status refresh);
    # between syncs the cached db_user_id/lang/status are reused, so most updates do
    # ZERO DB writes. Coarsens ban-enforcement + last_seen to this granularity.
    middleware_sync_seconds: float = Field(60.0, alias="MIDDLEWARE_SYNC_SECONDS")

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
        if url.startswith("sqlite://") and "+aiosqlite" not in url:
            return url.replace("sqlite://", "sqlite+aiosqlite://", 1)
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
