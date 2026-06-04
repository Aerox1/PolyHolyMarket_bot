"""core.logging.RedactSecretsFilter — the last-line-of-defence secret scrubber.

Covers message scrubbing (key-shaped + exact configured values) and the traceback
(exc_info) path, which the formatter renders AFTER the filter runs."""

import logging

from core import logging as core_logging
from core.logging import RedactSecretsFilter


def _record(msg, *args, exc_info=None):
    return logging.LogRecord("t", logging.INFO, __file__, 1, msg, args, exc_info)


def test_redacts_eth_private_key_in_message():
    f = RedactSecretsFilter()
    rec = _record("signing with %s now", "0x" + "a" * 64)
    f.filter(rec)
    assert "aaaa" not in rec.getMessage() and "«redacted»" in rec.getMessage()


def test_redacts_telegram_bot_token_shape():
    f = RedactSecretsFilter()
    rec = _record("calling api with 123456789:AAHk-fakebottoken_value_thirtyplus_chars")
    f.filter(rec)
    assert "AAHk-fakebottoken" not in rec.getMessage()


def test_redacts_exact_configured_secret_value(monkeypatch):
    # The Fernet master key / session secret are not key-SHAPED, so they are only
    # caught by exact-value scrubbing of the configured settings.
    monkeypatch.setattr(core_logging.settings, "session_secret", "super-secret-session-value-xyz")
    f = RedactSecretsFilter()  # snapshots secrets at construction
    rec = _record("loaded session_secret=super-secret-session-value-xyz ok")
    f.filter(rec)
    assert "super-secret-session-value-xyz" not in rec.getMessage()


def test_scrubs_secret_inside_traceback():
    f = RedactSecretsFilter()
    key = "0x" + "b" * 64
    try:
        raise ValueError(f"boom with key {key}")
    except ValueError:
        import sys
        rec = _record("operation failed", exc_info=sys.exc_info())
    f.filter(rec)
    # filter must have rendered + scrubbed the traceback and cleared exc_info
    assert rec.exc_info is None
    assert key not in (rec.exc_text or "") and "«redacted»" in (rec.exc_text or "")
