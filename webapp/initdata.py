"""Telegram Mini App ``initData`` validation (the canonical HMAC algorithm).

We NEVER trust a client-supplied telegram id — identity comes only from a
validated ``initData`` string:
  secret_key = HMAC_SHA256(key="WebAppData", msg=bot_token)
  data_check_string = "\\n".join(sorted "k=v" for all fields except `hash`)
  expected = hex(HMAC_SHA256(key=secret_key, msg=data_check_string))
  valid iff expected == provided `hash`  AND  auth_date is fresh.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl

from core.config import settings


class InitDataError(Exception):
    """initData missing, malformed, tampered, or expired."""


@dataclass(frozen=True)
class TelegramUser:
    id: int
    username: str | None
    first_name: str | None
    language_code: str | None


def _secret_key(bot_token: str) -> bytes:
    return hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()


def validate(init_data: str, *, max_age_seconds: int | None = None) -> TelegramUser:
    if not init_data:
        raise InitDataError("missing initData")
    if not settings.telegram_bot_token:
        raise InitDataError("server bot token not configured")

    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    provided_hash = pairs.pop("hash", None)
    if not provided_hash:
        raise InitDataError("missing hash")

    data_check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    expected = hmac.new(_secret_key(settings.telegram_bot_token),
                        data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, provided_hash):
        raise InitDataError("bad hash")

    # Freshness (reject replays).
    max_age = settings.initdata_max_age_seconds if max_age_seconds is None else max_age_seconds
    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError:
        raise InitDataError("bad auth_date") from None
    if max_age > 0 and (time.time() - auth_date) > max_age:
        raise InitDataError("initData expired")

    try:
        user = json.loads(pairs.get("user", "{}"))
    except json.JSONDecodeError:
        raise InitDataError("bad user payload") from None
    if not user.get("id"):
        raise InitDataError("no user in initData")

    return TelegramUser(
        id=int(user["id"]),
        username=user.get("username"),
        first_name=user.get("first_name"),
        language_code=user.get("language_code"),
    )
