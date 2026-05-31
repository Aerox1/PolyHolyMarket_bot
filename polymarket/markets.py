"""Public Polymarket (Gamma) data for the Mini App: categories (tags), the
markets in a category, and a single market. No auth — all public endpoints.

Categories are derived from the tags attached to the highest-volume events, so
they reflect what's actually active ("Trump", "Iran", "Sports", "NBA"…).

These are blocking httpx calls; async callers (the webapp) wrap them in
``asyncio.to_thread``.
"""

from __future__ import annotations

import json
import logging

import httpx

from core.config import settings

logger = logging.getLogger(__name__)

# Structural / non-topical tags to exclude from the category deck (admins can
# still hide more in the dashboard).
_TAG_DENYLIST = {
    "hide from new", "all", "games", "recurring", "weekly", "monthly", "daily",
    "new", "trending", "live", "sports games",
}


def _client() -> httpx.Client:
    return httpx.Client(timeout=20, base_url=settings.gamma_url)


def _as_list(payload) -> list:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        return payload.get("data") or payload.get("events") or payload.get("markets") or []
    return []


def _jsarr(value) -> list:
    """Gamma encodes arrays as JSON strings ('["Yes","No"]')."""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return []
    return []


def top_categories(limit: int = 30, event_scan: int = 120) -> list[dict]:
    """Rank tags by the aggregated 24h volume of the events they appear on."""
    with _client() as c:
        r = c.get("/events", params={"order": "volume24hr", "ascending": "false",
                                     "closed": "false", "limit": event_scan})
        r.raise_for_status()
        events = _as_list(r.json())

    agg: dict[str, dict] = {}
    for e in events:
        vol = float(e.get("volume24hr") or 0)
        for tag in (e.get("tags") or []):
            label = (tag.get("label") or "").strip()
            slug = tag.get("slug") or label.lower().replace(" ", "-")
            if not label or label.lower() in _TAG_DENYLIST:
                continue
            row = agg.setdefault(slug, {"slug": slug, "title": label, "tag_id": str(tag.get("id") or ""),
                                        "tag_slug": slug, "volume": 0.0})
            row["volume"] += vol
    ranked = sorted(agg.values(), key=lambda x: x["volume"], reverse=True)
    return ranked[:limit]


def _normalize_market(m: dict) -> dict | None:
    """Return a normalized BINARY (Yes/No) market, or None if not bettable."""
    outcomes = _jsarr(m.get("outcomes"))
    prices = _jsarr(m.get("outcomePrices"))
    tokens = _jsarr(m.get("clobTokenIds"))
    if len(outcomes) != 2 or len(tokens) != 2:
        return None
    if m.get("closed") or m.get("active") is False:
        return None

    def price(i):
        try:
            return float(prices[i])
        except (IndexError, ValueError, TypeError):
            return None

    return {
        "id": m.get("conditionId") or m.get("id"),
        "question": m.get("question") or m.get("title"),
        "volume": float(m.get("volume24hr") or m.get("volume") or 0),
        "neg_risk": bool(m.get("negRisk")),
        "yes_token": str(tokens[0]),
        "no_token": str(tokens[1]),
        "yes_price": price(0),
        "no_price": price(1),
        "image": m.get("image") or m.get("icon"),
    }


def category_markets(tag_slug: str, limit: int = 40) -> list[dict]:
    """Binary markets in a category (tag), sorted by 24h volume desc."""
    with _client() as c:
        r = c.get("/events", params={"tag_slug": tag_slug, "order": "volume24hr",
                                     "ascending": "false", "closed": "false", "limit": 30})
        r.raise_for_status()
        events = _as_list(r.json())

    out: list[dict] = []
    for e in events:
        for m in (e.get("markets") or []):
            nm = _normalize_market(m)
            if nm and nm["id"]:
                nm["event_title"] = e.get("title")
                out.append(nm)
    out.sort(key=lambda x: x["volume"], reverse=True)
    # de-dup by market id, keep highest volume
    seen, deduped = set(), []
    for m in out:
        if m["id"] in seen:
            continue
        seen.add(m["id"])
        deduped.append(m)
    return deduped[:limit]


def trending_markets(limit: int = 12) -> list[dict]:
    """Top binary markets across all of Polymarket by 24h volume."""
    with _client() as c:
        r = c.get("/markets", params={"order": "volume24hr", "ascending": "false",
                                      "closed": "false", "active": "true", "limit": max(limit * 3, 30)})
        r.raise_for_status()
        rows = _as_list(r.json())
    out: list[dict] = []
    seen: set[str] = set()
    for m in rows:
        nm = _normalize_market(m)
        if nm and nm["id"] and nm["id"] not in seen:
            seen.add(nm["id"])
            out.append(nm)
    out.sort(key=lambda x: x["volume"], reverse=True)
    return out[:limit]


def get_market(condition_id: str) -> dict | None:
    with _client() as c:
        r = c.get("/markets", params={"condition_ids": condition_id, "limit": 1})
        if r.status_code != 200:
            return None
        rows = _as_list(r.json())
    return _normalize_market(rows[0]) if rows else None
