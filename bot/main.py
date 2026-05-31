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

from core.config import settings
from core.logging import setup_logging
from db.engine import async_session_factory
from db.repositories.accounts import DbCredentialStore
from polymarket.account_manager import AccountManager

from bot import jobs, middleware
from bot.handlers import confirm, connect, discover, inquiry, positions_ui, start, trading

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
    BotCommand("trending", "🔥 Trending markets"),
    BotCommand("categories", "🗂 Trending categories"),
    BotCommand("manage", "Manage positions (sell/close)"),
    BotCommand("buy", "Limit buy: /buy <token> <price> <size>"),
    BotCommand("sell", "Limit sell: /sell <token> <price> <size>"),
    BotCommand("marketbuy", "Market buy: /marketbuy <token> <usd>"),
    BotCommand("marketsell", "Market sell: /marketsell <token> <shares>"),
    BotCommand("cancel", "Cancel an order: /cancel <order_id>"),
    BotCommand("cancelall", "Cancel all open orders"),
    BotCommand("search", "Search markets"),
    BotCommand("price", "Token price"),
    BotCommand("language", "Change language"),
    BotCommand("help", "Help"),
]


async def _post_init(app: Application) -> None:
    try:
        await app.bot.set_my_commands(COMMANDS)
    except Exception as exc:  # non-fatal
        logger.warning("set_my_commands failed: %s", type(exc).__name__)


async def _post_shutdown(app: Application) -> None:
    mgr: AccountManager | None = app.bot_data.get("account_manager")
    if mgr:
        mgr.clear()


def build_application() -> Application:
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set.")

    app = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    # Per-user client factory backed by the encrypted DB credential store.
    store = DbCredentialStore(async_session_factory())
    app.bot_data["account_manager"] = AccountManager(store)

    # Middleware runs first on every update (group -1).
    app.add_handler(TypeHandler(Update, middleware.preprocess), group=-1)

    # Each module registers its own handlers.
    start.register(app)
    connect.register(app)
    discover.register(app)
    inquiry.register(app)
    trading.register(app)
    positions_ui.register(app)
    confirm.register(app)  # ^ord_ok: / ^ord_no: confirmation callbacks

    # Background jobs (broadcast delivery, …)
    jobs.register_jobs(app)

    return app


def main() -> None:
    setup_logging()
    logger.info("Starting Polymarket trading bot…")
    app = build_application()
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
