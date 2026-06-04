"""Connect Account ConversationHandler + /disconnect.

SECURITY-CRITICAL. The plaintext private key only ever lives in:
  * the inbound Telegram message (deleted as the FIRST action in ``enter_key``),
  * a transient local variable inside ``enter_key`` (zeroized in ``finally``),
  * ``context.user_data['connect']['key']`` for the brief window between
    normalization and the call into ``auth.validate_and_derive`` (popped right
    after, and in every exit path via ``_clear_connect``).
The key is NEVER logged, echoed, or written to the audit ``detail`` payload.
Persistence/encryption happens exclusively inside ``accounts_repo.upsert_account``.
"""

from __future__ import annotations

import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot.handlers import common
from core import audit
from core.audit import AuditEvent
from db.engine import async_session_scope
from db.repositories import accounts as accounts_repo
from db.repositories import users as users_repo
from polymarket import auth

logger = logging.getLogger(__name__)

# ── Conversation states ───────────────────────────────────────────────────────
# No ENTER_ADDRESS: the signer address is derived from the key, not typed.
CHOOSE_TYPE, ENTER_FUNDER, ENTER_KEY, RETRY = range(4)

_PROXY_TYPES = (1, 2)  # signature types that require a funder address


def _nav_kb(context: ContextTypes.DEFAULT_TYPE, *, back_to: str | None = None) -> InlineKeyboardMarkup:
    """[⬅️ Back][✖ Cancel] for a connect step so the user is never stuck typing
    /cancel. ``back_to`` is 'type' or 'funder' (the step to return to)."""
    row = []
    if back_to:
        row.append(InlineKeyboardButton(common.tr(context, "bot.connect.nav_back"),
                                        callback_data=f"conn:to_{back_to}"))
    row.append(InlineKeyboardButton(common.tr(context, "bot.connect.nav_cancel"), callback_data="conn:cancel"))
    return InlineKeyboardMarkup([row])


# ── helpers ───────────────────────────────────────────────────────────────────

def _clear_connect(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Drop the entire transient connect state, including any plaintext key.

    We overwrite the key slot before popping so the string object is no longer
    referenced via the dict, then remove the dict itself.
    """
    state = context.user_data.get("connect")
    if isinstance(state, dict):
        if "key" in state:
            state["key"] = None
        state.pop("key", None)
    context.user_data.pop("connect", None)


def _short(address: str) -> str:
    """Abbreviate an address for inline-button labels (public data only)."""
    if address and len(address) > 12:
        return f"{address[:6]}…{address[-4:]}"
    return address or ""


def _type_keyboard(context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    # Order matches the prompt body: Email/Magic (most common) first, so a typical
    # email user taps it instead of falling into the EOA path by default.
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(common.tr(context, "bot.connect.type_proxy"), callback_data="ctype:1")],
            [InlineKeyboardButton(common.tr(context, "bot.connect.type_eoa"), callback_data="ctype:0")],
            [InlineKeyboardButton(common.tr(context, "bot.connect.type_safe"), callback_data="ctype:2")],
        ]
    )


# ── entry point ───────────────────────────────────────────────────────────────

async def start_connect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for both /connect and the ^menu:connect$ button."""
    if update.callback_query is not None:
        await update.callback_query.answer()

    _clear_connect(context)
    # Disarm any pending custom-amount capture so the key the user is about to paste
    # can never be read by the discover typed-amount handler.
    context.user_data.pop("awaiting_bet", None)
    context.user_data["connect"] = {}

    msg = update.effective_message
    if msg is not None:
        await msg.reply_text(
            common.tr(context, "bot.connect.choose_wallet_type"),
            parse_mode="Markdown",
            reply_markup=_type_keyboard(context),
        )
    return CHOOSE_TYPE


# ── CHOOSE_TYPE ───────────────────────────────────────────────────────────────

async def choose_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    try:
        sig_type = int(query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        sig_type = 0
    if sig_type not in (0, 1, 2):
        sig_type = 0

    context.user_data["connect"] = {"sig_type": sig_type}

    # Proxy/Safe accounts hold funds at a separate address → ask for the funder
    # first. EOA wallets are their own account → go straight to the key.
    if sig_type in _PROXY_TYPES:
        await query.edit_message_text(
            common.tr(context, "bot.connect.enter_funder"),
            parse_mode="Markdown", reply_markup=_nav_kb(context, back_to="type"))
        return ENTER_FUNDER

    await query.edit_message_text(
        common.tr(context, "bot.connect.enter_private_key"),
        parse_mode="Markdown", reply_markup=_nav_kb(context, back_to="type"))
    return ENTER_KEY


# ── ENTER_FUNDER ──────────────────────────────────────────────────────────────

async def enter_funder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    funder = (update.message.text or "").strip()
    if not await asyncio.to_thread(auth.is_valid_address, funder):
        await common.reply(update, context, "bot.connect.bad_address",
                           reply_markup=_nav_kb(context, back_to="type"))
        return ENTER_FUNDER

    state = context.user_data.setdefault("connect", {})
    state["funder"] = funder

    await common.reply(update, context, "bot.connect.enter_private_key",
                       reply_markup=_nav_kb(context, back_to="funder"))
    return ENTER_KEY


# ── Back / Cancel navigation (inline buttons on every step) ───────────────────

async def conn_nav(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    action = (query.data or "").split(":", 1)[1] if ":" in (query.data or "") else ""
    if action == "cancel":
        _clear_connect(context)
        try:
            await query.edit_message_text(common.tr(context, "bot.connect.cancelled"))
        except Exception:  # noqa: BLE001 — message may be uneditable
            await query.message.reply_text(common.tr(context, "bot.connect.cancelled"))
        return ConversationHandler.END
    if action == "to_type":
        context.user_data["connect"] = {}
        await query.edit_message_text(common.tr(context, "bot.connect.choose_wallet_type"),
                                      parse_mode="Markdown", reply_markup=_type_keyboard(context))
        return CHOOSE_TYPE
    if action == "to_funder":
        await query.edit_message_text(common.tr(context, "bot.connect.enter_funder"),
                                      parse_mode="Markdown", reply_markup=_nav_kb(context, back_to="type"))
        return ENTER_FUNDER
    return None  # unknown action → leave the conversation in its current state


# ── ENTER_KEY (and RETRY re-entry) ────────────────────────────────────────────

async def enter_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.message

    # *** FIRST ACTION: delete the message carrying the private key. ***
    deleted = False
    try:
        await message.delete()
        deleted = True
    except Exception as exc:  # noqa: BLE001 - never log the message content
        logger.warning("Could not delete private-key message: %s", type(exc).__name__)
    if not deleted:
        await context.bot.send_message(
            chat_id=message.chat_id,
            text=common.tr(context, "bot.connect.key_delete_failed"),
            parse_mode="Markdown",
        )

    chat_id = message.chat_id
    state = context.user_data.setdefault("connect", {})

    # Normalize WITHOUT echoing/logging the value.
    private_key = auth.normalize_private_key(message.text or "")
    if private_key is None:
        await context.bot.send_message(
            chat_id=chat_id,
            text=common.tr(context, "bot.connect.bad_key"),
            parse_mode="Markdown",
            reply_markup=_nav_kb(context, back_to="funder" if state.get("funder") else "type"),
        )
        return ENTER_KEY

    sig_type = int(state.get("sig_type", 0))
    funder = state.get("funder")

    # Hold the key in the transient state only for the validation window.
    state["key"] = private_key

    await context.bot.send_message(
        chat_id=chat_id,
        text=common.tr(context, "bot.connect.validating"),
        parse_mode="Markdown",
    )

    try:
        try:
            # wallet_address=None → the signer address is derived from the key
            # (no user-typed address, so no mismatch to fail on).
            result = await asyncio.to_thread(
                auth.validate_and_derive,
                private_key=private_key,
                wallet_address=None,
                signature_type=sig_type,
                funder_address=funder,
            )
        except Exception as exc:  # noqa: BLE001 - includes auth.ConnectError
            logger.warning("Connect validation failed: %s", type(exc).__name__)
            state.pop("key", None)
            await context.bot.send_message(
                chat_id=chat_id,
                text=common.tr(context, "bot.connect.validation_failed"),
                parse_mode="Markdown",
                reply_markup=_nav_kb(context, back_to="funder" if state.get("funder") else "type"),
            )
            return RETRY

        # ── success ──
        # The plaintext key is no longer needed in the transient state: the
        # creds we persist live in ``result.creds``. Drop it from user_data
        # immediately so it does not linger during DB writes / Telegram sends.
        state.pop("key", None)

        user_id = common.db_user_id(context)
        if user_id is None:
            await context.bot.send_message(
                chat_id=chat_id,
                text=common.tr(context, "bot.error.no_account"),
                parse_mode="Markdown",
            )
            return ConversationHandler.END
        telegram_id = update.effective_user.id
        try:
            async with async_session_scope() as session:
                acc = await accounts_repo.upsert_account(
                    session,
                    user_id,
                    result.creds,
                    label=common.tr(context, "bot.connect.default_label"),
                )
                await users_repo.set_active_account(session, telegram_id, acc.id)
                await audit.record_async(
                    session,
                    AuditEvent.ACCOUNT_CONNECTED,
                    actor_type="user",
                    user_id=user_id,
                    account_id=acc.id,
                    detail={
                        "wallet": result.creds.wallet_address,
                        "sig_type": result.creds.signature_type,
                    },
                )
            common.manager(context).invalidate(user_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Connect persistence failed: %s", type(exc).__name__)
            await context.bot.send_message(
                chat_id=chat_id,
                text=common.tr(context, "bot.error.generic"),
                parse_mode="Markdown",
            )
            return RETRY

        await context.bot.send_message(
            chat_id=chat_id,
            text=common.tr(
                context,
                "bot.connect.success",
                wallet=result.creds.wallet_address,
                balance=f"{result.balance_usdc:,.2f}",
            ),
            parse_mode="Markdown",
        )
        # If the user came here from a news-channel "Bet" CTA, resume that bet on
        # the amount picker. Best-effort and AFTER the key is gone — never raises
        # into the success path.
        await _resume_news_bet(update, context, chat_id, user_id)
        return ConversationHandler.END
    finally:
        # Zeroize the key in every exit path (success, retry, raise).
        private_key = None  # noqa: F841 - drop local reference
        if isinstance(state, dict):
            state["key"] = None
            state.pop("key", None)


# ── news-bet resume (after a successful connect) ──────────────────────────────

async def _resume_news_bet(update: Update, context: ContextTypes.DEFAULT_TYPE,
                           chat_id: int, user_id: int | None) -> None:
    """Resume a bet the user intended from a news-channel CTA before connecting.
    Gated on the ``news_bet_armed`` flag (set only on that path) so an unrelated
    /connect never resurfaces a stale bet. Renders the amount picker — NEVER
    auto-places. Best-effort: a failure here must not disturb the connect success.
    """
    if not context.user_data.pop("news_bet_armed", None) or user_id is None:
        return
    try:
        from bot.handlers import discover
        from db.repositories import pending_intents as intents_repo
        intent_id = market_id = outcome = item_id = None
        async with async_session_scope() as session:
            row = await intents_repo.latest_pending(session, user_id)
            if row is not None:
                intent_id, market_id, outcome, item_id = (
                    row.id, row.market_id, row.outcome, row.news_item_id)
        if not market_id:
            return
        ok = await discover.show_market_for_bet(
            update, context, market_id, preselect_outcome=outcome,
            news_item_id=item_id, pending_intent_id=intent_id, chat_id=chat_id)
        # Mark 'resumed' ONLY once the picker actually rendered. If the market was
        # closed/unavailable, leave the intent 'pending' so it isn't orphaned in a
        # dead 'resumed' state — the cleanup job reaps it at TTL, and a fresh tap
        # (now connected) takes the direct path with a live market check.
        if ok and intent_id is not None:
            async with async_session_scope() as session:
                await intents_repo.mark(session, intent_id, "resumed")
    except Exception as exc:  # noqa: BLE001 — resume is a bonus; connect already succeeded
        logger.warning("news bet resume failed: %s", type(exc).__name__)


# ── fallbacks / timeout ───────────────────────────────────────────────────────

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _clear_connect(context)
    await common.reply(update, context, "bot.connect.cancelled")
    return ConversationHandler.END


async def on_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # SECURITY: the timeout can fire while the user is on the key step; if they
    # then paste their key it routes here, so delete the inbound message FIRST
    # (mirror enter_key) before it lingers in the chat.
    if update.message is not None:
        try:
            await update.message.delete()
        except Exception as exc:  # noqa: BLE001 — never log message content
            logger.warning("Could not delete message on connect timeout: %s", type(exc).__name__)
    _clear_connect(context)
    chat = update.effective_chat
    if chat is not None:
        await context.bot.send_message(
            chat_id=chat.id,
            text=common.tr(context, "bot.connect.timeout"),
            parse_mode="Markdown",
        )
    return ConversationHandler.END


# ── /disconnect ───────────────────────────────────────────────────────────────

async def disconnect_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = common.db_user_id(context)
    if user_id is None:
        await common.reply(update, context, "bot.error.no_account")
        return

    try:
        accounts = await common.manager(context).list_accounts(user_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Disconnect list failed: %s", type(exc).__name__)
        await common.reply(update, context, "bot.error.generic")
        return

    if not accounts:
        await common.reply(update, context, "bot.disconnect.none")
        return

    rows = [
        [
            InlineKeyboardButton(
                common.tr(context, "bot.disconnect.button", label=acc.label, wallet=_short(acc.wallet_address)),
                callback_data=f"disc:{acc.account_id}",
            )
        ]
        for acc in accounts
    ]
    await update.effective_message.reply_text(
        common.tr(context, "bot.disconnect.choose"),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def on_disconnect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """First tap (disc:{id}) — ask for confirmation; deletion happens on discok:{id}."""
    query = update.callback_query
    await query.answer()
    user_id = common.db_user_id(context)
    if user_id is None:
        await query.message.reply_text(common.tr(context, "bot.error.no_account"))
        return
    try:
        account_id = int(query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await query.message.reply_text(common.tr(context, "bot.error.generic"))
        return
    try:
        accounts = await common.manager(context).list_accounts(user_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Disconnect list failed: %s", type(exc).__name__)
        await query.message.reply_text(common.tr(context, "bot.error.generic"))
        return
    acc = next((a for a in accounts if a.account_id == account_id), None)
    if acc is None:
        await query.edit_message_text(common.tr(context, "bot.disconnect.none"))
        return
    rows = [[
        InlineKeyboardButton(common.tr(context, "bot.disconnect.yes"), callback_data=f"discok:{account_id}"),
        InlineKeyboardButton(common.tr(context, "bot.confirm.no"), callback_data="discno"),
    ]]
    await query.edit_message_text(
        common.tr(context, "bot.disconnect.confirm", wallet=_short(acc.wallet_address)),
        parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows))


async def on_disconnect_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(common.tr(context, "bot.disconnect.cancelled"))


async def on_disconnect_confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Second tap (discok:{id}) — the actual, irreversible credential deletion."""
    query = update.callback_query
    await query.answer()

    user_id = common.db_user_id(context)
    if user_id is None:
        await query.message.reply_text(common.tr(context, "bot.error.no_account"))
        return

    try:
        account_id = int(query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await query.message.reply_text(common.tr(context, "bot.error.generic"))
        return

    telegram_id = update.effective_user.id
    try:
        async with async_session_scope() as session:
            deleted = await accounts_repo.delete_account(session, user_id, account_id)
            if not deleted:
                await query.message.reply_text(common.tr(context, "bot.disconnect.none"))
                return
            await users_repo.set_active_account(session, telegram_id, None)
            # Record the id in `detail`, NOT the account_id FK column: the account
            # row is being deleted in this same transaction, so an audit row that
            # referenced accounts.id would fail the FK on INSERT (with FKs enforced)
            # and roll back the whole disconnect — leaving the key undeleted.
            await audit.record_async(
                session,
                AuditEvent.ACCOUNT_DISCONNECTED,
                actor_type="user",
                user_id=user_id,
                detail={"account_id": account_id},
            )
        common.manager(context).invalidate(user_id, account_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Disconnect failed: %s", type(exc).__name__)
        await query.message.reply_text(common.tr(context, "bot.error.generic"))
        return

    await query.message.reply_text(
        common.tr(context, "bot.disconnect.done"), parse_mode="Markdown"
    )


# ── registration ──────────────────────────────────────────────────────────────

def register(application: Application) -> None:
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("connect", start_connect),
            CallbackQueryHandler(start_connect, pattern="^menu:connect$"),
        ],
        states={
            CHOOSE_TYPE: [
                CallbackQueryHandler(choose_type, pattern="^ctype:"),
                CallbackQueryHandler(conn_nav, pattern="^conn:"),
            ],
            ENTER_FUNDER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, enter_funder),
                CallbackQueryHandler(conn_nav, pattern="^conn:"),
            ],
            ENTER_KEY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, enter_key),
                CallbackQueryHandler(conn_nav, pattern="^conn:"),
            ],
            RETRY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, enter_key),
                CallbackQueryHandler(conn_nav, pattern="^conn:"),
            ],
            ConversationHandler.TIMEOUT: [
                MessageHandler(filters.ALL, on_timeout),
                CallbackQueryHandler(on_timeout),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        conversation_timeout=300,
        allow_reentry=True,
        name="connect_conversation",
    )
    application.add_handler(conv)
    application.add_handler(CommandHandler("disconnect", disconnect_cmd))
    application.add_handler(CallbackQueryHandler(on_disconnect, pattern="^disc:"))
    application.add_handler(CallbackQueryHandler(on_disconnect_confirmed, pattern="^discok:"))
    application.add_handler(CallbackQueryHandler(on_disconnect_cancel, pattern="^discno$"))
