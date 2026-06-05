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


def _pct(price) -> str:
    try:
        return f"{round(float(price) * 100)}%"
    except (TypeError, ValueError):
        return ""


def _outcome_text(outcome: dict) -> str:
    """Button/link text for a dynamic bet outcome — action-first so the wager is
    unambiguous: '✅ Bet Yes · 73%', '❌ Bet No · 27%', '📈 Bet <65,000 · 73%'
    (the market question is shown above, in the caption). The trailing % is the
    market's live odds for that outcome."""
    label = (outcome.get("label") or "?").strip()
    emoji = "✅" if label == "Yes" else "❌" if label == "No" else "📈"
    pct = _pct(outcome.get("price"))
    text = f"{emoji} Bet {label}"
    return f"{text} · {pct}" if pct else text


def snapshot(item) -> SimpleNamespace:
    """Detach the fields the publisher needs so the send can happen OUTSIDE the
    DB transaction (the ORM object would lazy-load on a closed session)."""
    return SimpleNamespace(
        id=item.id, title_orig=item.title_orig, body_orig=item.body_orig, url=item.url,
        translations=dict(item.translations or {}), hero_image_url=item.hero_image_url,
        cta_url=item.cta_url, cta_market_id=item.cta_market_id,
        cta_market_question=getattr(item, "cta_market_question", None),
        cta_outcomes=list(getattr(item, "cta_outcomes", None) or []),
    )


def _best_translation(item, lang: str) -> dict:
    tr = (item.translations or {}).get(lang)
    if tr:
        return tr
    any_tr = next(iter((item.translations or {}).values()), None)
    return any_tr or {"title": item.title_orig, "summary": item.body_orig or ""}


def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _summary_without_title(title: str | None, summary: str | None) -> str:
    """Drop leading summary lines that just repeat the headline, so a post never
    shows the title twice (feeds/old rows lead the body with the H1; a defensive
    twin of crawler.clean_body for already-stored items). Exact normalized match
    only — never trims real content."""
    nt = _norm(title)
    lines = (summary or "").splitlines()
    while lines and (_norm(lines[0]) == "" or (nt and _norm(lines[0]) == nt)):
        lines.pop(0)
    return "\n".join(lines).strip()


def _fit(plain: str, budget: int) -> str:
    """Escaped form of ``plain`` that fits ``budget`` chars; trims plaintext first
    (so the cut is never inside an entity), appends '…' if trimmed. Backs up to a
    word boundary so a trim never stops mid-word."""
    plain = plain or ""
    if len(_esc(plain)) <= budget:
        return _esc(plain)
    s = plain
    while s and len(_esc(s)) + 1 > budget:  # +1 for the ellipsis
        s = s[:-16]
    s = s.rstrip()
    sp = s.rfind(" ")  # drop a trailing partial word (only shortens → still fits)
    if sp > 0:
        s = s[:sp].rstrip(" ,;:–—-")
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

    # The market proposition the Bet YES/NO buttons act on — shown so the wager is
    # never ambiguous ("Celine Dion … Bet YES?" → here it's the actual question).
    market_q = getattr(item, "cta_market_question", None)
    market_html = f"📊 {_fit(market_q, 240)}" if market_q else ""

    used = len(title_html) + (len(market_html) + sep if market_html else 0)
    summary_budget = cap - used - len(tail) - sep * 2
    summary_plain = _summary_without_title(tr.get("title") or item.title_orig, tr.get("summary"))
    summary_html = _fit(summary_plain, summary_budget) if summary_budget > 24 else ""

    parts = [title_html]
    if market_html:
        parts.append(market_html)
    if summary_html:
        parts.append(summary_html)
    if source:
        parts.append(source)
    parts.append(footer)
    out = "\n\n".join(parts)
    if len(out) > cap:  # pathological title — drop the summary (keep the market line)
        out = "\n\n".join(p for p in (title_html, market_html, source, footer) if p)
    return out


def build_digest(items, *, lang: str, header: str, bot_username: str | None = None) -> str:
    """A per-user DM bundling several items (used by realtime + daily digest).
    Each item is short (title + clipped summary + a CTA link), so the total stays
    well under the 4096 message cap; items are pre-limited by the caller.

    When an item has resolved bet outcomes AND we know the bot username, each outcome
    is offered as an inline link (Yes/No, or the event's real choices with odds);
    otherwise a single Trade/Open link (channel buttons aren't possible in a DM
    that bundles many items)."""
    blocks = [f"<b>{_esc(header)}</b>"] if header else []
    for it in items:
        tr = _best_translation(it, lang)
        line = f"<b>{_esc((tr.get('title') or it.title_orig or '')[:_TITLE_MAX])}</b>"
        summary = _summary_without_title(tr.get("title") or it.title_orig, tr.get("summary"))[:240]
        if summary:
            line += f"\n{_esc(summary)}"
        outcomes = list(getattr(it, "cta_outcomes", None) or [])
        if outcomes and bot_username:
            mq = getattr(it, "cta_market_question", None)
            if mq:
                line += f"\n📊 {_esc(mq[:240])}"   # the proposition the bet acts on
            links = [f'<a href="{_esc(cta_mod.bet_deeplink(bot_username, item_id=it.id, index=i))}">'
                     f'{_esc(_outcome_text(o))}</a>' for i, o in enumerate(outcomes)]
            line += "\n" + " · ".join(links)
        else:
            link = it.cta_url or it.url
            if link:
                label = t("bot.news.cta_trade", lang) if it.cta_market_id else t("bot.news.cta_open", lang)
                line += f'\n🔗 <a href="{_esc(link)}">{_esc(label)}</a>'
        blocks.append(line)
    blocks.append(_esc(t("bot.news.nfa_footer", lang)))
    return "\n\n".join(blocks)


def _vote_text(label: str, count: int, total: int) -> str:
    """Label for an inline engagement-poll vote button: '🗳 Yes' before any votes,
    '🗳 Yes · 62%' once the tally has data (share of all votes on this item)."""
    base = f"🗳 {(label or '?').strip()}"
    return f"{base} · {round(count / total * 100)}%" if total else base


def vote_callback_data(item_id: int, index: int) -> str:
    """Callback payload for a poll vote button (well under Telegram's 64-byte cap).
    Only the item id + a small outcome index travel; the vote maps to
    ``cta_outcomes[index]`` server-side, the same index the bet button uses."""
    return f"nv:{item_id}:{int(index)}"


def _poll_rows(item, outcomes: list[dict], tallies: dict[int, int] | None) -> list[list[InlineKeyboardButton]]:
    """Inline engagement-poll vote buttons (sentiment/social-proof), laid out 2-up
    so they stay compact under the full-width bet buttons. One button per outcome,
    BY INDEX (so a vote maps to the same ``cta_outcomes[i]`` the bet button does);
    labels show the live share once votes exist."""
    counts = tallies or {}
    total = sum(counts.values())
    buttons = [InlineKeyboardButton(_vote_text(o.get("label") or "?", counts.get(i, 0), total),
                                    callback_data=vote_callback_data(item.id, i))
               for i, o in enumerate(outcomes)]
    return [buttons[j:j + 2] for j in range(0, len(buttons), 2)]


def build_keyboard(item, *, bot_username: str | None, lang: str,
                   with_poll: bool = False, tallies: dict[int, int] | None = None) -> InlineKeyboardMarkup | None:
    # When the item has resolved bet outcomes AND we know our bot username, surface a
    # button per outcome with live odds — Yes/No for a binary market, or the event's
    # real choices (candidates / price buckets) for a multi-outcome event. The
    # outcome→token mapping is resolved fresh server-side when the link is opened
    # (never from the payload), so labels can carry the (render-time) odds.
    outcomes = list(getattr(item, "cta_outcomes", None) or [])
    if outcomes and bot_username:
        # one bet button PER ROW (stacked) — the action-first labels ("📈 Bet <65,000
        # · 73%") read far better full-width than squeezed two-up.
        rows = [[InlineKeyboardButton(_outcome_text(o),
                                      url=cta_mod.bet_deeplink(bot_username, item_id=item.id, index=i))]
                for i, o in enumerate(outcomes)]
        if with_poll:
            # …then the engagement poll, INLINE on the same card (callback vote
            # buttons), so there's one message instead of a separate poll reply.
            rows.extend(_poll_rows(item, outcomes, tallies))
        return InlineKeyboardMarkup(rows)
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


async def _send_card(bot, item, *, chat_id: int, lang: str, bot_username: str | None,
                     with_poll: bool = False) -> int | None:
    """Send the news card (photo/text + caption + bet buttons, plus the inline
    engagement-poll vote buttons when ``with_poll``). Returns the message_id, or None
    on a transient failure. A parse failure NEVER returns None — it falls back to a
    plain-text send so the item can't get stuck."""
    kb = build_keyboard(item, bot_username=bot_username, lang=lang, with_poll=with_poll)

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


async def post_item_to_channel(bot, item, *, chat_id: int, lang: str,
                               bot_username: str | None, with_poll: bool = False) -> int | None:
    """Send one item to the channel as a SINGLE news card. When ``with_poll`` and the
    item has resolved outcomes, the card carries the inline engagement poll as
    callback vote buttons under the bet buttons (sentiment/social proof) — one
    message, not a separate poll reply. Votes are tallied live and the card's
    keyboard re-renders on each tap (see ``bot.handlers.news.on_news_vote``).
    Returns the card's message_id (the at-most-once anchor), or None on a transient
    failure (item left for retry)."""
    return await _send_card(bot, item, chat_id=chat_id, lang=lang,
                            bot_username=bot_username, with_poll=with_poll)
