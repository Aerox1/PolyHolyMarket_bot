"""News-pipeline model constraints: status/kind/delivery CHECKs, dedup keys,
and cascade deletes. Uses the sync ``session`` fixture (conftest) on SQLite with
PRAGMA foreign_keys=ON, so CASCADE behaves like Postgres."""

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from db.models import (
    Category,
    NewsChannelPost,
    NewsDelivered,
    NewsItem,
    User,
    UserNewsPrefs,
    UserTopicFollow,
)


def _user(session, telegram_id=7001, **kw):
    u = User(telegram_id=telegram_id, username="u", language="en", **kw)
    session.add(u)
    session.flush()
    return u


def _item(session, url_hash="h1", **kw):
    it = NewsItem(url=f"https://x/{url_hash}", url_hash=url_hash, title_orig="t", **kw)
    session.add(it)
    session.flush()
    return it


def _category(session, slug="econ", **kw):
    c = Category(slug=slug, title="Economy", **kw)
    session.add(c)
    session.flush()
    return c


# ── defaults + CHECK constraints ─────────────────────────────────────────────

def test_news_item_defaults(session):
    it = _item(session)
    assert it.status == "backlog"
    assert it.image_status == "none"
    assert it.excluded_from_autopublish is False
    assert it.translations == {}


def test_news_item_bad_status_rejected(session):
    with pytest.raises(IntegrityError):
        _item(session, url_hash="bad", status="bogus")
    session.rollback()


@pytest.mark.parametrize(
    "st", ["backlog", "approved", "translating", "rendering", "ready", "sent", "rejected"]
)
def test_news_item_all_valid_statuses_accepted(session, st):
    it = _item(session, url_hash=f"st-{st}", status=st)
    assert it.status == st


def test_news_item_translations_roundtrip_and_inplace_mutation(session):
    """The translations column must persist BOTH a full reassignment AND an
    in-place key write (the pipeline mutates it in place) — guards the
    MutableDict.as_mutable(JSON) wrapper."""
    it = _item(session, url_hash="tr")
    it.translations = {"en": {"title": "T", "summary": "S"}}
    session.commit()
    session.expire_all()
    reloaded = session.get(NewsItem, it.id)
    assert reloaded.translations == {"en": {"title": "T", "summary": "S"}}
    # in-place write on a loaded row — silently lost without MutableDict
    reloaded.translations["fa"] = {"title": "ع", "summary": "خ"}
    session.commit()
    session.expire_all()
    final = session.get(NewsItem, it.id)
    assert set(final.translations) == {"en", "fa"}


def test_news_item_bad_image_status_rejected(session):
    with pytest.raises(IntegrityError):
        _item(session, url_hash="bad2", image_status="weird")
    session.rollback()


def test_news_item_url_hash_unique(session):
    _item(session, url_hash="dup")
    with pytest.raises(IntegrityError):
        _item(session, url_hash="dup")
    session.rollback()


def test_category_kind_default_and_check(session):
    c = _category(session, slug="d1")
    assert c.kind == "market"
    with pytest.raises(IntegrityError):
        _category(session, slug="d2", kind="nonsense")
    session.rollback()


def test_news_prefs_delivery_check(session):
    u = _user(session, telegram_id=7100)
    session.add(UserNewsPrefs(user_id=u.id, delivery="bogus"))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_news_prefs_digest_hour_range(session):
    u = _user(session, telegram_id=7101)
    session.add(UserNewsPrefs(user_id=u.id, digest_hour=99))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_news_prefs_defaults(session):
    u = _user(session, telegram_id=7102)
    p = UserNewsPrefs(user_id=u.id)
    session.add(p)
    session.flush()
    assert p.delivery == "daily"
    assert p.digest_hour == 9
    assert p.max_per_digest == 5
    assert p.only_relevant is False


# ── dedup / idempotency keys ─────────────────────────────────────────────────

def test_news_channel_post_unique_per_item_chat_lang(session):
    it = _item(session, url_hash="cp")
    session.add(NewsChannelPost(news_item_id=it.id, chat_id=-100, lang="en"))
    session.flush()
    session.add(NewsChannelPost(news_item_id=it.id, chat_id=-100, lang="en"))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_news_channel_post_allows_other_lang(session):
    it = _item(session, url_hash="cp2")
    session.add(NewsChannelPost(news_item_id=it.id, chat_id=-100, lang="en"))
    session.add(NewsChannelPost(news_item_id=it.id, chat_id=-100, lang="fa"))
    session.flush()  # different lang → no conflict


def test_news_delivered_dedup_pk(session):
    u = _user(session, telegram_id=7200)
    it = _item(session, url_hash="nd")
    session.add(NewsDelivered(user_id=u.id, news_item_id=it.id, channel="digest"))
    session.flush()
    session.add(NewsDelivered(user_id=u.id, news_item_id=it.id, channel="realtime"))
    with pytest.raises(IntegrityError):
        session.flush()  # composite PK (user, item) — at most once per user
    session.rollback()


# ── cascade deletes ──────────────────────────────────────────────────────────

def test_cascade_delete_user_removes_news_rows(session):
    u = _user(session, telegram_id=7300)
    c = _category(session, slug="cas", kind="news")
    it = _item(session, url_hash="cas")
    session.add(UserNewsPrefs(user_id=u.id))
    session.add(UserTopicFollow(user_id=u.id, category_id=c.id))
    session.add(NewsDelivered(user_id=u.id, news_item_id=it.id, channel="digest"))
    session.flush()
    uid = u.id
    session.delete(u)
    session.flush()
    assert session.scalar(select(UserNewsPrefs).where(UserNewsPrefs.user_id == uid)) is None
    assert session.scalar(select(UserTopicFollow).where(UserTopicFollow.user_id == uid)) is None
    assert session.scalar(select(NewsDelivered).where(NewsDelivered.user_id == uid)) is None
    # the news item itself is independent of the user and remains
    assert session.scalar(select(NewsItem).where(NewsItem.id == it.id)) is not None


def test_delete_category_nulls_news_item_fk(session):
    c = _category(session, slug="catfk", kind="news")
    it = _item(session, url_hash="catfk", category_id=c.id)
    session.flush()
    session.delete(c)
    session.flush()
    session.refresh(it)
    assert it.category_id is None  # ON DELETE SET NULL preserves the item
