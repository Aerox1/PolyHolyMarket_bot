from core import i18n


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


def test_no_orphan_keys_in_translations():
    """Every key in a translation must exist in en.json (the source of truth).

    Note: en may have keys the others lack (they fall back to en) — that is
    expected until the Phase 4 translation pass. We only forbid orphan keys.
    """
    en_keys = i18n.all_keys("en")
    for lang in ("fa", "ru", "zh"):
        orphans = i18n.all_keys(lang) - en_keys
        assert not orphans, f"{lang}.json has keys missing from en.json: {orphans}"


def test_normalize_unknown_lang_defaults_to_en():
    assert i18n.normalize_lang("xx") == "en"
    assert i18n.normalize_lang(None) == "en"
