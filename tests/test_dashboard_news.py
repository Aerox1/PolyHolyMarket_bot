"""News admin dashboard: auth gate, rendering, keyless flag-writes (approve/
reject/sources/settings), and the no-Gemini/keyless invariant."""

import re

import pytest
from starlette.testclient import TestClient

from core import crypto
from db.engine import SessionLocal
from db.models import Admin, NewsItem, NewsSource


def _csrf(client) -> str:
    html = client.get("/login").text
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', html)
    assert m, "no csrf token in login form"
    return m.group(1)


def _login(client, *, superadmin=True):
    with SessionLocal() as s:
        from sqlalchemy import select
        if s.scalar(select(Admin).where(Admin.username == "newsadmin")) is None:
            s.add(Admin(username="newsadmin", password_hash=crypto.hash_password("pw!"),
                        is_superadmin=superadmin))
            s.commit()
    token = _csrf(client)
    r = client.post("/login", data={"username": "newsadmin", "password": "pw!", "csrf_token": token})
    assert r.status_code in (302, 303)
    return token


def _seed_item(status="backlog", **kw):
    with SessionLocal() as s:
        it = NewsItem(url=kw.get("url", "https://x/1"), url_hash=kw.get("url_hash", "h1"),
                      title_orig=kw.get("title_orig", "Fed holds rates"), status=status)
        s.add(it)
        s.commit()
        return it.id


@pytest.fixture
def client(monkeypatch):
    # keyless invariant: the dashboard must NEVER call Gemini. If any news flow
    # reaches the Gemini client, these raise and the test fails loudly.
    def _no_gemini(*a, **k):
        raise AssertionError("dashboard must not call Gemini (keyless invariant)")

    monkeypatch.setattr("core.gemini._call_gemini_text", _no_gemini)
    monkeypatch.setattr("core.gemini._call_gemini_image", _no_gemini)
    from dashboard.app import app
    return TestClient(app, follow_redirects=False)


# ── auth + rendering ──────────────────────────────────────────────────────────

def test_news_requires_auth(client):
    r = client.get("/news")
    assert r.status_code in (302, 303) and "/login" in r.headers.get("location", "")


def test_news_pages_render(client):
    _login(client)
    for path in ("/news", "/news/sources", "/news/settings"):
        r = client.get(path)
        assert r.status_code == 200, path
    # item detail
    item_id = _seed_item()
    r = client.get(f"/news/{item_id}")
    assert r.status_code == 200
    assert "Fed holds rates" in r.text


def test_news_item_404(client):
    _login(client)
    assert client.get("/news/999999").status_code == 404


# ── flag-writes ───────────────────────────────────────────────────────────────

def _post(client, path, token, **data):
    return client.post(path, data={"csrf_token": token, **data})


def test_approve_and_reject_are_flag_writes(client):
    from sqlalchemy import select
    token = _login(client)
    item_id = _seed_item(status="backlog")
    r = _post(client, f"/news/{item_id}/action", token, action="approve")
    assert r.status_code in (302, 303)
    with SessionLocal() as s:
        it = s.get(NewsItem, item_id)
        assert it.status == "approved" and it.approved_at is not None

    r = _post(client, f"/news/{item_id}/action", token, action="reject")
    assert r.status_code in (302, 303)
    with SessionLocal() as s:
        assert s.get(NewsItem, item_id).status == "rejected"


def test_action_requires_csrf(client):
    _login(client)
    item_id = _seed_item()
    r = client.post(f"/news/{item_id}/action", data={"action": "approve"})  # no csrf
    assert r.status_code == 400


def test_edit_translations(client):
    token = _login(client)
    item_id = _seed_item()
    r = _post(client, f"/news/{item_id}/translations", token,
              title_en="EN title", summary_en="EN summary", title_fa="عنوان", summary_fa="خلاصه")
    assert r.status_code in (302, 303)
    with SessionLocal() as s:
        tr = s.get(NewsItem, item_id).translations
        assert tr["en"] == {"title": "EN title", "summary": "EN summary"}
        assert tr["fa"]["title"] == "عنوان"


def test_source_create_toggle_delete(client):
    from sqlalchemy import select
    token = _login(client)
    r = _post(client, "/news/sources", token, name="Reuters", url="https://r.example/rss", kind="rss")
    assert r.status_code in (302, 303)
    with SessionLocal() as s:
        src = s.scalar(select(NewsSource).where(NewsSource.url == "https://r.example/rss"))
        assert src is not None and src.enabled is True
        sid = src.id
    # duplicate URL rejected
    r = _post(client, "/news/sources", token, name="dup", url="https://r.example/rss", kind="rss")
    assert r.status_code == 400
    # toggle disables
    _post(client, f"/news/sources/{sid}", token, action="toggle")
    with SessionLocal() as s:
        assert s.get(NewsSource, sid).enabled is False
    # delete removes
    _post(client, f"/news/sources/{sid}", token, action="delete")
    with SessionLocal() as s:
        assert s.get(NewsSource, sid) is None


def test_settings_save(client):
    token = _login(client, superadmin=True)
    r = _post(client, "/news/settings", token, channel_id="-1001234567890", top_n="7", autosend="true")
    assert r.status_code in (302, 303)
    from dashboard import repo
    with SessionLocal() as s:
        cfg = repo.news_settings(s)
        assert cfg["channel_id"] == "-1001234567890"
        assert cfg["top_n"] == 7
        assert cfg["autosend"] is True


def test_settings_save_requires_superadmin(client):
    token = _login(client, superadmin=False)  # plain admin
    r = _post(client, "/news/settings", token, channel_id="-100", top_n="5", autosend="false")
    assert r.status_code == 403


def test_regen_image_requeues_for_render(client):
    token = _login(client)
    item_id = _seed_item(status="sent")
    with SessionLocal() as s:  # simulate a previously-rendered item
        it = s.get(NewsItem, item_id)
        it.image_status = "ready"
        it.rendered_image_path = "/cards/news/x.png"
        s.commit()
    r = _post(client, f"/news/{item_id}/action", token, action="regen_image")
    assert r.status_code in (302, 303)
    with SessionLocal() as s:
        it = s.get(NewsItem, item_id)
        assert it.image_status == "none" and it.rendered_image_path is None
        assert it.status == "approved"  # re-queued so render_job will rebuild it


def test_source_create_rejects_bad_scheme(client):
    token = _login(client)
    r = _post(client, "/news/sources", token, name="evil", url="javascript:alert(1)", kind="auto")
    assert r.status_code == 400
