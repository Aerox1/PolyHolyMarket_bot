"""Discovery → buy funnel: /trending and /categories.

A tappable funnel that replaces the old copy-paste-the-id flow:
  list of markets (buttons) → market panel → Buy YES/NO → preset amount → confirm.

Discovery reads public Polymarket data (no account); placing the order hands off
to confirm.py (which resolves the trading client + audits). The market the user
is acting on is stashed in user_data and referenced by a (generation, index) pair
in callback_data — the generation guard guarantees a Buy can never resolve against
a stale list (which would mean betting on the wrong market).
"""

from __future__ import annotations

import asyncio
import logging

from telegram import InlineKeyboardButton, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from bot.handlers import common, confirm, inquiry
from core.config import settings
from polymarket import markets

logger = logging.getLogger(__name__)

_MKTS = "disc_markets"   # stashed normalized markets for the current list
_CATS = "disc_cats"      # stashed categories
_GEN = "disc_gen"        # monotonic list generation
_NEWS_BET = "disc_news_bet"  # {gen, item_id, pending_intent_id} for the news-bet funnel
_AMOUNTS = (5, 10, 25, 50)  # market-buy presets (USD)


def _pct(p) -> str:
    try:
        return f"{round(float(p) * 100)}%"
    except (TypeError, ValueError):
        return "—"


def _vol(v) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "$0"
    if v >= 1_000_000:
        return f"${v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"${v / 1_000:.0f}K"
    return f"${v:.0f}"


def _truncate(s: str, n: int = 38) -> str:
    s = (s or "").strip()
    if len(s) <= n:
        return s
    return (s[:n].rsplit(" ", 1)[0] or s[:n]) + "…"


def _new_gen(context: ContextTypes.DEFAULT_TYPE) -> int:
    g = int(context.user_data.get(_GEN, 0)) + 1
    context.user_data[_GEN] = g
    return g


# ── market list (shared by /trending and a tapped category) ───────────────────

async def _show_markets(update: Update, context: ContextTypes.DEFAULT_TYPE, mkts: list[dict], *,
                        header_key: str) -> None:
    gen = _new_gen(context)
    common.stash(context, _MKTS, mkts)
    header = common.tr(context, header_key).replace("*", "").replace("`", "")
    rows = []
    for i, m in enumerate(mkts):
        label = f"🔥 {_pct(m.get('yes_price'))} · {_truncate(m.get('question') or '?')}"
        rows.append([InlineKeyboardButton(label, callback_data=f"mkt:{gen}:{i}")])
    rows.append([
        InlineKeyboardButton(common.tr(context, "bot.discover.cats_btn"), callback_data="dcats"),
        InlineKeyboardButton(common.tr(context, "bot.tile.refresh"), callback_data="dtrending"),
    ])
    await common.screen(update, context, text=f"<b>{common.esc(header)}</b>",
                        reply_markup=common.with_nav(context, rows))


async def trending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await common.typing(update, context)
    try:
        mkts = await asyncio.to_thread(markets.trending_markets, 12)
    except Exception as exc:  # noqa: BLE001
        logger.warning("trending failed: %s", type(exc).__name__)
        await common.reply(update, context, "bot.error.generic", reply_markup=common.with_nav(context))
        return
    if not mkts:
        await common.reply(update, context, "bot.discover.none", reply_markup=common.with_nav(context))
        return
    await _show_markets(update, context, mkts, header_key="bot.discover.trending_header")


async def categories(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await common.typing(update, context)
    try:
        cats = await asyncio.to_thread(markets.top_categories, 15)
    except Exception as exc:  # noqa: BLE001
        logger.warning("categories failed: %s", type(exc).__name__)
        await common.reply(update, context, "bot.error.generic", reply_markup=common.with_nav(context))
        return
    if not cats:
        await common.reply(update, context, "bot.discover.none", reply_markup=common.with_nav(context))
        return
    common.stash(context, _CATS, cats)
    header = common.tr(context, "bot.discover.categories_header").replace("*", "").replace("`", "")
    rows = [[InlineKeyboardButton(f"{_truncate(c.get('title') or '?', 28)} · {_vol(c.get('volume'))}",
                                  callback_data=f"cat:{i}")] for i, c in enumerate(cats)]
    rows.append([InlineKeyboardButton(common.tr(context, "bot.tile.trending"), callback_data="dtrending")])
    await common.screen(update, context, text=f"<b>{common.esc(header)}</b>",
                        reply_markup=common.with_nav(context, rows))


# ── callbacks ─────────────────────────────────────────────────────────────────

async def on_cat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    idx = (query.data or "").split(":", 1)[1] if ":" in (query.data or "") else ""
    cat = common.from_stash(context, _CATS, idx)
    if not cat:
        await common.reply(update, context, "bot.discover.outdated", reply_markup=common.with_nav(context))
        return
    await common.typing(update, context)
    try:
        mkts = await asyncio.to_thread(markets.category_markets, cat.get("slug"), 20)
    except Exception as exc:  # noqa: BLE001
        logger.warning("category_markets failed: %s", type(exc).__name__)
        await common.reply(update, context, "bot.error.generic", reply_markup=common.with_nav(context))
        return
    if not mkts:
        await common.reply(update, context, "bot.discover.none", reply_markup=common.with_nav(context))
        return
    await _show_markets(update, context, mkts, header_key="bot.discover.trending_header")


def _resolve(context: ContextTypes.DEFAULT_TYPE, gen: str, idx: str) -> dict | None:
    """Resolve a stashed market only if it belongs to the CURRENT list generation."""
    if str(context.user_data.get(_GEN, 0)) != str(gen):
        return None
    m = common.from_stash(context, _MKTS, idx)
    return m if isinstance(m, dict) else None


def _panel_text(context: ContextTypes.DEFAULT_TYPE, m: dict) -> str:
    q = common.esc(m.get("question") or "?")
    return (f"🔥 <b>{q}</b>\n\n"
            f"🟢 YES {_pct(m.get('yes_price'))}    🔴 NO {_pct(m.get('no_price'))}\n"
            f"📊 {_vol(m.get('volume'))}")


def _panel_kb(context: ContextTypes.DEFAULT_TYPE, gen, idx, m: dict):
    rows = [
        [InlineKeyboardButton(f"💵 YES {_pct(m.get('yes_price'))}", callback_data=f"buy:{gen}:{idx}:yes"),
         InlineKeyboardButton(f"💵 NO {_pct(m.get('no_price'))}", callback_data=f"buy:{gen}:{idx}:no")],
        [InlineKeyboardButton("💲 Price", callback_data=f"mprice:{gen}:{idx}"),
         InlineKeyboardButton("📗 Book", callback_data=f"mbook:{gen}:{idx}")],
        [InlineKeyboardButton(common.tr(context, "bot.tile.trending"), callback_data="dtrending")],
    ]
    return common.with_nav(context, rows)


async def _show_panel(update: Update, context: ContextTypes.DEFAULT_TYPE, m: dict, gen, idx) -> None:
    await common.screen(update, context, text=_panel_text(context, m), reply_markup=_panel_kb(context, gen, idx, m))


async def on_market(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, gen, idx = (query.data or "::").split(":")
    m = _resolve(context, gen, idx)
    if m is None:
        await common.reply(update, context, "bot.discover.outdated", reply_markup=common.with_nav(context))
        return
    await _show_panel(update, context, m, gen, idx)


async def on_market_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, gen, idx = (query.data or "::").split(":")
    m = _resolve(context, gen, idx)
    if m is None:
        await common.reply(update, context, "bot.discover.outdated", reply_markup=common.with_nav(context))
        return
    await inquiry.render_price(update, context, str(m.get("yes_token")))


async def on_market_book(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, gen, idx = (query.data or "::").split(":")
    m = _resolve(context, gen, idx)
    if m is None:
        await common.reply(update, context, "bot.discover.outdated", reply_markup=common.with_nav(context))
        return
    await inquiry.render_book(update, context, str(m.get("yes_token")))


# ── /search <query> + /market <condition_id> ──────────────────────────────────

async def search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await common.reply(update, context, "bot.market.search_usage", reply_markup=common.with_nav(context))
        return
    query = " ".join(context.args)
    await common.typing(update, context)
    try:
        mkts = await asyncio.to_thread(markets.search_markets, query, 15)
    except Exception as exc:  # noqa: BLE001
        logger.warning("search failed: %s", type(exc).__name__)
        await common.reply(update, context, "bot.error.generic", reply_markup=common.with_nav(context))
        return
    if not mkts:
        kb = common.with_nav(context, [[InlineKeyboardButton(
            common.tr(context, "bot.tile.trending"), callback_data="dtrending")]])
        await common.reply(update, context, "bot.market.no_results", reply_markup=kb, query=query)
        return
    await _show_markets(update, context, mkts, header_key="bot.discover.trending_header")


async def show_market_by_id(update: Update, context: ContextTypes.DEFAULT_TYPE, market_id: str) -> bool:
    """Fetch a single market by conditionId, stash it as a fresh generation, and
    show its Buy panel. Returns False (after a reply) if not found/unavailable.
    Reused by the /market command and the news-channel CTA deep-link."""
    await common.typing(update, context)
    try:
        m = await asyncio.to_thread(markets.get_market, market_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("market failed: %s", type(exc).__name__)
        await common.reply(update, context, "bot.error.generic", reply_markup=common.with_nav(context))
        return False
    if not m:
        await common.reply(update, context, "bot.market.not_found", reply_markup=common.with_nav(context))
        return False
    gen = _new_gen(context)
    common.stash(context, _MKTS, [m])
    await _show_panel(update, context, m, gen, 0)
    return True


async def market(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await common.reply(update, context, "bot.market.market_usage", reply_markup=common.with_nav(context))
        return
    await show_market_by_id(update, context, context.args[0])


# ── news-channel "Bet on this" funnel ─────────────────────────────────────────

def _bet_amount_screen(context: ContextTypes.DEFAULT_TYPE, m: dict, gen, idx, side: str):
    """Amount picker for a PRE-SELECTED outcome (skips the YES/NO panel). The
    amount + switch buttons reuse the existing buy:/buyamt: callbacks, so the
    generation guard and on_buy_amount handle them unchanged."""
    outcome = "YES" if side == "yes" else "NO"
    other = "no" if side == "yes" else "yes"
    amounts = [InlineKeyboardButton(f"${a}", callback_data=f"buyamt:{gen}:{idx}:{side}:{a}") for a in _AMOUNTS]
    switch = InlineKeyboardButton(common.tr(context, "bot.news.bet_switch"),
                                  callback_data=f"buy:{gen}:{idx}:{other}")
    body = (f"💵 <b>{common.esc(outcome)} — {common.esc(m.get('question') or '?')}</b>\n\n"
            f"🟢 YES {_pct(m.get('yes_price'))}    🔴 NO {_pct(m.get('no_price'))}\n\n"
            f"{common.esc(common.tr(context, 'bot.market.choose_amount'))}")
    return body, common.with_nav(context, [amounts, [switch]])


async def _bet_text(update: Update, context: ContextTypes.DEFAULT_TYPE, key: str, chat_id: int | None) -> None:
    if chat_id is not None:
        await context.bot.send_message(chat_id=chat_id, text=common.tr(context, key),
                                       parse_mode="Markdown", reply_markup=common.with_nav(context))
        return
    await common.reply(update, context, key, reply_markup=common.with_nav(context))


async def show_market_for_bet(
    update: Update, context: ContextTypes.DEFAULT_TYPE, market_id: str, *,
    preselect_outcome: str, news_item_id: int | None = None,
    pending_intent_id: int | None = None, chat_id: int | None = None,
) -> bool:
    """Land a news-channel bet CTA straight on the amount picker for a chosen
    outcome. Resolves the market FRESH (so the token + price are current, never a
    stale snapshot) and distinguishes a closed market from a transient upstream
    blip. ``chat_id`` sends via the bot (used by the connect resume hook, where the
    inbound message was deleted and there's no callback to edit). Returns False
    (after a message) if the market is closed/unavailable."""
    state, m = await asyncio.to_thread(markets.get_market_state, market_id)
    if state == "closed":
        await _bet_text(update, context, "bot.news.bet_closed", chat_id)
        return False
    if state != "open" or not m:
        await _bet_text(update, context, "bot.news.bet_unavailable", chat_id)
        return False
    gen = _new_gen(context)
    common.stash(context, _MKTS, [m])
    context.user_data[_NEWS_BET] = {
        "gen": gen, "item_id": news_item_id, "pending_intent_id": pending_intent_id}
    side = "yes" if str(preselect_outcome).upper().startswith("Y") else "no"
    text, kb = _bet_amount_screen(context, m, gen, 0, side)
    if chat_id is not None:
        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML",
                                       reply_markup=kb, disable_web_page_preview=True)
    else:
        await common.screen(update, context, text=text, reply_markup=kb)
    return True


async def on_buy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, gen, idx, side = (query.data or ":::").split(":")
    m = _resolve(context, gen, idx)
    if m is None:
        await common.reply(update, context, "bot.discover.outdated", reply_markup=common.with_nav(context))
        return
    outcome = "YES" if side == "yes" else "NO"
    amounts = [InlineKeyboardButton(f"${a}", callback_data=f"buyamt:{gen}:{idx}:{side}:{a}") for a in _AMOUNTS]
    rows = [amounts, [InlineKeyboardButton(common.tr(context, "bot.nav.back"), callback_data=f"mkt:{gen}:{idx}")]]
    body = (f"💵 <b>{common.esc(outcome)} — {common.esc(m.get('question') or '?')}</b>\n\n"
            f"{common.esc(common.tr(context, 'bot.market.choose_amount'))}")
    await common.screen(update, context, text=body, reply_markup=common.with_nav(context, rows))


async def on_buy_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, gen, idx, side, amt = (query.data or "::::").split(":")
    m = _resolve(context, gen, idx)
    if m is None:
        await common.reply(update, context, "bot.discover.outdated", reply_markup=common.with_nav(context))
        return
    token = m.get("yes_token") if side == "yes" else m.get("no_token")
    outcome = "YES" if side == "yes" else "NO"
    entry_price = m.get("yes_price") if side == "yes" else m.get("no_price")
    try:
        amount = float(amt)
    except ValueError:
        return
    title = m.get("question") or token
    fields = dict(side="buy", token_id=str(token), amount=amount, title=title, outcome=outcome,
                  market_id=str(m.get("id") or ""), entry_price=entry_price)
    # If this buy belongs to the active news-bet funnel (same generation), tag it
    # so it's recorded as a settleable Bet and placed with a slippage cap (the tap
    # came from a public channel, where the price may have moved since posting).
    nb = context.user_data.get(_NEWS_BET)
    if nb and str(nb.get("gen")) == str(gen):
        # A news-channel bet MUST be slippage-capped — the tap came from a public
        # message where the price may have moved. If we couldn't price the outcome,
        # REFUSE rather than silently fall back to an uncapped market order.
        if not entry_price:
            await common.reply(update, context, "bot.news.bet_unavailable",
                               reply_markup=common.with_nav(context))
            return
        fields.update(source="news", news_item_id=nb.get("item_id"),
                      pending_intent_id=nb.get("pending_intent_id"), neg_risk=bool(m.get("neg_risk")),
                      max_price=min(float(entry_price) * (1 + settings.news_bet_slippage), 0.99))
    intent = confirm.make_intent("market", **fields)
    await confirm.request(update, context, intent, "bot.confirm.buy_market",
                          outcome=outcome, title=common.md_safe(title, 60), amount=f"{amount:g}")


async def on_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = (update.callback_query.data or "") if update.callback_query else ""
    if data == "dcats":
        await categories(update, context)
    else:
        await trending(update, context)


def register(application: Application) -> None:
    application.add_handler(CommandHandler("trending", trending))
    application.add_handler(CommandHandler("categories", categories))
    application.add_handler(CommandHandler("search", search))
    application.add_handler(CommandHandler("market", market))
    application.add_handler(CallbackQueryHandler(on_cat, pattern=r"^cat:\d+$"))
    application.add_handler(CallbackQueryHandler(on_market, pattern=r"^mkt:\d+:\d+$"))
    application.add_handler(CallbackQueryHandler(on_market_price, pattern=r"^mprice:\d+:\d+$"))
    application.add_handler(CallbackQueryHandler(on_market_book, pattern=r"^mbook:\d+:\d+$"))
    application.add_handler(CallbackQueryHandler(on_buy, pattern=r"^buy:\d+:\d+:(yes|no)$"))
    application.add_handler(CallbackQueryHandler(on_buy_amount, pattern=r"^buyamt:\d+:\d+:(yes|no):\d+$"))
    application.add_handler(CallbackQueryHandler(on_refresh, pattern=r"^d(trending|cats)$"))
