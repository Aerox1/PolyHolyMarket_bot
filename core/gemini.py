"""Gemini category-image generation with a hard weekly budget + graceful fallback.

Design:
* Generates ONLY an image (text-free, editorial illustration). All labels/info are
  overlaid by the frontend as UI elements — never baked into the image.
* Before each call, checks rolling-7-day spend against the live budget
  (``app_config.gemini_weekly_budget_usd``, default from settings). If the next
  image would exceed the budget, it is skipped — the caller falls back to a
  gradient placeholder card.
* Successes are charged to the ``gemini_usage`` ledger; failures are recorded at
  $0 for observability. The plaintext API key is never logged.
* Images are cached on disk under ``settings.cards_dir`` and served at ``/cards``.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from pathlib import Path

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from db.repositories import appconfig, categories as categories_repo, gemini_usage

logger = logging.getLogger(__name__)

_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
_MIME_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}


def cards_dir() -> Path:
    p = Path(settings.cards_dir)
    if not p.is_absolute():
        p = Path(__file__).resolve().parent.parent / settings.cards_dir
    p.mkdir(parents=True, exist_ok=True)
    return p


def build_prompt(title: str) -> str:
    return (
        f"A dramatic TWO-SIDED VERSUS battle poster for the topic \"{title}\", depicting it as a "
        f"head-to-head showdown between two opposing sides. "
        "Composition: a symmetrical split-screen face-off — left side VS right side — clashing at a "
        "charged, glowing dividing line straight down the middle of a vertical 9:16 frame. "
        "Two rival forces / symbolic champions representing each outcome confront each other with "
        "high tension and motion. Style: epic fighting-game splash art meets boxing/MMA fight poster "
        "— cinematic rim lighting, sparks and energy bursts at the central clash point, bold CONTRASTING "
        "color palettes for the two sides (one warm/red, one cool/blue), deep contrast, vivid and punchy. "
        "Keep the upper third and lower third calmer (gradient / negative space) so overlaid UI text "
        "stays legible. ABSOLUTELY NO text, words, letters, numbers, scores, logos, signatures, or "
        "watermarks anywhere in the image — pure imagery only."
    )


def _call_gemini_image(prompt: str, attempts: int = 3) -> tuple[bytes, str]:
    """Sync Gemini image call. Returns (image_bytes, mime). Raises on failure.

    Image responses are large (~1-2 MB base64); ``Connection: close`` avoids
    keep-alive issues through some proxies, and we retry transient transport
    errors (RemoteProtocolError etc.).
    """
    url = f"{_API_BASE}/models/{settings.gemini_image_model}:generateContent"
    headers = {"x-goog-api-key": settings.gemini_api_key, "Connection": "close"}
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseModalities": ["IMAGE"]},
    }
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with httpx.Client(timeout=90, trust_env=settings.gemini_trust_env) as client:
                r = client.post(url, headers=headers, json=body)
                r.raise_for_status()
                data = r.json()
            for cand in data.get("candidates", []):
                for part in cand.get("content", {}).get("parts", []):
                    inline = part.get("inlineData") or part.get("inline_data")
                    if inline and inline.get("data"):
                        mime = inline.get("mimeType") or inline.get("mime_type") or "image/png"
                        return base64.b64decode(inline["data"]), mime
            raise ValueError("no image in Gemini response")
        except httpx.TransportError as exc:  # transient — retry
            last_exc = exc
            logger.info("Gemini image transport error (attempt %d/%d): %s",
                        attempt, attempts, type(exc).__name__)
            continue
    raise last_exc if last_exc else RuntimeError("gemini image call failed")


def _save(slug: str, img: bytes, mime: str) -> str:
    ext = _MIME_EXT.get(mime, "png")
    fname = f"{slug}.{ext}"
    (cards_dir() / fname).write_bytes(img)
    return f"/cards/{fname}"  # served by the webapp


async def generate_category_image(session: AsyncSession, category) -> str | None:
    """Generate + cache a category image, honoring the weekly budget.

    Returns the served image path on success, or None (caller uses a placeholder).
    """
    if not settings.gemini_api_key:
        return None

    budget = await appconfig.get_float(session, appconfig.GEMINI_WEEKLY_BUDGET, settings.gemini_weekly_budget_usd)
    spent = await gemini_usage.weekly_spend(session)
    cost = settings.gemini_image_cost_usd
    if spent + cost > budget:
        logger.info("Gemini weekly budget reached (%.2f/%.2f) — skipping %s", spent, budget, category.slug)
        return None

    prompt = build_prompt(category.title)
    await categories_repo.set_image(session, category.id, path=None, status="generating", prompt=prompt)

    try:
        img, mime = await asyncio.to_thread(_call_gemini_image, prompt)
    except Exception as exc:  # noqa: BLE001 — never log the key; httpx errors don't contain it
        logger.warning("Gemini image failed for %s: %s", category.slug, type(exc).__name__)
        await gemini_usage.record(session, category_id=category.id, cost_usd=0, model=settings.gemini_image_model, ok=False)
        await categories_repo.set_image(session, category.id, path=None, status="failed")
        return None

    path = _save(category.slug, img, mime)
    await gemini_usage.record(session, category_id=category.id, cost_usd=cost, model=settings.gemini_image_model, ok=True)
    await categories_repo.set_image(session, category.id, path=path, status="ready")
    logger.info("Generated card image for %s (%d bytes)", category.slug, len(img))
    return path
