"""Telegram bot entrypoint.

Builds the PTB Application, wires the AccountManager + DB credential store into
``bot_data``, installs the preprocessing middleware (group -1), and lets each
handler module register itself via its ``register(application)`` contract.

Run:  python -m bot.main
"""

from __future__ import annotations

import logging

from telegram import BotCommand, Update
from telegram.ext import Application, ApplicationBuilder, ContextTypes, TypeHandler
from telegram.request import HTTPXRequest

from core.config import settings
from core.logging import setup_logging
from db.engine import async_session_factory
from db.repositories.accounts import DbCredentialStore
from polymarket.account_manager import AccountManager

from bot import jobs, middleware
from bot.handlers import confirm, connect, discover, inquiry, news, positions_ui, start, trading
from bot.news import jobs as news_jobs

logger = logging.getLogger(__name__)

COMMANDS = [
    BotCommand("start", "Start / main menu"),
    BotCommand("connect", "Connect a Polymarket wallet"),
    BotCommand("disconnect", "Disconnect a wallet"),
    BotCommand("portfolio", "Portfolio summary"),
    BotCommand("positions", "Open positions"),
    BotCommand("balance", "USDC balance"),
    BotCommand("orders", "Open orders"),
    BotCommand("trades", "Recent trades"),
    BotCommand("activity", "Recent on-chain activity"),
    BotCommand("trending", "🔥 Trending markets"),
    BotCommand("categories", "🗂 Trending categories"),
    BotCommand("rewards", "💰 Rewards & referrals"),
    BotCommand("news", "📰 News preferences"),
    BotCommand("manage", "Manage positions (sell/close)"),
    BotCommand("buy", "Limit buy: /buy <token> <price> <size>"),
    BotCommand("sell", "Limit sell: /sell <token> <price> <size>"),
    BotCommand("marketbuy", "Market buy: /marketbuy <token> <usd>"),
    BotCommand("marketsell", "Market sell: /marketsell <token> <shares>"),
    BotCommand("cancel", "Cancel an order: /cancel <order_id>"),
    BotCommand("cancelall", "Cancel all open orders"),
    BotCommand("search", "Search markets"),
    BotCommand("price", "Token price"),
    BotCommand("market", "Market details: /market <id>"),
    BotCommand("book", "Order book: /book <token>"),
    BotCommand("language", "Change language"),
    BotCommand("help", "Help"),
]


async def _post_init(app: Application) -> None:
    try:
        await app.bot.set_my_commands(COMMANDS)
    except Exception as exc:  # non-fatal
        logger.warning("set_my_commands failed: %s", type(exc).__name__)


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log handler errors cleanly (network blips, etc.) instead of crashing."""
    logger.warning("Handler error: %s", type(context.error).__name__)


async def _post_shutdown(app: Application) -> None:
    mgr: AccountManager | None = app.bot_data.get("account_manager")
    if mgr:
        mgr.clear()


def build_application() -> Application:
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set.")

    # trust_env=False makes httpx ignore a macOS/VPN system proxy that otherwise
    # drops Telegram connections (NetworkError/RemoteProtocolError on send).
    _kw = {"httpx_kwargs": {"trust_env": settings.telegram_trust_env}}
    app = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token)
        .request(HTTPXRequest(**_kw))
        .get_updates_request(HTTPXRequest(**_kw))
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    app.add_error_handler(_on_error)

    # Per-user client factory backed by the encrypted DB credential store.
    store = DbCredentialStore(async_session_factory())
    app.bot_data["account_manager"] = AccountManager(store)

    # Middleware runs first on every update (group -1).
    app.add_handler(TypeHandler(Update, middleware.preprocess), group=-1)

    # Each module registers its own handlers.
    start.register(app)
    connect.register(app)
    discover.register(app)
    news.register(app)
    inquiry.register(app)
    trading.register(app)
    positions_ui.register(app)
    confirm.register(app)  # ^ord_ok: / ^ord_no: confirmation callbacks

    # Background jobs (broadcast delivery, settlement, …)
    jobs.register_jobs(app)
    # News pipeline jobs (crawl + render) — no-op unless NEWS_PIPELINE_ENABLED=1
    news_jobs.register_news_jobs(app)

    return app


def main() -> None:
    setup_logging()
    logger.info("Starting Polymarket trading bot…")
    app = build_application()
    # bootstrap_retries=-1: retry the startup getMe indefinitely instead of aborting
    # on the first network timeout, so a transient egress blip / VPN flap at launch
    # doesn't kill the process — it waits and connects once Telegram is reachable.
    app.run_polling(allowed_updates=Update.ALL_TYPES, bootstrap_retries=-1)


if __name__ == "__main__":
    main()
