"""Invite-code access gate for NEW users (admin-toggleable).

A new user is 'locked' (``User.access_granted`` False) until they enter a valid
code. Two things unlock the bot:
  * the global access code (default ``POLYHOLY``, admin-editable in app_config), or
  * any real OTHER user's referral code — which ALSO records the referral edge so
    the referrer is credited through the normal referral flow.

Existing users are grandfathered (migration 0010 backfills access_granted=true).
Enforcement lives in ``bot.middleware`` (locked users are blocked from every
handler except code entry).
"""

from __future__ import annotations

import re

from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from db.models import User
from db.repositories import appconfig
from db.repositories import rewards as rewards_repo

# /start r-<code>  (a deep-link invite) — the r-code unlocks just like a typed code.
_START_REF_RE = re.compile(r"^/start(?:@\w+)?\s+r-(\S+)", re.IGNORECASE)


async def gate_enabled(session: AsyncSession) -> bool:
    # admin override (app_config) wins; otherwise the env/default (on in prod, off in tests).
    default = "1" if settings.access_gate_enabled else "0"
    return (await appconfig.get(session, appconfig.ACCESS_GATE_ENABLED, default)) != "0"


def code_from_update(update) -> str | None:
    """The code a locked user is offering: a plain typed message (the code), or the
    ``r-<code>`` arg of a /start deep-link. None for other commands/callbacks."""
    msg = getattr(update, "message", None)
    text = (msg.text or "").strip() if (msg is not None and getattr(msg, "text", None)) else ""
    if not text:
        return None
    m = _START_REF_RE.match(text)
    if m:
        return m.group(1).strip()
    if text.startswith("/"):
        return None  # other commands aren't access codes
    return text


async def try_grant(session: AsyncSession, user: User, code: str) -> bool:
    """Grant access if ``code`` is the global access code OR a valid referral code
    (which also attributes the referral, crediting the referrer). Sets
    ``user.access_granted`` and returns True on success; False if the code is invalid."""
    code = (code or "").strip()
    if not code:
        return False
    access_code = (await appconfig.get(session, appconfig.ACCESS_CODE, appconfig.DEFAULT_ACCESS_CODE)
                  ) or appconfig.DEFAULT_ACCESS_CODE
    if code.lower() == access_code.strip().lower():
        user.access_granted = True
        return True
    # a real OTHER user's referral code → attribute (credits referrer) + unlock
    if await rewards_repo.attribute_referral(session, user, code):
        user.access_granted = True
        return True
    return False
