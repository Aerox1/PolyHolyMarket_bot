"""Shared "is this headline sensitive?" gate for the news voice.

Conservative keyword check (war / death / disaster / violence). Used to switch the
playful poll labels (publisher) AND the headline/summary editorial tone (render)
OFF, so the funny voice never lands on a tragedy. A category/LLM classifier would
be a better upgrade later — this is the deterministic floor.
"""

from __future__ import annotations

import re

SENSITIVE_RE = re.compile(
    r"\b(war|wars|killed|kill|dead|death|deaths|attack|attacks|bomb|bombed|bombing|"
    r"shooting|terror|terrorist|hostage|hostages|casualt(?:y|ies)|massacre|genocide|"
    r"earthquake|flood|wildfire|airstrike|missile|wounded|injured|funeral|murder|"
    r"invasion|famine|outbreak|crash|quake)\b",
    re.IGNORECASE,
)


def is_sensitive(title: str | None) -> bool:
    return bool(title and SENSITIVE_RE.search(title))
