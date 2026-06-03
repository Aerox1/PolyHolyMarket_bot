"""/start — a Trojan-style tile dashboard + the Rewards (referral) screen.

The dashboard shows the connected wallet, balance (on Refresh), referral link, and
a grid of action tiles. Tiles route to existing features; ``menu:connect`` is
owned by connect.py (conversation entry), so this module handles everything else.
"""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import quote

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update, WebAppInfo
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from bot.handlers import common, discover, inquiry, news
from core import gemini
from core.config import settings
from core.i18n import LANG_FLAGS, LANG_NAMES, SUPPORTED, t
from db.engine import async_session_scope
from db.repositories import accounts as accounts_repo
from db.repositories import pending_intents as intents_repo
from db.repositories import rewards as rewards_repo
from db.repositories import users as users_repo

logger = logging.getLogger(__name__)


def _parse_usdc(raw) -> float:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return v / 1e6 if v > 1_000_000 else v


def referral_link(context: ContextTypes.DEFAULT_TYPE, code: str | None) -> str:
    uname = getattr(context.bot, "username", None) or "the_bot"
    return f"https://t.me/{uname}?start=r-{code}" if code else f"https://t.me/{uname}"


# ── keyboards ─────────────────────────────────────────────────────────────────

def language_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"{LANG_FLAGS.get(c, '')} {LANG_NAMES.get(c, c)}".strip(), callback_data=f"lang:{c}")
    ] for c in SUPPORTED])


def dashboard_keyboard(context: ContextTypes.DEFAULT_TYPE, *, connected: bool) -> InlineKeyboardMarkup:
    def b(key, data):
        return InlineKeyboardButton(t(f"bot.tile.{key}", common.lang_of(context)), callback_data=f"menu:{data}")

    rows = [
        [b("buy", "buy"), b("sell", "sell")],
        [b("positions", "positions"), b("orders", "orders")],
        [b("trending", "trending"), _play_button(context)],
        [b("rewards", "rewards"), b("news", "news")],
    ]
    if connected:
        rows.append([b("accounts", "accounts"), b("settings", "settings")])
    else:
        rows.append([
            InlineKeyboardButton(t("bot.menu.connect", common.lang_of(context)), callback_data="menu:connect"),
            InlineKeyboardButton(t("bot.menu.create", common.lang_of(context)), callback_data="menu:create"),
        ])
    rows.append([b("help", "help"), b("refresh", "refresh")])
    return InlineKeyboardMarkup(rows)


def _help_keyboard(context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    """Turn /help into a launchpad rather than a dead-end wall of text."""
    def b(key, data):
        return InlineKeyboardButton(t(f"bot.tile.{key}", common.lang_of(context)), callback_data=f"menu:{data}")
    return common.with_nav(context, [
        [b("trending", "trending"), b("positions", "positions")],
        [b("rewards", "rewards")],
    ])


def _create_keyboard(context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    return common.with_nav(context, [
        [InlineKeyboardButton(t("bot.menu.connect", common.lang_of(context)), callback_data="menu:connect")],
        [InlineKeyboardButton("🌐 Polymarket", url=settings.polymarket_signup_url)],
    ])


def _play_button(context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardButton:
    label = t("bot.tile.play", common.lang_of(context))
    if settings.webapp_base_url:
        return InlineKeyboardButton(label, web_app=WebAppInfo(url=settings.webapp_base_url))
    return InlineKeyboardButton(label, callback_data="menu:play")


# ── dashboard render ──────────────────────────────────────────────────────────

async def _dashboard_text(update: Update, context: ContextTypes.DEFAULT_TYPE, *, balance: float | None) -> tuple[str, bool]:
    tg = update.effective_user
    user_id = common.db_user_id(context)
    async with async_session_scope() as s:
        user = await users_repo.get_user(s, tg.id)
        code = await rewards_repo.ensure_referral_code(s, user) if user else None
        acc = await accounts_repo.resolve_account(s, user_id) if user_id else None
        wallet = acc.wallet_address if acc else None
    link = referral_link(context, code)
    if wallet:
        bal = f"${balance:,.2f} USDC" if balance is not None else t("bot.dash.balance_hint", common.lang_of(context))
        text = t("bot.dash.connected", common.lang_of(context),
                 name=tg.first_name or "", wallet=wallet, balance=bal, link=link)
    else:
        text = t("bot.dash.welcome", common.lang_of(context), name=tg.first_name or "", link=link)
    return text, bool(wallet)


async def show_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE, *, balance: float | None = None, edit: bool = False) -> None:
    text, connected = await _dashboard_text(update, context, balance=balance)
    kb = dashboard_keyboard(context, connected=connected)
    banner = gemini.welcome_image_file()  # admin-managed Gemini hero image (or None)

    if edit and update.callback_query is not None:
        msg = update.callback_query.message
        # If the original /start was sent as a photo (banner present), edit its
        # caption; otherwise edit the text. Telegram won't convert between the two.
        # ``message`` may be an InaccessibleMessage (stale callback >48h) with no
        # .photo attr — isinstance guard falls through to edit via the query.
        try:
            if isinstance(msg, Message) and msg.photo:
                await msg.edit_caption(caption=text, reply_markup=kb, parse_mode="Markdown")
            else:
                await update.callback_query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown",
                                                              disable_web_page_preview=True)
        except BadRequest as exc:
            # "message is not modified" → a no-op Refresh on identical content; ignore.
            # Any other edit failure (stale/uneditable) → send a fresh dashboard
            # instead of bubbling up to on_menu's generic-error reply.
            if "not modified" in str(exc).lower():
                return
            if update.effective_message is not None:
                await update.effective_message.reply_text(text, reply_markup=kb, parse_mode="Markdown",
                                                          disable_web_page_preview=True)
        return

    if update.effective_message is None:
        return
    if banner is not None:
        try:
            await update.effective_message.reply_photo(
                photo=banner, caption=text[:1024], reply_markup=kb, parse_mode="Markdown")
            return
        except Exception as exc:  # noqa: BLE001 — fall back to text if the upload fails
            logger.info("welcome banner send failed: %s", type(exc).__name__)
    await update.effective_message.reply_text(text, reply_markup=kb, parse_mode="Markdown",
                                               disable_web_page_preview=True)


# ── handlers ──────────────────────────────────────────────────────────────────

async def _open_news_item(update: Update, context: ContextTypes.DEFAULT_TYPE, item_id: int) -> None:
    """News-channel CTA target: route to the item's cached market (tap-to-trade)
    if one was resolved, else fall back to the dashboard."""
    from db.models import NewsItem
    async with async_session_scope() as s:
        item = await s.get(NewsItem, item_id)
        market_id = item.cta_market_id if item else None
    if market_id:
        # show_market_by_id already replies (panel, or not-found/error) — don't
        # also dump the dashboard on top of a not-found reply.
        await discover.show_market_by_id(update, context, market_id)
        return
    await show_dashboard(update, context)


async def _open_news_bet(update: Update, context: ContextTypes.DEFAULT_TYPE,
                         item_id: int, outcome: str) -> None:
    """News-channel Bet CTA target (``nb-<id>-<y|n>``). Connected user → jump
    straight to the amount picker for the chosen outcome. New user → persist the
    intended bet, arm resume, and route through the existing connect flow; the bet
    resumes on the amount picker after onboarding (never auto-placed)."""
    from db.models import NewsItem
    user_id = common.db_user_id(context)
    async with async_session_scope() as s:
        item = await s.get(NewsItem, item_id)
        market_id = item.cta_market_id if item else None
        question = item.title_orig if item else None
        acc = await accounts_repo.resolve_account(s, user_id) if user_id else None
    if not market_id:
        # no bettable market resolved for this item → open it normally
        await _open_news_item(update, context, item_id)
        return
    if acc is not None:
        await discover.show_market_for_bet(
            update, context, market_id, preselect_outcome=outcome, news_item_id=item_id)
        return

    # Not connected: stash the intent so it survives onboarding, then send to connect.
    pid = None
    if user_id is not None:
        try:
            async with async_session_scope() as s:
                intent = await intents_repo.upsert_intent(
                    s, user_id=user_id, news_item_id=item_id, market_id=market_id,
                    outcome=outcome, question=question, ttl_hours=settings.news_intent_ttl_hours)
                pid = intent.id
        except Exception as exc:  # noqa: BLE001 — never block onboarding on the intent write
            logger.warning("pending intent upsert failed: %s", type(exc).__name__)
    # Arm resume ONLY for this bet-prompt path (so a later unrelated /connect won't
    # yank the user into a stale bet picker).
    context.user_data["news_bet_armed"] = pid or True
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(
        common.tr(context, "bot.news.bet_connect_btn"), callback_data="menu:connect")]])
    await common.reply(update, context, "bot.news.bet_connect_prompt", reply_markup=kb,
                       outcome=outcome, headline=common.md_safe(question or "", 120))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start [r-<code> | n-<item_id> | nb-<item_id>-<y|n>] — attribute a referral,
    open a news item's market, pre-select a bet outcome, else show the dashboard."""
    try:
        ref_code = None
        news_item_id = None
        news_bet = None  # (item_id, "YES"|"NO")
        for a in (context.args or []):
            al = a.lower()
            if al.startswith("nb-"):
                parts = a[3:].split("-")
                if len(parts) == 2 and parts[0].isdigit() and parts[1].lower() in ("y", "n"):
                    news_bet = (int(parts[0]), "YES" if parts[1].lower() == "y" else "NO")
            elif al.startswith("r-"):
                ref_code = a[2:]
            elif al.startswith("n-") and a[2:].isdigit():
                news_item_id = int(a[2:])
        if ref_code:
            tg = update.effective_user
            async with async_session_scope() as s:
                user = await users_repo.get_user(s, tg.id)
                if user:
                    await rewards_repo.attribute_referral(s, user, ref_code)
        if news_bet is not None:
            await _open_news_bet(update, context, news_bet[0], news_bet[1])
            return
        if news_item_id is not None:
            await _open_news_item(update, context, news_item_id)
            return
        await show_dashboard(update, context)
    except Exception as exc:  # noqa: BLE001
        logger.warning("start failed: %s", type(exc).__name__)
        await common.reply(update, context, "bot.error.generic")


async def on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    action = (query.data or "").split(":", 1)[1] if ":" in (query.data or "") else ""
    try:
        await query.answer()
        if action == "refresh":
            balance = None
            user_id = common.db_user_id(context)
            try:
                pm = await common.manager(context).get_trading_client(user_id)
                bal = await asyncio.to_thread(pm.get_balance)
                balance = _parse_usdc(bal.get("balance")) if isinstance(bal, dict) else None
            except Exception as exc:  # noqa: BLE001 — no account / not signable / network
                logger.info("refresh balance unavailable: %s", type(exc).__name__)
            await show_dashboard(update, context, balance=balance, edit=True)
        elif action == "home":
            await show_dashboard(update, context, edit=True)
        elif action == "create":
            await query.message.reply_text(
                common.tr(context, "bot.create.instructions", url=settings.polymarket_signup_url),
                parse_mode="Markdown", reply_markup=_create_keyboard(context), disable_web_page_preview=True)
        elif action == "help":
            await query.message.reply_text(common.tr(context, "bot.start.help_text"),
                                           parse_mode="Markdown", reply_markup=_help_keyboard(context),
                                           disable_web_page_preview=True)
        elif action == "positions":
            await inquiry.positions(update, context)
        elif action == "orders":
            await inquiry.orders(update, context)
        elif action == "trending":
            await discover.trending(update, context)
        elif action == "rewards":
            await rewards_screen(update, context)
        elif action == "news":
            await news.show_settings_screen(update, context)
        elif action == "accounts":
            await _accounts(update, context)
        elif action == "settings":
            await _settings(update, context)
        elif action in ("buy", "sell"):
            await query.message.reply_text(common.tr(context, "bot.dash.trade_hint"),
                                           parse_mode="Markdown", reply_markup=common.with_nav(context))
        elif action == "watchlist":
            await query.message.reply_text(common.tr(context, "bot.dash.coming_soon"),
                                           parse_mode="Markdown", reply_markup=common.with_nav(context))
        elif action == "play":
            await query.message.reply_text(common.tr(context, "bot.dash.play_hint"),
                                           parse_mode="Markdown", reply_markup=common.with_nav(context))
    except Exception as exc:  # noqa: BLE001
        logger.warning("on_menu(%s) failed: %s", action, type(exc).__name__)
        await common.reply(update, context, "bot.error.generic")


async def on_language_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    try:
        await query.answer()
        code = (query.data or "").split(":", 1)[1] if ":" in (query.data or "") else ""
        if code not in SUPPORTED:
            return
        async with async_session_scope() as s:
            await users_repo.set_language(s, update.effective_user.id, code)
        context.user_data["lang"] = code
        await query.message.reply_text(common.tr(context, "bot.start.language_set"), parse_mode="Markdown")
        await show_dashboard(update, context)
    except Exception as exc:  # noqa: BLE001
        logger.warning("on_language_choice failed: %s", type(exc).__name__)


async def rewards_screen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg = update.effective_user
    async with async_session_scope() as s:
        user = await users_repo.get_user(s, tg.id)
        code = await rewards_repo.ensure_referral_code(s, user) if user else None
        stats = await rewards_repo.referral_stats(s, user) if user else {}
    layers = " · ".join(f"L{i+1} {int(r*100)}%" for i, r in enumerate(rewards_repo.REFERRAL_LAYER_RATES))
    text = common.tr(
        context, "bot.rewards.screen",
        balance=stats.get("balance", 0), direct=stats.get("direct", 0),
        indirect=stats.get("indirect", 0), unlocked=stats.get("unlocked", 0),
        referral_points=stats.get("referral_points", 0), layers=layers,
        link=referral_link(context, code), signup=rewards_repo.SIGNUP_BONUS,
        unlock_bets=rewards_repo.REFERRAL_UNLOCK_BETS,
    )
    share_url = "https://t.me/share/url?url=" + quote(referral_link(context, code), safe="")
    kb = common.with_nav(context, [[InlineKeyboardButton(common.tr(context, "bot.rewards.share"), url=share_url)]])
    target = update.callback_query.message if update.callback_query else update.effective_message
    await target.reply_text(text, parse_mode="Markdown", disable_web_page_preview=True, reply_markup=kb)


async def _accounts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = common.db_user_id(context)
    accts = await common.manager(context).list_accounts(user_id) if user_id else []
    msg = update.callback_query.message if update.callback_query else update.effective_message
    if not accts:
        await msg.reply_text(common.tr(context, "bot.account.none"), parse_mode="Markdown")
        return
    lines = [common.tr(context, "bot.account.list_header")]
    rows = []
    for a in accts:
        lines.append(f"`{a.wallet_address}` ({a.mode})")
        rows.append([InlineKeyboardButton(
            common.tr(context, "bot.disconnect.button", label=a.label, wallet=common.short(a.wallet_address, 6, 4)),
            callback_data=f"disc:{a.account_id}")])
    await msg.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=common.with_nav(context, rows))


async def _settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.callback_query.message if update.callback_query else update.effective_message
    await msg.reply_text(common.tr(context, "bot.settings.language_prompt"),
                         reply_markup=language_keyboard(), parse_mode="Markdown")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await common.reply(update, context, "bot.start.help_text",
                       reply_markup=_help_keyboard(context), disable_preview=True)


async def language_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _settings(update, context)


def register(application: Application) -> None:
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("language", language_command))
    application.add_handler(CommandHandler("rewards", rewards_screen))
    application.add_handler(CallbackQueryHandler(on_language_choice, pattern="^lang:"))
    application.add_handler(CallbackQueryHandler(
        on_menu,
        pattern="^menu:(home|create|help|positions|orders|trending|rewards|news|watchlist|play|settings|accounts|refresh|buy|sell)$"))
