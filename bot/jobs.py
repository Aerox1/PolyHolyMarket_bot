"""Background jobs run on the PTB JobQueue.

For the MVP this hosts the broadcast consumer: the dashboard enqueues BROADCAST
Command rows; this job delivers them to users and marks them done/error.
"""

from __future__ import annotations

import logging

from telegram.error import Forbidden, TelegramError
from telegram.ext import Application, ContextTypes

from db.engine import async_session_scope
from db.repositories import commands as commands_repo

logger = logging.getLogger(__name__)

BROADCAST_INTERVAL_SECONDS = 20


async def broadcast_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Deliver pending BROADCAST commands (a few per tick to respect rate limits)."""
    async with async_session_scope() as session:
        cmds = await commands_repo.pending(session, action="BROADCAST", limit=25)
        for cmd in cmds:
            message = (cmd.payload or {}).get("message", "")
            telegram_id = await commands_repo.telegram_id_for(session, cmd.user_id)
            if not message or telegram_id is None:
                await commands_repo.mark(session, cmd.id, "error")
                continue
            try:
                await context.bot.send_message(chat_id=telegram_id, text=message)
                await commands_repo.mark(session, cmd.id, "done")
            except Forbidden:
                # user blocked the bot — not retryable
                await commands_repo.mark(session, cmd.id, "error")
            except TelegramError as exc:
                logger.warning("broadcast send failed for cmd %s: %s", cmd.id, type(exc).__name__)
                await commands_repo.mark(session, cmd.id, "error")


def register_jobs(application: Application) -> None:
    jq = application.job_queue
    if jq is None:
        logger.warning("JobQueue unavailable — broadcast delivery disabled.")
        return
    jq.run_repeating(broadcast_job, interval=BROADCAST_INTERVAL_SECONDS, first=10, name="broadcast")
