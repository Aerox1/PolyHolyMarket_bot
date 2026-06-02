"""Publish ready news items to the configured Telegram news channel.

Builds an HTML caption in the channel language (source link + localized
not-financial-advice footer) with a tap-to-trade CTA button that deep-links back
into the bot. Runs in the bot process; never touches wallet keys.

Truncation is HTML-SAFE: plaintext title/summary are trimmed to a budget BEFORE
escaping/assembly, so a cut can never land inside an entity or tag (which would
make Telegram reject the message with a parse error). A final plain-text fallback
guarantees a parse failure can never pin an item in the publish queue.
"""

from __future__ import annotations

import html
import logging
import re
from types import SimpleNamespace

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, TelegramError

from bot.news import cta as cta_mod
from core.i18n import t

logger = logging.getLogger(__name__)

_CAPTION_CAP = 1024   # Telegram photo-caption limit
_TEXT_CAP = 4096      # Telegram message limit
_TITLE_MAX = 220      # hard plaintext cap on the title
_TAG_RE = re.compile(r"<[^>]+>")


def _esc(s: str | None) -> str:
    return html.escape(s or "")


def _strip_tags(s: str) -> str:
    return html.unescape(_TAG_RE.sub("", s or ""))


def snapshot(item) -> SimpleNamespace:
    """Detach the fields the publisher needs so the send can happen OUTSIDE the
    DB transaction (the ORM object would lazy-load on a closed session)."""
    return SimpleNamespace(
        id=item.id, title_orig=item.title_orig, body_orig=item.body_orig, url=item.url,
        translations=dict(item.translations or {}), hero_image_url=item.hero_image_url,
        cta_url=item.cta_url, cta_market_id=item.cta_market_id,
    )


def _best_translation(item, lang: str) -> dict:
    tr = (item.translations or {}).get(lang)
    if tr:
        return tr
    any_tr = next(iter((item.translations or {}).values()), None)
    return any_tr or {"title": item.title_orig, "summary": item.body_orig or ""}


def _fit(plain: str, budget: int) -> str:
    """Escaped form of ``plain`` that fits ``budget`` chars; trims plaintext first
    (so the cut is never inside an entity), appends '…' if trimmed."""
    plain = plain or ""
    if len(_esc(plain)) <= budget:
        return _esc(plain)
    s = plain
    while s and len(_esc(s)) + 1 > budget:  # +1 for the ellipsis
        s = s[:-16]
    s = s.rstrip()
    return (_esc(s) + "…") if s else ""


def build_caption(item, *, lang: str, cap: int) -> str:
    """HTML caption guaranteed ≤ cap, never cutting an entity/tag."""
    tr = _best_translation(item, lang)
    source = (f'🔗 <a href="{_esc(item.url)}">{_esc(t("bot.news.source", lang))}</a>'
              if item.url else "")
    footer = _esc(t("bot.news.nfa_footer", lang))
    tail = "\n\n".join(p for p in (source, footer) if p)
    sep = 2  # len("\n\n")

    title_budget = max(40, cap - len(tail) - sep * 2 - 7)  # 7 ≈ "<b></b>"
    title_html = f"<b>{_fit(tr.get('title') or item.title_orig or '', min(title_budget, _TITLE_MAX))}</b>"

    summary_budget = cap - len(title_html) - len(tail) - sep * 2
    summary_html = _fit(tr.get("summary") or "", summary_budget) if summary_budget > 24 else ""

    parts = [title_html]
    if summary_html:
        parts.append(summary_html)
    if source:
        parts.append(source)
    parts.append(footer)
    out = "\n\n".join(parts)
    if len(out) > cap:  # pathological title — drop the summary (still tag-safe)
        out = "\n\n".join(p for p in (title_html, source, footer) if p)
    return out


def build_keyboard(item, *, bot_username: str | None, lang: str) -> InlineKeyboardMarkup | None:
    url = item.cta_url or (cta_mod.news_deeplink(bot_username, item_id=item.id) if bot_username else None)
    if not url:
        return None  # no link target yet → post the article without a button
    label = t("bot.news.cta_trade", lang) if item.cta_market_id else t("bot.news.cta_open", lang)
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, url=url)]])


async def channel_is_admin(bot, chat_id: int) -> bool:
    """Best-effort: the bot must be an admin of the channel to post."""
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(chat_id, me.id)
        return getattr(member, "status", None) in ("administrator", "creator")
    except TelegramError as exc:
        logger.info("channel admin check failed for %s: %s", chat_id, type(exc).__name__)
        return False


async def post_item_to_channel(bot, item, *, chat_id: int, lang: str, bot_username: str | None) -> int | None:
    """Send one item to the channel. Returns the message_id, or None on a
    transient failure (item left for retry). A parse failure NEVER returns None —
    it falls back to a plain-text send so the item can't get stuck."""
    kb = build_keyboard(item, bot_username=bot_username, lang=lang)

    if item.hero_image_url:
        caption = build_caption(item, lang=lang, cap=_CAPTION_CAP)
        try:
            msg = await bot.send_photo(chat_id=chat_id, photo=item.hero_image_url,
                                       caption=caption, parse_mode="HTML", reply_markup=kb)
            return msg.message_id
        except BadRequest:  # bad image OR caption parse — fall back to a text message
            logger.info("photo send rejected for item %s; falling back to text", item.id)
        except TelegramError as exc:
            logger.warning("photo send failed for item %s: %s", item.id, type(exc).__name__)
            return None

    text = build_caption(item, lang=lang, cap=_TEXT_CAP)
    try:
        msg = await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML",
                                     reply_markup=kb, disable_web_page_preview=False)
        return msg.message_id
    except BadRequest:  # HTML parse failure despite safe truncation → plain text, no parse
        logger.info("HTML caption rejected for item %s; sending plain text", item.id)
        try:
            msg = await bot.send_message(chat_id=chat_id, text=_strip_tags(text)[:_TEXT_CAP], reply_markup=kb)
            return msg.message_id
        except TelegramError as exc:
            logger.warning("plain send failed for item %s: %s", item.id, type(exc).__name__)
            return None
    except TelegramError as exc:
        logger.warning("text send failed for item %s: %s", item.id, type(exc).__name__)
        return None
