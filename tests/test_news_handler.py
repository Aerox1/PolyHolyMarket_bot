"""/news delivery-preferences handler (bot/handlers/news.py).

Covers the pure helpers (_mode_row / _quiet_str / _settings_text), the
settings-screen renderer, the /news command delegation + error path, every
on_news callback verb (mode/relevant/quiet/hour/sethour/topics/topic/back),
the uid-None and query-None guards, the exception→generic-error branch, and
register() wiring. news.py opens its OWN async sessions via async_session_scope,
so we seed + assert through the same conftest temp DB (pattern (a)). Telegram is
faked; no network. Asserting STRUCTURE (callback_data/markup/DB state), not copy.
"""

from __future__ import annotations

from types import SimpleNamespace

from telegram import InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler, CommandHandler

from bot.handlers import common, news
from core.i18n import t
from db.engine import async_session_scope
from db.models import Category
from db.repositories import news_prefs
from db.repositories import users as users_repo


# ── Telegram fakes (mirrors tests/test_news_bet.py) ──────────────────────────────

class _Query:
    """Callback query whose .message is NOT a telegram.Message → common.screen
    falls through to update.effective_message.reply_text (our _RecMsg)."""

    def __init__(self, data):
        self.data = data
        self.message = None
        self.answered = False

    async def answer(self, *a, **k):
        self.answered = True

    async def edit_message_text(self, *a, **k):
        pass


class _RecMsg:
    def __init__(self):
        self.sent = []  # list[(text, kwargs)]

    async def reply_text(self, text, **kw):
        self.sent.append((text, kw))


def _update(*, query=None, msg=None):
    return SimpleNamespace(callback_query=query, effective_message=msg,
                           effective_user=SimpleNamespace(id=111), effective_chat=None)


def _ctx(**user_data):
    user_data.setdefault("lang", "en")
    return SimpleNamespace(user_data=user_data, bot=None,
                           application=SimpleNamespace(bot_data={}))


# ── DB seed helpers (conftest temp DB, pattern (a)) ──────────────────────────────

async def _seed_user(tg_id=900):
    async with async_session_scope() as s:
        u = await users_repo.get_or_create_user(s, telegram_id=tg_id, username="u",
                                                 first_name="U", default_language="en")
        return u.id


async def _seed_topic(slug="elections", title="Elections", kind="news",
                      hidden=False, display_order=0):
    """A Category row that list_news_topics() will surface (kind news/both, not hidden)."""
    async with async_session_scope() as s:
        c = Category(slug=slug, title=title, kind=kind, hidden=hidden,
                     display_order=display_order)
        s.add(c)
        await s.flush()
        return c.id


def _all_buttons(markup: InlineKeyboardMarkup):
    return [b for row in markup.inline_keyboard for b in row]


def _datas(markup: InlineKeyboardMarkup):
    return [b.callback_data for b in _all_buttons(markup) if b.callback_data]


# ── _mode_row ────────────────────────────────────────────────────────────────────

def test_mode_row_three_buttons_only_current_checked():
    ctx = _ctx()
    row = news._mode_row(ctx, "daily")
    # exactly the three delivery modes, in order, callback_data news:mode:<mode>
    assert [b.callback_data for b in row] == ["news:mode:off", "news:mode:daily", "news:mode:realtime"]
    # only the current mode ("daily") carries the check mark
    checked = [b.text for b in row if "✓" in b.text]
    assert len(checked) == 1 and t("bot.news.mode_daily", "en") in checked[0]
    # the off/realtime labels are present and unchecked
    assert "✓" not in row[0].text and "✓" not in row[2].text


def test_mode_row_realtime_current():
    row = news._mode_row(_ctx(), "realtime")
    assert sum("✓" in b.text for b in row) == 1
    assert "✓" in row[2].text  # realtime is the 3rd button


# ── _quiet_str ───────────────────────────────────────────────────────────────────

def test_quiet_str_off_when_unset():
    ctx = _ctx()
    prefs = SimpleNamespace(quiet_start=None, quiet_end=None)
    assert news._quiet_str(ctx, prefs) == t("bot.news.off", "en")
    # one side None is still "off" (guard is OR)
    assert news._quiet_str(ctx, SimpleNamespace(quiet_start=22, quiet_end=None)) == t("bot.news.off", "en")
    assert news._quiet_str(ctx, SimpleNamespace(quiet_start=None, quiet_end=7)) == t("bot.news.off", "en")


def test_quiet_str_window_formatting():
    ctx = _ctx()
    assert news._quiet_str(ctx, SimpleNamespace(quiet_start=22, quiet_end=7)) == "22:00–07:00"
    # zero-padded both ends
    assert news._quiet_str(ctx, SimpleNamespace(quiet_start=1, quiet_end=9)) == "01:00–09:00"


# ── _settings_text ───────────────────────────────────────────────────────────────

async def test_settings_text_contains_all_fields():
    ctx = _ctx()
    prefs = SimpleNamespace(delivery="realtime", digest_hour=9, quiet_start=22,
                            quiet_end=7, only_relevant=True)
    txt = await news._settings_text(ctx, prefs, followed_n=3)
    # HTML wrapper + the rendered field values
    assert txt.startswith("<b>") and "</b>" in txt
    assert t("bot.news.settings_title", "en") in txt
    assert t("bot.news.mode_realtime", "en") in txt  # current delivery mode
    assert "09:00" in txt                            # digest hour
    assert "22:00–07:00" in txt                      # quiet window
    assert t("bot.news.on", "en") in txt             # only_relevant True → "On"
    assert ">3<" in txt or "3</b>" in txt            # followed count


async def test_settings_text_relevant_off_shows_off_label():
    ctx = _ctx()
    prefs = SimpleNamespace(delivery="off", digest_hour=0, quiet_start=None,
                            quiet_end=None, only_relevant=False)
    txt = await news._settings_text(ctx, prefs, followed_n=0)
    assert "00:00" in txt
    # only_relevant False → the "off" label appears (also quiet is off)
    assert t("bot.news.off", "en") in txt


# ── show_settings_screen ─────────────────────────────────────────────────────────

async def test_show_settings_screen_renders_full_keyboard():
    uid = await _seed_user(tg_id=901)
    # seed prefs explicitly so we know the rendered defaults
    async with async_session_scope() as s:
        await news_prefs.get_or_create(s, uid)

    msg = _RecMsg()
    ctx = _ctx(db_user_id=uid)
    await news.show_settings_screen(_update(query=_Query("news:back"), msg=msg), ctx)

    assert msg.sent, "settings screen should render a message"
    text, kw = msg.sent[0]
    assert kw["parse_mode"] == "HTML"
    markup = kw["reply_markup"]
    datas = _datas(markup)
    # mode row + hour + quiet + relevant + topics (+ nav home appended by with_nav)
    assert "news:mode:off" in datas and "news:mode:daily" in datas and "news:mode:realtime" in datas
    assert "news:hour" in datas
    assert "news:quiet" in datas
    assert "news:relevant" in datas
    assert "news:topics" in datas
    assert "menu:home" in datas  # nav row appended
    # body is the settings HTML
    assert t("bot.news.settings_title", "en") in text


async def test_show_settings_screen_uid_none_errors():
    msg = _RecMsg()
    ctx = _ctx()  # no db_user_id
    await news.show_settings_screen(_update(query=_Query("news:back"), msg=msg), ctx)
    # falls back to common.reply with the generic error (Markdown reply)
    assert msg.sent and msg.sent[0][0] == t("bot.error.generic", "en")
    assert msg.sent[0][1].get("parse_mode") == "Markdown"


# ── news_command ─────────────────────────────────────────────────────────────────

async def test_news_command_delegates_to_settings():
    uid = await _seed_user(tg_id=902)
    msg = _RecMsg()
    ctx = _ctx(db_user_id=uid)
    # command-style update: no callback_query → screen sends via effective_message
    upd = SimpleNamespace(callback_query=None, effective_message=msg, effective_chat=None,
                          effective_user=SimpleNamespace(id=111),
                          message=SimpleNamespace(text="/news"))
    await news.news_command(upd, ctx)
    assert msg.sent and t("bot.news.settings_title", "en") in msg.sent[0][0]


async def test_news_command_swallows_error_to_generic(monkeypatch):
    async def boom(update, context):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(news, "show_settings_screen", boom)
    msg = _RecMsg()
    upd = SimpleNamespace(callback_query=None, effective_message=msg, effective_chat=None,
                          effective_user=SimpleNamespace(id=111),
                          message=SimpleNamespace(text="/news"))
    await news.news_command(upd, _ctx(db_user_id=1))
    # error path replies with the generic key, suite stays green
    assert msg.sent and msg.sent[0][0] == t("bot.error.generic", "en")


# ── on_news: guards ──────────────────────────────────────────────────────────────

async def test_on_news_query_none_is_noop():
    ctx = _ctx(db_user_id=1)
    # no callback_query → returns immediately, nothing sent
    upd = _update(query=None, msg=_RecMsg())
    await news.on_news(upd, ctx)
    assert upd.effective_message.sent == []


async def test_on_news_uid_none_answers_then_returns():
    q = _Query("news:mode:daily")
    msg = _RecMsg()
    ctx = _ctx()  # no db_user_id
    await news.on_news(_update(query=q, msg=msg), ctx)
    assert q.answered is True   # always acks the tap
    assert msg.sent == []       # but does nothing further (no screen, no error)


# ── on_news: mode ────────────────────────────────────────────────────────────────

async def test_on_news_mode_sets_delivery_and_rerenders():
    uid = await _seed_user(tg_id=910)
    msg = _RecMsg()
    ctx = _ctx(db_user_id=uid)
    await news.on_news(_update(query=_Query("news:mode:realtime"), msg=msg), ctx)
    # DB mutated
    async with async_session_scope() as s:
        prefs = await news_prefs.get_or_create(s, uid)
        assert prefs.delivery == "realtime"
    # screen re-rendered with realtime checked
    assert msg.sent and "news:mode:realtime" in _datas(msg.sent[0][1]["reply_markup"])


# ── on_news: relevant ────────────────────────────────────────────────────────────

async def test_on_news_relevant_toggles_db():
    uid = await _seed_user(tg_id=911)
    async with async_session_scope() as s:
        before = (await news_prefs.get_or_create(s, uid)).only_relevant
    msg = _RecMsg()
    await news.on_news(_update(query=_Query("news:relevant"), msg=msg), _ctx(db_user_id=uid))
    async with async_session_scope() as s:
        after = (await news_prefs.get_or_create(s, uid)).only_relevant
    assert after is (not before)
    assert msg.sent  # re-rendered the settings screen


# ── on_news: quiet (off → 22..7 → off) ───────────────────────────────────────────

async def test_on_news_quiet_toggles_window_on_and_off():
    uid = await _seed_user(tg_id=912)
    # starts unset → first tap sets the overnight window 22:00–07:00
    msg1 = _RecMsg()
    await news.on_news(_update(query=_Query("news:quiet"), msg=msg1), _ctx(db_user_id=uid))
    async with async_session_scope() as s:
        prefs = await news_prefs.get_or_create(s, uid)
        assert prefs.quiet_start == 22 and prefs.quiet_end == 7
    # second tap clears it back to off
    msg2 = _RecMsg()
    await news.on_news(_update(query=_Query("news:quiet"), msg=msg2), _ctx(db_user_id=uid))
    async with async_session_scope() as s:
        prefs = await news_prefs.get_or_create(s, uid)
        assert prefs.quiet_start is None and prefs.quiet_end is None
    assert msg1.sent and msg2.sent


# ── on_news: hour → _show_hours ──────────────────────────────────────────────────

async def test_on_news_hour_shows_24_cells_plus_back():
    uid = await _seed_user(tg_id=913)
    msg = _RecMsg()
    await news.on_news(_update(query=_Query("news:hour"), msg=msg), _ctx(db_user_id=uid))
    assert msg.sent
    datas = _datas(msg.sent[0][1]["reply_markup"])
    # 24 hour cells news:sethour:0..23
    sethours = [d for d in datas if d.startswith("news:sethour:")]
    assert len(sethours) == 24
    assert "news:sethour:0" in sethours and "news:sethour:23" in sethours
    assert "news:back" in datas  # back row
    # pick-hour prompt body
    assert t("bot.news.pick_hour", "en") in msg.sent[0][0]


# ── on_news: sethour → set_digest_hour ───────────────────────────────────────────

async def test_on_news_sethour_persists_and_rerenders():
    uid = await _seed_user(tg_id=914)
    msg = _RecMsg()
    await news.on_news(_update(query=_Query("news:sethour:14"), msg=msg), _ctx(db_user_id=uid))
    async with async_session_scope() as s:
        assert (await news_prefs.get_or_create(s, uid)).digest_hour == 14
    # back on the settings screen, hour button shows 14:00
    assert msg.sent and "14:00" in msg.sent[0][0]


# ── on_news: topics ──────────────────────────────────────────────────────────────

async def test_on_news_topics_renders_topic_rows():
    uid = await _seed_user(tg_id=915)
    cid = await _seed_topic(slug="elections-915", title="Elections")
    msg = _RecMsg()
    await news.on_news(_update(query=_Query("news:topics"), msg=msg), _ctx(db_user_id=uid))
    assert msg.sent
    datas = _datas(msg.sent[0][1]["reply_markup"])
    assert f"news:topic:{cid}" in datas
    assert "news:back" in datas
    # not-yet-followed → ▫️ marker, title present
    btns = _all_buttons(msg.sent[0][1]["reply_markup"])
    topic_btn = next(b for b in btns if b.callback_data == f"news:topic:{cid}")
    assert "▫️" in topic_btn.text and "Elections" in topic_btn.text
    assert t("bot.news.topics_title", "en") in msg.sent[0][0]


async def test_on_news_topics_empty_state():
    uid = await _seed_user(tg_id=916)
    # no topics seeded → empty-state screen with just a back button
    msg = _RecMsg()
    await news.on_news(_update(query=_Query("news:topics"), msg=msg), _ctx(db_user_id=uid))
    assert msg.sent
    assert t("bot.news.no_topics", "en") in msg.sent[0][0]
    datas = _datas(msg.sent[0][1]["reply_markup"])
    assert "news:back" in datas
    assert not any(d.startswith("news:topic:") for d in datas)


# ── on_news: topic:<id> → toggle_follow then re-render ───────────────────────────

async def test_on_news_topic_toggle_follows_then_shows_check():
    uid = await _seed_user(tg_id=917)
    cid = await _seed_topic(slug="sports-917", title="Sports")
    msg = _RecMsg()
    await news.on_news(_update(query=_Query(f"news:topic:{cid}"), msg=msg), _ctx(db_user_id=uid))
    # DB: now following
    async with async_session_scope() as s:
        assert cid in await news_prefs.followed_ids(s, uid)
    # re-rendered topics screen with the ✅ marker on this topic
    btns = _all_buttons(msg.sent[0][1]["reply_markup"])
    topic_btn = next(b for b in btns if b.callback_data == f"news:topic:{cid}")
    assert "✅" in topic_btn.text

    # second tap unfollows
    msg2 = _RecMsg()
    await news.on_news(_update(query=_Query(f"news:topic:{cid}"), msg=msg2), _ctx(db_user_id=uid))
    async with async_session_scope() as s:
        assert cid not in await news_prefs.followed_ids(s, uid)


# ── on_news: back → settings screen ──────────────────────────────────────────────

async def test_on_news_back_returns_to_settings():
    uid = await _seed_user(tg_id=918)
    msg = _RecMsg()
    await news.on_news(_update(query=_Query("news:back"), msg=msg), _ctx(db_user_id=uid))
    assert msg.sent and t("bot.news.settings_title", "en") in msg.sent[0][0]
    assert "news:mode:daily" in _datas(msg.sent[0][1]["reply_markup"])


# ── on_news: unknown verb is a silent no-op (still answers) ───────────────────────

async def test_on_news_unknown_verb_noop():
    uid = await _seed_user(tg_id=919)
    q = _Query("news:bogus")
    msg = _RecMsg()
    await news.on_news(_update(query=q, msg=msg), _ctx(db_user_id=uid))
    assert q.answered is True
    assert msg.sent == []  # no branch matched → nothing rendered


# ── on_news: exception inside → generic error ────────────────────────────────────

async def test_on_news_exception_falls_back_to_generic(monkeypatch):
    uid = await _seed_user(tg_id=920)

    async def boom(*a, **k):
        raise RuntimeError("repo blew up")

    # force the mode branch (set_delivery) to raise; the try/except → common.reply generic
    monkeypatch.setattr(news.news_prefs, "set_delivery", boom)
    msg = _RecMsg()
    await news.on_news(_update(query=_Query("news:mode:daily"), msg=msg), _ctx(db_user_id=uid))
    assert msg.sent and msg.sent[0][0] == t("bot.error.generic", "en")


# ── register ─────────────────────────────────────────────────────────────────────

def test_register_wires_command_and_callback():
    added = []

    class _App:
        def add_handler(self, h):
            added.append(h)

    news.register(_App())
    cmd = [h for h in added if isinstance(h, CommandHandler)]
    cbq = [h for h in added if isinstance(h, CallbackQueryHandler)]
    assert len(cmd) == 1 and "news" in cmd[0].commands
    assert len(cbq) == 1
    # callback pattern matches every news: verb
    assert cbq[0].pattern.search("news:mode:off")
    assert cbq[0].pattern.search("news:topic:5")
    assert not cbq[0].pattern.search("menu:home")
