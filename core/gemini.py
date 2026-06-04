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
import json
import logging
from pathlib import Path

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import SUPPORTED_LANGUAGES, settings
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


async def generate_image(
    session: AsyncSession, *, slug: str, prompt: str, category_id: int | None = None
) -> str | None:
    """Generate + cache ONE image under ``cards/{slug}.*``, honoring the weekly
    budget. Generic core shared by category cards and the welcome banner.

    Returns the served image path on success, or None (budget/no-key/failure).
    """
    if not settings.gemini_api_key:
        return None

    budget = await appconfig.get_float(session, appconfig.GEMINI_WEEKLY_BUDGET, settings.gemini_weekly_budget_usd)
    spent = await gemini_usage.weekly_spend(session)
    cost = settings.gemini_image_cost_usd
    if spent + cost > budget:
        logger.info("Gemini weekly budget reached (%.2f/%.2f) — skipping %s", spent, budget, slug)
        return None

    try:
        img, mime = await asyncio.to_thread(_call_gemini_image, prompt)
    except Exception as exc:  # noqa: BLE001 — never log the key; httpx errors don't contain it
        logger.warning("Gemini image failed for %s: %s", slug, type(exc).__name__)
        await gemini_usage.record(session, category_id=category_id, cost_usd=0, model=settings.gemini_image_model, ok=False)
        return None

    path = _save(slug, img, mime)
    await gemini_usage.record(session, category_id=category_id, cost_usd=cost, model=settings.gemini_image_model, ok=True)
    logger.info("Generated image for %s (%d bytes)", slug, len(img))
    return path


async def generate_category_image(session: AsyncSession, category) -> str | None:
    """Generate + cache a category card image, tracking the category's status.

    Returns the served image path on success, or None (caller uses a placeholder).
    """
    if not settings.gemini_api_key:
        return None
    # Admin-set custom prompt takes priority over the default two-sided template.
    prompt = (getattr(category, "prompt_override", None) or "").strip() or build_prompt(category.title)
    await categories_repo.set_image(session, category.id, path=None, status="generating", prompt=prompt)
    path = await generate_image(session, slug=category.slug, prompt=prompt, category_id=category.id)
    await categories_repo.set_image(session, category.id, path=path, status=("ready" if path else "failed"))
    return path


# ── Welcome banner (the image shown at the top of /start) ───────────────────────

WELCOME_SLUG = "welcome"
WELCOME_PATH_KEY = "welcome_image_path"
WELCOME_PROMPT_KEY = "welcome_image_prompt"
DEFAULT_WELCOME_PROMPT = (
    "A premium, eye-catching WIDE hero banner (16:9) for a prediction-market trading "
    "app called PolyHolyMarket. Theme: 'Refer. Trade. Earn More.' — dramatic, vibrant, "
    "abstract financial energy: two opposing glowing market forces meeting at a charged "
    "central clash line, upward-surging candlestick charts, neon coins and reward "
    "particles streaming outward, cinematic rim lighting, deep contrast, bold warm-vs-cool "
    "palette, futuristic premium fintech feel. Leave calmer gradient space along the edges "
    "for overlaid UI. ABSOLUTELY NO text, words, letters, numbers, logos, signatures, or "
    "watermarks anywhere — pure imagery only."
)


def welcome_image_file() -> Path | None:
    """Filesystem path of the cached welcome banner, or None if not generated yet."""
    for ext in ("png", "jpg", "webp"):
        p = cards_dir() / f"{WELCOME_SLUG}.{ext}"
        if p.exists():
            return p
    return None


async def welcome_prompt(session: AsyncSession) -> str:
    return (await appconfig.get(session, WELCOME_PROMPT_KEY)) or DEFAULT_WELCOME_PROMPT


async def generate_welcome_image(session: AsyncSession, *, force: bool = False) -> str | None:
    """Generate the welcome banner (unless one already exists and force=False).

    Stores the served path in app_config so the dashboard/bot can find it.
    """
    if not force and welcome_image_file() is not None:
        return await appconfig.get(session, WELCOME_PATH_KEY)
    prompt = await welcome_prompt(session)
    path = await generate_image(session, slug=WELCOME_SLUG, prompt=prompt)
    if path:
        await appconfig.set_(session, WELCOME_PATH_KEY, path)
    return path


# ── Text generation (news translate + summarize) ────────────────────────────────
# Mirrors the image path: budget-gated against the SAME weekly ledger, blocking
# REST call off the event loop, never logs the key, charged to gemini_usage.


def _call_gemini_text(prompt: str, *, response_json: bool = False, attempts: int = 3) -> str:
    """Sync Gemini text call. Returns the concatenated text of the first
    candidate. Raises on failure. Set ``response_json`` to force a JSON body."""
    url = f"{_API_BASE}/models/{settings.gemini_text_model}:generateContent"
    headers = {"x-goog-api-key": settings.gemini_api_key, "Connection": "close"}
    body: dict = {"contents": [{"parts": [{"text": prompt}]}]}
    if response_json:
        body["generationConfig"] = {"responseMimeType": "application/json"}
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with httpx.Client(timeout=90, trust_env=settings.gemini_trust_env) as client:
                r = client.post(url, headers=headers, json=body)
                r.raise_for_status()
                data = r.json()
            chunks: list[str] = []
            for cand in data.get("candidates", []):
                for part in cand.get("content", {}).get("parts", []):
                    if isinstance(part.get("text"), str):
                        chunks.append(part["text"])
            text = "".join(chunks).strip()
            if not text:
                raise ValueError("no text in Gemini response")
            return text
        except httpx.TransportError as exc:  # transient — retry
            last_exc = exc
            logger.info("Gemini text transport error (attempt %d/%d): %s",
                        attempt, attempts, type(exc).__name__)
            continue
    raise last_exc if last_exc else RuntimeError("gemini text call failed")


async def generate_text(
    session: AsyncSession, *, prompt: str, kind: str = "news_text",
    category_id: int | None = None, response_json: bool = False,
) -> str | None:
    """Budget-gated text generation. Returns the model text, or None
    (no key / budget reached / failure). Charges the shared weekly ledger."""
    if not settings.gemini_api_key:
        return None

    budget = await appconfig.get_float(session, appconfig.GEMINI_WEEKLY_BUDGET, settings.gemini_weekly_budget_usd)
    spent = await gemini_usage.weekly_spend(session)
    cost = settings.gemini_text_cost_usd
    if spent + cost > budget:
        logger.info("Gemini weekly budget reached (%.2f/%.2f) — skipping %s", spent, budget, kind)
        return None

    try:
        text = await asyncio.to_thread(_call_gemini_text, prompt, response_json=response_json)
    except Exception as exc:  # noqa: BLE001 — never log the key; httpx errors don't carry it
        logger.warning("Gemini text failed for %s: %s", kind, type(exc).__name__)
        await gemini_usage.record(session, category_id=category_id, cost_usd=0,
                                  model=settings.gemini_text_model, ok=False, kind=kind)
        return None

    await gemini_usage.record(session, category_id=category_id, cost_usd=cost,
                              model=settings.gemini_text_model, ok=True, kind=kind)
    return text


def _build_translate_prompt(title: str, body: str, target_langs, tone_prompt: str) -> str:
    langs = ", ".join(target_langs)
    tone = f"\nEditorial tone/style to apply: {tone_prompt.strip()}" if tone_prompt.strip() else ""
    return (
        "You are a financial-news editor. Translate AND summarize the article below into EACH of these "
        f"language codes: {langs}.{tone}\n"
        "For every language produce a concise headline-style title and a 2–3 sentence neutral summary in "
        "THAT language. Do not add facts that are not in the source. Do not include markdown, links, or "
        "emojis.\n"
        "Return ONLY a JSON object mapping each language code to an object with keys \"title\" and "
        '"summary", e.g. {"en": {"title": "...", "summary": "..."}}.\n\n'
        f"ARTICLE TITLE:\n{title}\n\nARTICLE BODY:\n{body}"
    )


async def translate_summarize_news(
    session: AsyncSession, *, title: str, body: str,
    target_langs: tuple[str, ...] = SUPPORTED_LANGUAGES, tone_prompt: str = "",
) -> dict[str, dict[str, str]] | None:
    """ONE budget-charged call → ``{lang: {"title","summary"}}`` for all target
    languages. Routed to Claude (Agent SDK) or Gemini per ``news_text_provider``;
    Claude reaches Anthropic so it works when the VPN blocks Gemini. Returns None
    on no-provider / budget / failure (caller passes through source text)."""
    prompt = _build_translate_prompt(title, body or title, target_langs, tone_prompt)
    if settings.news_text_provider == "claude":
        from core import claude_text
        raw = await claude_text.generate_json(session, prompt=prompt, kind="news_text")
    else:
        raw = await generate_text(session, prompt=prompt, kind="news_text", response_json=True)
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        logger.warning("translate_summarize_news: model returned non-JSON")
        return None
    if not isinstance(parsed, dict):
        return None
    out: dict[str, dict[str, str]] = {}
    for lang in target_langs:
        entry = parsed.get(lang)
        if isinstance(entry, dict):
            t_val, s_val = entry.get("title"), entry.get("summary")
            if isinstance(t_val, str) and isinstance(s_val, str) and t_val.strip() and s_val.strip():
                out[lang] = {"title": t_val.strip(), "summary": s_val.strip()}
    return out or None
