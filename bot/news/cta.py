"""News → Polymarket market CTA. Resolves the most relevant market for an
article (once, at render time — cached on the row) and builds the bot deep-link.

All Polymarket calls are blocking Gamma HTTP, wrapped in ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

from polymarket import markets

logger = logging.getLogger(__name__)

# How many top lexical candidates to hand the LLM relevance judge. Lexical overlap
# is RECALL (shortlist plausible events); the LLM is the PRECISION decision.
LLM_CANDIDATES = 5

# Sentinel: the LLM judge could not run (no provider / budget reached / parse
# failure). Distinct from None ("ran, none are relevant") so the caller can fall
# back to the lexical best instead of dropping the CTA.
_LLM_UNAVAILABLE: object = object()

# Minimum shared significant words between a headline and a market question for the
# market to count as "about this story". Gamma's title_like search falls back to
# the highest-volume market when nothing matches, so without this gate EVERY
# article gets stapled to the current top market (e.g. a celebrity-death story got
# "Will Mexico win the World Cup?"). 2 is deliberately precise over permissive.
MIN_TOKEN_OVERLAP = 2

# How many trending (actively-bet) markets/events to fold into the candidate pool,
# so news can match a market people are already betting even if text search ranked
# it low.
TRENDING_CANDIDATES = 40

# Max bet options shown under a news item (top by probability). Multi-outcome events
# (elections, price buckets) can have dozens of sub-markets; we surface the leaders.
OUTCOMES_CAP = 4

# Common words that carry no topical signal — excluded from the overlap check so a
# match must share real entities/keywords, not "after"/"says"/"will".
_STOPWORDS = {
    "the", "and", "for", "are", "but", "not", "you", "all", "any", "can", "has", "had",
    "her", "him", "his", "our", "out", "day", "new", "now", "old", "see", "two", "use",
    "who", "why", "how", "its", "let", "say", "she", "too", "was", "way", "with", "that",
    "this", "from", "have", "what", "when", "your", "said", "were", "they", "them", "than",
    "then", "into", "over", "more", "some", "such", "only", "also", "been", "being", "after",
    "about", "could", "would", "should", "amid", "says", "year", "years", "first", "last",
    "next", "week", "month", "today", "report", "reports", "news", "update", "live", "video",
    "watch", "may", "will", "amid",
}


def _significant_tokens(text: str) -> set[str]:
    """Lowercased content words ≥3 chars, de-stopworded, plural-normalized, digits
    dropped (so a shared year like '2026' can't manufacture a match)."""
    out: set[str] = set()
    for w in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if len(w) < 3 or w.isdigit() or w in _STOPWORDS:
            continue
        out.add(w[:-1] if w.endswith("s") and len(w) > 3 else w)  # cheap plural strip
    return out


def _relevance(title: str, question: str | None) -> int:
    """How many significant words a headline and a market question share."""
    return len(_significant_tokens(title) & _significant_tokens(question or ""))


# ── dynamic-outcome CTA (event-aware) ────────────────────────────────────────────

def _candidate_name_tokens(event: dict) -> set[str]:
    """Significant tokens that exist ONLY because a sub-market is labelled with a
    candidate name (``groupItemTitle``) — e.g. {mark, cuban} for the "Mark Cuban"
    sub-market of a nominee event. A headline overlapping an event solely on these
    is an entity collision (the person is merely named in a market), not topical
    relevance about the story."""
    names: set[str] = set()
    for m in (event.get("markets") or []):
        names |= _significant_tokens(m.get("groupItemTitle") or "")
    return names


def _event_relevance(title: str, event: dict) -> int:
    """Best keyword overlap between the headline and the event title OR any of its
    sub-market questions (so "Iowa Governor Winner" matches via "...Iowa...race").

    Entity-collision guard: if the ONLY overlap is a sub-market's candidate name —
    no event-title hit and no shared *topical* (non-name) token — score 0. Without
    it, a story that merely NAMES a person who happens to have a candidacy market
    (e.g. "Mark Cuban sells his Bitcoin" → the "2028 Democratic Nominee" event, via
    its "Mark Cuban" sub-market) clears the gate. Both ``resolve_cta`` and the
    crawl-time ``trending_matches`` auto-approver call this, so both inherit the fix."""
    htoks = _significant_tokens(title)
    title_overlap = htoks & _significant_tokens(event.get("title"))
    name_tokens = _candidate_name_tokens(event)
    sub_overlap: set[str] = set()
    best = len(title_overlap)
    for m in (event.get("markets") or []):
        q_shared = htoks & _significant_tokens(m.get("question"))
        sub_overlap |= q_shared
        best = max(best, len(q_shared))
    # No event-title hit AND every shared sub-market token is just a candidate name
    # → entity collision, not about this story.
    if not title_overlap and not (sub_overlap - name_tokens):
        return 0
    return best


def _build_outcomes(event: dict) -> list[dict]:
    """The bettable outcomes for a matched event. Each outcome is a (sub)market + a
    side to buy — the bet primitive stays binary:
      * multi-outcome (≥2 live, labelled sub-markets): each one's YES side, labelled
        by ``groupItemTitle`` (candidate / price bucket), sorted by probability;
      * otherwise the single live binary market's Yes and No.
    Only OPEN, priced sub-markets are offered (``_normalize_market`` drops the rest)."""
    live = [(m, nm) for m in (event.get("markets") or [])
            if (nm := markets._normalize_market(m)) and nm.get("id")]
    if not live:
        return []
    multi, seen = [], set()
    for m, nm in live:
        label = (m.get("groupItemTitle") or "").strip()
        if label and nm.get("yes_price") is not None and nm["id"] not in seen:
            seen.add(nm["id"])  # Gamma can repeat a sub-market; one button per market
            multi.append({"label": label[:48], "market_id": nm["id"], "side": "yes", "price": nm["yes_price"]})
    if len(multi) >= 2:
        multi.sort(key=lambda o: o["price"], reverse=True)
        return multi[:OUTCOMES_CAP]
    # plain binary Yes/No on the most-priced live market
    nm = max(live, key=lambda x: (x[1].get("yes_price") or 0))[1]
    return [
        {"label": "Yes", "market_id": nm["id"], "side": "yes", "price": nm.get("yes_price")},
        {"label": "No",  "market_id": nm["id"], "side": "no",  "price": nm.get("no_price")},
    ]


async def _llm_pick_event(session, title: str, body: str, candidates: list[dict]):
    """Ask the news-text LLM which candidate event the story is GENUINELY about.

    Returns the chosen index, ``None`` (the model judged none relevant → drop the
    CTA), or ``_LLM_UNAVAILABLE`` (no provider / budget reached / unparseable → the
    caller keeps the lexical best). Routes through the SAME budget-gated provider as
    translate (claude_text / gemini), so it costs one small, ledger-charged call."""
    from core.config import settings
    listing = "\n".join(
        f"{i}: {e.get('title')!r} — e.g. {((e.get('markets') or [{}])[0] or {}).get('question') or ''!r}"
        for i, e in enumerate(candidates)
    )
    prompt = (
        "Match a NEWS STORY to the ONE prediction market it is GENUINELY about. A "
        "shared person name or a single incidental keyword is NOT enough — the "
        "market's question must concern what the story actually reports/claims.\n\n"
        f"STORY HEADLINE: {title}\nSTORY BODY: {(body or '')[:1200]}\n\n"
        "CANDIDATE MARKETS:\n" + listing + "\n\n"
        'Reply with ONLY JSON: {"index": <int>} for the market the story is about, '
        'or {"index": null} if NONE genuinely fits.'
    )
    try:
        if settings.news_text_provider == "claude":
            from core import claude_text
            raw = await claude_text.generate_json(session, prompt=prompt, kind="cta_pick")
        else:
            from core import gemini
            raw = await gemini.generate_text(session, prompt=prompt, kind="cta_pick", response_json=True)
    except Exception as exc:  # noqa: BLE001 — relevance check is best-effort
        logger.info("CTA llm pick failed: %s", type(exc).__name__)
        return _LLM_UNAVAILABLE
    if not raw:
        return _LLM_UNAVAILABLE  # no provider / budget reached
    try:
        idx = json.loads(raw).get("index")
    except (json.JSONDecodeError, ValueError, AttributeError):
        return _LLM_UNAVAILABLE
    if idx is None:
        return None  # model: none of the candidates fit → no CTA
    if isinstance(idx, int) and 0 <= idx < len(candidates):
        return idx
    return _LLM_UNAVAILABLE  # out-of-range / garbage → don't trust it, keep lexical best


async def resolve_cta(
    *, title: str, body: str = "", category_tag_slug: str | None = None,
    hint_market_id: str | None = None, session=None,
) -> dict | None:
    """Resolve a news item's bet CTA as ``{market_id, question, outcomes}`` or None.

    Lexical overlap is RECALL: it shortlists topically-plausible events (relevance-
    gated, with the entity-collision guard). When a ``session`` is supplied the LLM
    judge makes the PRECISION decision — picks the right candidate or rejects all
    (catching same-topic look-alikes a keyword match can't, e.g. a Bitcoin-holdings
    story vs a Bitcoin-price market). Without a session, or if the judge can't run,
    it degrades to the lexical best. Best-effort → None on no match / error."""
    if hint_market_id:
        try:
            m = await asyncio.to_thread(markets.get_market, hint_market_id)
        except Exception:  # noqa: BLE001
            m = None
        if not m:
            return None
        return {"market_id": m["id"], "question": m.get("question"), "outcomes": [
            {"label": "Yes", "market_id": m["id"], "side": "yes", "price": m.get("yes_price")},
            {"label": "No",  "market_id": m["id"], "side": "no",  "price": m.get("no_price")}]}
    events: list[dict] = []
    try:
        if title:
            events += await asyncio.to_thread(markets.search_events, title, 20) or []
        events += await asyncio.to_thread(markets.trending_events, TRENDING_CANDIDATES) or []
    except Exception as exc:  # noqa: BLE001 — CTA is optional; never fail the render
        logger.info("CTA resolve failed: %s", type(exc).__name__)
        return None
    # RECALL: every event clearing the (entity-collision-guarded) lexical gate, best first.
    scored = [(s, e) for e in events if isinstance(e, dict)
              and (s := _event_relevance(title, e)) >= MIN_TOKEN_OVERLAP]
    if not scored:
        return None
    scored.sort(key=lambda se: se[0], reverse=True)
    candidates = [e for _s, e in scored[:LLM_CANDIDATES]]

    chosen = candidates[0]  # lexical best — default + fallback
    if session is not None:
        pick = await _llm_pick_event(session, title, body, candidates)
        if pick is None:
            return None  # PRECISION: the judge found none genuinely relevant → no CTA
        if pick is not _LLM_UNAVAILABLE:
            chosen = candidates[pick]

    outcomes = _build_outcomes(chosen)
    if not outcomes:
        return None
    question = chosen.get("title")
    # a plain binary (both sides share one market) reads better as the market question
    if len(outcomes) == 2 and outcomes[0]["market_id"] == outcomes[1]["market_id"]:
        for m in (chosen.get("markets") or []):
            if (m.get("conditionId") or m.get("id")) == outcomes[0]["market_id"]:
                question = m.get("question") or question
                break
    return {"market_id": outcomes[0]["market_id"], "question": question, "outcomes": outcomes}


async def trending_matches(candidates: list[tuple[int, str]]) -> set[int]:
    """Given ``(item_id, title)`` pairs, return the ids whose headline matches a
    currently-trending Polymarket event (same relevance gate as ``resolve_cta``).

    Fetches the trending event pool ONCE and scores every candidate against it, so
    auto-approval costs a single Gamma call per crawl cycle regardless of how many
    fresh items there are. Best-effort → empty set on error (auto-approval is
    optional; a Gamma hiccup must never wedge the crawl)."""
    if not candidates:
        return set()
    try:
        events = await asyncio.to_thread(markets.trending_events, TRENDING_CANDIDATES) or []
    except Exception as exc:  # noqa: BLE001 — optional; never fail the crawl
        logger.info("trending auto-approve fetch failed: %s", type(exc).__name__)
        return set()
    events = [e for e in events if isinstance(e, dict)]
    if not events:
        return set()
    matched: set[int] = set()
    for item_id, title in candidates:
        if any(_event_relevance(title, e) >= MIN_TOKEN_OVERLAP for e in events):
            matched.add(item_id)
    return matched


def news_deeplink(bot_username: str, *, item_id: int) -> str:
    """t.me deep-link back into the bot, carrying the NEWS ITEM id (``n-<id>``).

    The market id is NOT encoded directly: a Polymarket conditionId is 66 chars
    and Telegram caps the start payload at 64. Instead /start loads the item and
    routes to its cached ``cta_market_id`` (tap-to-trade) when present, else opens
    the bot. Mirrors start.py's start-arg convention (``r-<code>``)."""
    return f"https://t.me/{bot_username}?start=n-{item_id}"


def bet_deeplink(bot_username: str, *, item_id: int, index: int) -> str:
    """Deep-link pre-selecting OUTCOME #``index`` on the item (``nb-<id>-<index>``).

    Only the item id + a small integer index are encoded (well under Telegram's
    64-char start-payload cap). The token/price/side are resolved server-side from
    the item's stored ``cta_outcomes[index]`` at click time — never carried in the
    link — so an edited payload can at most pick a different item or outcome index,
    never inject an arbitrary token or amount. (Legacy ``nb-<id>-y|n`` links from
    older posts still resolve: y→0, n→1.)"""
    return f"https://t.me/{bot_username}?start=nb-{item_id}-{int(index)}"
