import re

from core import i18n


def _flat(d, prefix=""):
    out = {}
    for k, v in d.items():
        nk = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(_flat(v, nk))
        else:
            out[nk] = v
    return out


def test_basic_lookup_and_interpolation():
    msg = i18n.t("bot.start.welcome", "en", name="Sam")
    assert "Sam" in msg


def test_fallback_to_english_for_untranslated_key():
    # All locales are now fully translated, so simulate a missing key in 'ru'
    # by temporarily removing one and confirming it falls back to English.
    i18n._load()
    en_val = i18n.t("bot.confirm.yes", "en")
    ru_confirm = i18n._CATALOGS["ru"]["bot"]["confirm"]
    saved = ru_confirm.pop("yes")
    try:
        assert i18n.t("bot.confirm.yes", "ru") == en_val
    finally:
        ru_confirm["yes"] = saved


def test_missing_key_returns_key_string():
    assert i18n.t("bot.does.not.exist", "en") == "bot.does.not.exist"


def test_translated_string_differs_from_english():
    assert i18n.t("bot.start.language_set", "fa") != i18n.t("bot.start.language_set", "en")


def test_text_direction():
    assert i18n.text_dir("fa") == "rtl"
    assert i18n.text_dir("en") == "ltr"
    assert i18n.text_dir("zh") == "ltr"


def test_all_locales_complete():
    """Completeness gate: every en.json key MUST exist in fa/ru/zh (no silent
    English fallback), and no locale may carry orphan keys absent from en.

    This replaces the old orphan-only check that deferred the en→lang direction.
    Adding a new en.json string now fails the build until it is translated.
    """
    i18n._load()
    en = set(_flat(i18n._CATALOGS["en"]))
    for lang in ("fa", "ru", "zh"):
        loc = set(_flat(i18n._CATALOGS[lang]))
        missing = en - loc
        orphan = loc - en
        assert not missing, f"{lang}.json is missing {len(missing)} keys, e.g. {sorted(missing)[:8]}"
        assert not orphan, f"{lang}.json has orphan keys absent from en: {sorted(orphan)[:8]}"


def test_placeholder_parity_across_locales():
    """Every translated string must use the same {placeholder} set as en.json,
    so str.format() never raises KeyError or silently drops a variable."""
    i18n._load()
    en = _flat(i18n._CATALOGS["en"])
    ph = lambda s: set(re.findall(r"{[^}]+}", s)) if isinstance(s, str) else set()
    for lang in ("fa", "ru", "zh"):
        loc = _flat(i18n._CATALOGS[lang])
        for key, en_val in en.items():
            if key in loc:
                assert ph(loc[key]) == ph(en_val), (
                    f"{lang}.json '{key}' placeholders {ph(loc[key])} != en {ph(en_val)}"
                )


def test_markdown_entities_balanced():
    """Every bot.* string must have balanced Markdown *bold* / _italic_ markers
    (after removing `code` spans and {placeholders}, which aren't parsed), so a
    parse_mode=Markdown send never fails with 'can't parse entities'. Regression
    guard for command-syntax tokens like <token_id> that left a dangling _."""
    i18n._load()
    strip = lambda s: re.sub(r"\{[^}]*\}", "", re.sub(r"`[^`]*`", "", s))
    for lang in ("en", "fa", "ru", "zh"):
        for key, val in _flat(i18n._CATALOGS[lang]).items():
            if not key.startswith("bot.") or not isinstance(val, str):
                continue
            s = strip(val)
            assert s.count("_") % 2 == 0, f"{lang} '{key}': unbalanced _ (would break Markdown)"
            assert s.count("*") % 2 == 0, f"{lang} '{key}': unbalanced * (would break Markdown)"


def test_normalize_unknown_lang_defaults_to_en():
    assert i18n.normalize_lang("xx") == "en"
    assert i18n.normalize_lang(None) == "en"
