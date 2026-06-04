"""Background jobs run on the PTB JobQueue.

For the MVP this hosts the broadcast consumer: the dashboard enqueues BROADCAST
Command rows; this job delivers them to users and marks them done/error.
"""

from __future__ import annotations

import asyncio
import logging

from telegram.error import Forbidden, TelegramError
from telegram.ext import Application, ContextTypes

from core.i18n import t
from db.engine import async_session_scope
from db.models import User
from db.repositories import bets as bets_repo
from db.repositories import commands as commands_repo
from db.repositories import rewards as rewards_repo
from db.repositories import stats as stats_repo
from polymarket import markets

logger = logging.getLogger(__name__)

BROADCAST_INTERVAL_SECONDS = 20
SETTLEMENT_INTERVAL_SECONDS = 180


async def broadcast_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Deliver pending BROADCAST commands (a few per tick to respect rate limits)."""
    # 1) Snapshot the batch (resolve recipients) in a short scope, marking malformed
    #    rows as errors immediately — so we never hold a DB connection across the
    #    Telegram sends (which can stall on a slow/blocked endpoint).
    async with async_session_scope() as session:
        cmds = await commands_repo.pending(session, action="BROADCAST", limit=25)
        jobs: list[tuple[int, int, str]] = []  # (cmd_id, telegram_id, message)
        for cmd in cmds:
            message = (cmd.payload or {}).get("message", "")
            telegram_id = await commands_repo.telegram_id_for(session, cmd.user_id)
            if not message or telegram_id is None:
                await commands_repo.mark(session, cmd.id, "error")
                continue
            jobs.append((cmd.id, telegram_id, message))

    # 2) Send OUTSIDE any transaction; collect each row's outcome.
    results: list[tuple[int, str]] = []
    for cmd_id, telegram_id, message in jobs:
        try:
            await context.bot.send_message(chat_id=telegram_id, text=message)
            results.append((cmd_id, "done"))
        except Forbidden:  # user blocked the bot — not retryable
            results.append((cmd_id, "error"))
        except TelegramError as exc:
            logger.warning("broadcast send failed for cmd %s: %s", cmd_id, type(exc).__name__)
            results.append((cmd_id, "error"))

    # 3) Persist outcomes in a short scope.
    if results:
        async with async_session_scope() as session:
            for cmd_id, status in results:
                await commands_repo.mark(session, cmd_id, status)


def _settle_message(bet, vals: dict, lang: str) -> str:
    q = (bet.question or "")[:60]
    outcome = bet.outcome
    if vals["status"] == "WON":
        return t("bot.settle.won", lang, outcome=outcome, q=q,
                 payout=f"{vals['payout']:,.2f}", pnl=f"{vals['pnl']:,.2f}")
    if vals["status"] == "LOST":
        return t("bot.settle.lost", lang, outcome=outcome, q=q, amount=f"{float(bet.amount_usd):,.2f}")
    return t("bot.settle.void", lang, q=q, amount=f"{float(bet.amount_usd):,.2f}")


async def settlement_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Resolve open bets whose markets have settled; book P&L + accuracy and
    queue win/loss notifications. Idempotent: only OPEN bets are processed."""
    async with async_session_scope() as session:
        market_ids = await bets_repo.open_market_ids(session)
    if not market_ids:
        return

    # Resolve each distinct market once (public Polymarket data, blocking).
    resolutions: dict[str, dict] = {}
    for mid in market_ids[:100]:
        resolutions[mid] = await asyncio.to_thread(markets.market_resolution, mid)

    pending_notifs: list[tuple[int, str]] = []
    async with async_session_scope() as session:
        for bet in await bets_repo.open_bets(session):
            res = resolutions.get(bet.market_id)
            if not res or not res.get("resolved"):
                continue
            # Per-bet savepoint: a single malformed bet rolls back only its own
            # writes instead of poisoning the whole batch (and re-poisoning every
            # future run). The bet stays OPEN and is retried next tick. Capture
            # ids up front — a savepoint rollback expires ORM attributes, so we
            # must not lazy-load them from the except handler.
            bet_id, bet_market = bet.id, bet.market_id
            try:
                async with session.begin_nested():
                    vals = bets_repo.settle_bet_values(
                        bet, winning_token=res["winning_token"], void=res["void"])
                    bets_repo.apply_settlement(bet, vals)
                    await stats_repo.record_settlement(session, bet.user_id, status=vals["status"],
                                                       pnl=vals["pnl"], brier=vals["brier"])
                    if vals["status"] == "WON":
                        await rewards_repo.reward_for_win(session, bet.user_id)
                    user = await session.get(User, bet.user_id)
                    notif = (user.telegram_id, _settle_message(bet, vals, user.language)) if user else None
            except Exception:  # noqa: BLE001 — isolate one bad bet, keep settling the rest
                logger.exception("settlement failed for bet %s (market %s); left OPEN",
                                 bet_id, bet_market)
                continue
            if notif:
                pending_notifs.append(notif)
        # session commits all settlements + stats atomically (savepoints already
        # flushed each bet's writes); notifications are sent only after commit.

    for telegram_id, message in pending_notifs:
        try:
            await context.bot.send_message(chat_id=telegram_id, text=message, parse_mode="Markdown")
        except (Forbidden, TelegramError) as exc:
            logger.info("settlement notify skipped for %s: %s", telegram_id, type(exc).__name__)
    if pending_notifs:
        logger.info("Settled bets; sent %d notifications", len(pending_notifs))


def register_jobs(application: Application) -> None:
    jq = application.job_queue
    if jq is None:
        logger.warning("JobQueue unavailable — broadcast/settlement disabled.")
        return
    jq.run_repeating(broadcast_job, interval=BROADCAST_INTERVAL_SECONDS, first=10, name="broadcast")
    jq.run_repeating(settlement_job, interval=SETTLEMENT_INTERVAL_SECONDS, first=30, name="settlement")
