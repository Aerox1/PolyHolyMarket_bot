"""Logging setup with a safety filter that redacts anything resembling a key.

Defence-in-depth: handlers are written to never log key material, but this filter
scrubs secrets from any log record — both the message AND a rendered traceback —
as a last line of defence. It works two ways: it replaces the EXACT configured
secret values verbatim (no false positives/negatives), and it masks anything
key-SHAPED (private keys, Fernet tokens, bot tokens) even if it isn't a configured
value.
"""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler

from core.config import settings

# Key-shaped material (masked even when it isn't a configured secret value).
_HEX_KEY = re.compile(r"\b(0x)?[0-9a-fA-F]{64}\b")          # eth private key
_B64_TOKEN = re.compile(r"\bg[AQ][A-Za-z0-9_\-]{60,}\b")    # Fernet ciphertext token (gA/gQ…)
_TG_TOKEN = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b")    # telegram bot token (id:secret)
_PATTERNS = (_HEX_KEY, _B64_TOKEN, _TG_TOKEN)

_MASK = "«redacted»"


def _secret_values() -> list[str]:
    """Exact secret strings to scrub verbatim — the most reliable redaction (covers
    the Fernet master key / session secret / api key, which are not key-SHAPED and
    so are missed by the regexes above)."""
    vals = [settings.telegram_bot_token, settings.session_secret, settings.gemini_api_key]
    vals += settings.encryption_keys
    # Only scrub non-trivial values (avoid masking empty/short config like "").
    return sorted({v for v in vals if v and len(v) >= 8}, key=len, reverse=True)


def _scrub(text: str, secrets: list[str]) -> str:
    for s in secrets:
        if s in text:
            text = text.replace(s, _MASK)
    for pat in _PATTERNS:
        text = pat.sub(_MASK, text)
    return text


class RedactSecretsFilter(logging.Filter):
    def __init__(self) -> None:
        super().__init__()
        self._secrets = _secret_values()

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        scrubbed = _scrub(msg, self._secrets)
        if scrubbed != msg:
            record.msg = scrubbed
            record.args = ()
        # Scrub a rendered traceback too: the Formatter appends exc_text AFTER this
        # filter runs, so a secret surfaced inside a third-party exception (e.g. on
        # the signing path) would otherwise be emitted un-redacted. Set exc_text and
        # clear exc_info so the Formatter emits the scrubbed text and re-renders nothing.
        if record.exc_info:
            rendered = logging.Formatter().formatException(record.exc_info)
            cleaned = _scrub(rendered, self._secrets)
            if cleaned != rendered:
                record.exc_text = cleaned
                record.exc_info = None
        return True


def setup_logging(level: str | None = None) -> None:
    logging.basicConfig(
        level=(level or settings.log_level).upper(),
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
    )
    root = logging.getLogger()
    redactor = RedactSecretsFilter()
    # Optional size-rotated file sink (opt-in via LOG_FILE) so a long-running bot
    # can't grow an unbounded log file.
    if settings.log_file:
        fh = RotatingFileHandler(
            settings.log_file, maxBytes=settings.log_max_bytes,
            backupCount=settings.log_backup_count, encoding="utf-8",
        )
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s — %(message)s"))
        root.addHandler(fh)
    # Attach to the root logger AND its current handlers, so records are scrubbed
    # regardless of which handler (incl. ones added later) emits them.
    root.addFilter(redactor)
    for handler in root.handlers:
        handler.addFilter(redactor)
    # Quiet noisy third-party loggers.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    # APScheduler logs every job submission/execution at INFO — on a 20s-tick bot
    # that floods the log and buries real events. Keep warnings+.
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
