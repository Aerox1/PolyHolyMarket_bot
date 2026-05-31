"""Lightweight i18n: ``locales/{lang}.json`` + ``t(key, lang, **vars)``.

Chosen over gettext/babel because (a) one mechanism serves both the bot and the
dashboard, (b) JSON is editable by non-engineer translators, and (c) per-user
language already lives in the DB (``users.language``), so no Accept-Language
negotiation is needed.

Keys are dotted paths into a nested catalog, e.g. ``"bot.start.welcome"``.
``en.json`` is the source of truth; missing keys fall back to English, then to
the raw key string (so a missing translation is visible, never a crash).
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

SUPPORTED = ("en", "fa", "ru", "zh")
DEFAULT = "en"
LANG_NAMES = {"en": "English", "fa": "فارسی", "ru": "Русский", "zh": "中文"}
LANG_FLAGS = {"en": "🇬🇧", "fa": "🇮🇷", "ru": "🇷🇺", "zh": "🇨🇳"}

_LOCALES_DIR = Path(__file__).resolve().parent.parent / "locales"
_CATALOGS: dict[str, dict] = {}


def _load() -> None:
    """Load every locale JSON once into memory."""
    if _CATALOGS:
        return
    for lang in SUPPORTED:
        path = _LOCALES_DIR / f"{lang}.json"
        if path.exists():
            try:
                _CATALOGS[lang] = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.error("Failed to load locale %s: %s", lang, exc)
                _CATALOGS[lang] = {}
        else:
            _CATALOGS[lang] = {}
    if not _CATALOGS.get(DEFAULT):
        logger.warning("Default locale '%s' is empty or missing.", DEFAULT)


def _deep_get(catalog: dict, dotted_key: str):
    node = catalog
    for part in dotted_key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node if isinstance(node, str) else None


def normalize_lang(lang: str | None) -> str:
    return lang if lang in SUPPORTED else DEFAULT


def t(key: str, lang: str = DEFAULT, **variables) -> str:
    """Resolve a dotted key for ``lang`` with EN fallback and var interpolation."""
    _load()
    lang = normalize_lang(lang)
    value = _deep_get(_CATALOGS.get(lang, {}), key)
    if value is None and lang != DEFAULT:
        value = _deep_get(_CATALOGS.get(DEFAULT, {}), key)
    if value is None:
        return key  # visible, never crashes
    if variables:
        try:
            return value.format(**variables)
        except (KeyError, IndexError):
            return value
    return value


def text_dir(lang: str) -> str:
    """'rtl' for Farsi, else 'ltr' (read from the catalog's _meta)."""
    _load()
    return _CATALOGS.get(normalize_lang(lang), {}).get("_meta", {}).get("dir", "ltr")


@lru_cache(maxsize=8)
def catalog_json(lang: str) -> str:
    """The full catalog for a language as a JSON string (for the dashboard JS)."""
    _load()
    return json.dumps(_CATALOGS.get(normalize_lang(lang), {}), ensure_ascii=False)


def all_keys(lang: str = DEFAULT) -> set[str]:
    """Every dotted leaf key in a catalog (used by the completeness test)."""
    _load()

    def walk(node, prefix=""):
        out: set[str] = set()
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "_meta":
                    continue
                out |= walk(v, f"{prefix}{k}.")
        else:
            out.add(prefix.rstrip("."))
        return out

    return walk(_CATALOGS.get(normalize_lang(lang), {}))
