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
from polymarket.credentials import WalletMismatchError

logger = logging.getLogger(__name__)

# ── Conversation states ───────────────────────────────────────────────────────
CHOOSE_TYPE, ENTER_ADDRESS, ENTER_FUNDER, ENTER_KEY, RETRY = range(5)

_PROXY_TYPES = (1, 2)  # signature types that require a funder address


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
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(common.tr(context, "bot.connect.type_eoa"), callback_data="ctype:0")],
            [InlineKeyboardButton(common.tr(context, "bot.connect.type_proxy"), callback_data="ctype:1")],
            [InlineKeyboardButton(common.tr(context, "bot.connect.type_safe"), callback_data="ctype:2")],
        ]
    )


# ── entry point ───────────────────────────────────────────────────────────────

async def start_connect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for both /connect and the ^menu:connect$ button."""
    if update.callback_query is not None:
        await update.callback_query.answer()

    _clear_connect(context)
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
    await query.message.reply_text(
        common.tr(context, "bot.connect.enter_address"),
        parse_mode="Markdown",
    )
    return ENTER_ADDRESS


# ── ENTER_ADDRESS ─────────────────────────────────────────────────────────────

async def enter_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    address = (update.message.text or "").strip()
    if not await asyncio.to_thread(auth.is_valid_address, address):
        await common.reply(update, context, "bot.connect.bad_address")
        return ENTER_ADDRESS

    state = context.user_data.setdefault("connect", {})
    state["address"] = address

    if state.get("sig_type") in _PROXY_TYPES:
        await common.reply(update, context, "bot.connect.enter_funder")
        return ENTER_FUNDER

    await common.reply(update, context, "bot.connect.enter_private_key")
    return ENTER_KEY


# ── ENTER_FUNDER ──────────────────────────────────────────────────────────────

async def enter_funder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    funder = (update.message.text or "").strip()
    if not await asyncio.to_thread(auth.is_valid_address, funder):
        await common.reply(update, context, "bot.connect.bad_address")
        return ENTER_FUNDER

    state = context.user_data.setdefault("connect", {})
    state["funder"] = funder

    await common.reply(update, context, "bot.connect.enter_private_key")
    return ENTER_KEY


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
        )
        return ENTER_KEY

    address = state.get("address")
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
            result = await asyncio.to_thread(
                auth.validate_and_derive,
                private_key=private_key,
                wallet_address=address,
                signature_type=sig_type,
                funder_address=funder,
            )
        except WalletMismatchError:
            # No secret in this exception; do not log key material.
            state.pop("key", None)
            await context.bot.send_message(
                chat_id=chat_id,
                text=common.tr(context, "bot.connect.mismatch"),
                parse_mode="Markdown",
            )
            return RETRY
        except Exception as exc:  # noqa: BLE001 - includes auth.ConnectError
            logger.warning("Connect validation failed: %s", type(exc).__name__)
            state.pop("key", None)
            await context.bot.send_message(
                chat_id=chat_id,
                text=common.tr(context, "bot.connect.validation_failed"),
                parse_mode="Markdown",
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
        return ConversationHandler.END
    finally:
        # Zeroize the key in every exit path (success, retry, raise).
        private_key = None  # noqa: F841 - drop local reference
        if isinstance(state, dict):
            state["key"] = None
            state.pop("key", None)


# ── fallbacks / timeout ───────────────────────────────────────────────────────

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _clear_connect(context)
    await common.reply(update, context, "bot.connect.cancelled")
    return ConversationHandler.END


async def on_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
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
    if len(accounts) == 1:
        prompt = common.tr(context, "bot.disconnect.confirm", wallet=accounts[0].wallet_address)
    else:
        prompt = common.tr(context, "bot.disconnect.choose")
    await update.effective_message.reply_text(
        prompt,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def on_disconnect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
            await audit.record_async(
                session,
                AuditEvent.ACCOUNT_DISCONNECTED,
                actor_type="user",
                user_id=user_id,
                account_id=account_id,
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
            CHOOSE_TYPE: [CallbackQueryHandler(choose_type, pattern="^ctype:")],
            ENTER_ADDRESS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, enter_address)
            ],
            ENTER_FUNDER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, enter_funder)
            ],
            ENTER_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, enter_key)],
            RETRY: [MessageHandler(filters.TEXT & ~filters.COMMAND, enter_key)],
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
