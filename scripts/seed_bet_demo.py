"""Post ONE demo "Bet on this" item to a channel — end-to-end proof of the
news-bet CTA without enabling the whole crawl/render/publish pipeline.

It picks a real, open, binary Polymarket market (or one you name), creates a
backing ``NewsItem`` row (so the ``nb-<id>-<y|n>`` deep-link resolves), and posts
it to the channel with the two Bet YES / Bet NO buttons. Tapping a button then
exercises the real funnel (fresh token resolution → amount picker → slippage-
capped, force-confirmed buy → settleable Bet).

Prereqs: the bot must be an ADMIN of the target channel, TELEGRAM_BOT_TOKEN set,
and clean egress to Telegram + Gamma (VPN off — see the VPN-egress note).

    .venv/bin/python -m scripts.seed_bet_demo --chat-id -1001234567890
    .venv/bin/python -m scripts.seed_bet_demo --chat-id -100... --market 0x<conditionId>

With no --chat-id it falls back to the configured news_channel_id (app_config).
"""

from __future__ import annotations

import argparse
import asyncio

from telegram import Bot

from bot.news import publisher
from core.config import settings
from db.engine import async_session_scope
from db.repositories import appconfig
from db.repositories import news_items as items_repo
from polymarket import markets


async def _resolve_chat_id(arg: str | None) -> int | None:
    if arg:
        return int(arg)
    async with async_session_scope() as s:
        raw = (await appconfig.get(s, "news_channel_id") or "").strip()
    return int(raw) if raw else None


async def _pick_market(condition_id: str | None) -> dict | None:
    if condition_id:
        return await asyncio.to_thread(markets.get_market, condition_id)
    rows = await asyncio.to_thread(markets.trending_markets, 1)
    return rows[0] if rows else None


async def main() -> int:
    ap = argparse.ArgumentParser(description="Post one demo bet-on-news item to a channel.")
    ap.add_argument("--chat-id", help="Channel id, e.g. -1001234567890 (default: app_config news_channel_id)")
    ap.add_argument("--market", help="Market conditionId (default: top trending open market)")
    args = ap.parse_args()

    if not settings.telegram_bot_token:
        print("✗ TELEGRAM_BOT_TOKEN is not set.")
        return 2

    chat_id = await _resolve_chat_id(args.chat_id)
    if chat_id is None:
        print("✗ No channel id. Pass --chat-id or set news_channel_id in the dashboard (News → Settings).")
        return 2

    market = await _pick_market(args.market)
    if not market or not market.get("id"):
        print("✗ Could not resolve an open binary market (Gamma egress? try --market <conditionId>).")
        return 2

    question = market.get("question") or "Demo market"
    print(f"• Market: {market['id']}  «{question}»  YES={market.get('yes_price')} NO={market.get('no_price')}")

    bot = Bot(settings.telegram_bot_token)
    async with bot:
        me = await bot.get_me()
        if not await publisher.channel_is_admin(bot, chat_id):
            print(f"✗ Bot @{me.username} is not an admin of channel {chat_id}. Add it as admin and retry.")
            return 2

        # Backing row so /start nb-<id>-<y|n> can load cta_market_id at tap time.
        async with async_session_scope() as s:
            item = await items_repo.create(
                s, url=f"https://demo.local/{market['id']}",
                url_hash=f"demo-{market['id']}"[:64],
                title_orig=question, body_orig="Demo item for the bet-on-news CTA.",
                lang_orig="en")
            item.cta_market_id = market["id"]
            item.status = "sent"
            item.translations.update({"en": {"title": question,
                                             "summary": "Tap a side to bet on this market."}})
            item_id = item.id

        snap = publisher.snapshot(item)
        msg_id = await publisher.post_item_to_channel(
            bot, snap, chat_id=chat_id, lang=settings.news_channel_lang, bot_username=me.username)
        if msg_id is None:
            print("✗ Post failed (see logs). Channel id / admin / egress?")
            return 1

    print(f"✓ Posted item #{item_id} (msg {msg_id}) to {chat_id} with Bet YES / Bet NO buttons.")
    print(f"  Deep-links:  https://t.me/{me.username}?start=nb-{item_id}-y  |  ...nb-{item_id}-n")
    print("  Tap a button as a connected user → amount picker → ✅ confirm → real (small) bet.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
