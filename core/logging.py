"""Logging setup with a safety filter that redacts anything resembling a key.

Defence-in-depth: handlers are written to never log key material, but this
filter scrubs long hex strings / Fernet-looking tokens from any log record as a
last line of defence.
"""

from __future__ import annotations

import logging
import re

from core.config import settings

# 64-hex private keys (optionally 0x-prefixed) and 32+ byte base64 tokens.
_HEX_KEY = re.compile(r"\b(0x)?[0-9a-fA-F]{64}\b")
_B64_TOKEN = re.compile(r"\bg[AQ][A-Za-z0-9_\-]{60,}\b")  # Fernet tokens start with 'gA'/'gQ'


class RedactSecretsFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        if _HEX_KEY.search(msg) or _B64_TOKEN.search(msg):
            redacted = _B64_TOKEN.sub("«redacted»", _HEX_KEY.sub("«redacted»", msg))
            record.msg = redacted
            record.args = ()
        return True


def setup_logging(level: str | None = None) -> None:
    logging.basicConfig(
        level=(level or settings.log_level).upper(),
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
    )
    root = logging.getLogger()
    redactor = RedactSecretsFilter()
    # Attach to the root logger AND its current handlers, so records are scrubbed
    # regardless of which handler (incl. ones added later) emits them.
    root.addFilter(redactor)
    for handler in root.handlers:
        handler.addFilter(redactor)
    # Quiet noisy third-party loggers.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
