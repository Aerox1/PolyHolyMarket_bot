"""Guards for the handler integration contract:
  * every handler module exposes a callable ``register``;
  * every i18n key referenced in bot code exists in en.json (fail loudly instead
    of silently rendering the raw key string at runtime).
"""

import glob
import re

from core import i18n


def test_handler_modules_expose_register():
    from bot.handlers import connect, inquiry, start

    for mod in (start, connect, inquiry):
        assert callable(getattr(mod, "register", None)), f"{mod.__name__} missing register()"


def test_all_referenced_i18n_keys_exist_in_en():
    en_keys = i18n.all_keys("en")
    referenced: set[str] = set()
    for path in glob.glob("bot/**/*.py", recursive=True):
        with open(path, encoding="utf-8") as fh:
            referenced |= set(re.findall(r'["\'](bot\.[a-zA-Z0-9_.]+)["\']', fh.read()))
    missing = sorted(k for k in referenced if k not in en_keys)
    assert not missing, f"i18n keys referenced in code but missing from en.json: {missing}"
