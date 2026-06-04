"""Coverage for dashboard/routers/pages.py + dashboard/repo.py.

Complements tests/test_dashboard.py (which covers the login gate/flow, metrics,
users list, csrf, miniapp render, referrals, user-detail key-leak). Here we add:
  - broadcast: GET form + POST enqueues Command rows + superadmin gate + csrf
  - user moderation: POST /users/{id}/status flips User.status (+ invalid guard)
  - audit log page (filters / pagination)
  - miniapp budget / welcome / category edit + curate POSTs
  - users-list pagination / empty-state branches
  - direct unit calls into dashboard.repo helpers (sync session)

All POSTs fetch the CSRF token from the rendered form first. Polymarket.get_positions
is monkeypatched to [] so the user-detail page never hits the Data API.
"""

import re

import pytest
from starlette.testclient import TestClient

from core import crypto
from core.audit import AuditEvent
from db.engine import SessionLocal
from db.models import (
    Account, Admin, AuditLog, Category, Command, PointsLedger, Referral, User,
    UserStats, UserStatus,
)

PLAINTEXT_KEY = "0x" + "f" * 64


# ── shared helpers (mirror test_dashboard.py) ───────────────────────────────

def _csrf(client) -> str:
    """GET /login (sets the session cookie) and extract the CSRF token."""
    html = client.get("/login").text
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', html)
    assert m, "no csrf token in login form"
    return m.group(1)


def _form_csrf(client, path: str) -> str:
    """Pull the CSRF token out of an authenticated form page."""
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', client.get(path).text)
    assert m, f"no csrf token in {path}"
    return m.group(1)


def _login(client, username="dashadmin", password="s3cret!"):
    token = _csrf(client)
    return client.post("/login", data={"username": username, "password": password,
                                       "csrf_token": token})


@pytest.fixture
def seeded():
    """Superadmin + non-superadmin + a user with an encrypted account (per test)."""
    from sqlalchemy import select

    with SessionLocal() as s:
        if s.scalar(select(Admin).where(Admin.username == "dashadmin")) is None:
            s.add(Admin(username="dashadmin",
                        password_hash=crypto.hash_password("s3cret!"), is_superadmin=True))
        if s.scalar(select(Admin).where(Admin.username == "plainadmin")) is None:
            s.add(Admin(username="plainadmin",
                        password_hash=crypto.hash_password("pw123!"), is_superadmin=False))
        user = s.scalar(select(User).where(User.telegram_id == 9990001))
        if user is None:
            user = User(telegram_id=9990001, username="victim", language="en")
            s.add(user)
            s.flush()
        ciphertext = crypto.encrypt(PLAINTEXT_KEY)
        if s.scalar(select(Account).where(Account.user_id == user.id)) is None:
            s.add(Account(user_id=user.id, wallet_address="0x" + "1" * 40,
                          encrypted_private_key=ciphertext, label="Main"))
        s.commit()
        return {"user_id": user.id, "ciphertext": ciphertext}


@pytest.fixture
def client(monkeypatch):
    # avoid any real Data-API network call from the user-detail page
    monkeypatch.setattr("polymarket.client.Polymarket.get_positions", lambda self, *a, **k: [])
    from dashboard.app import app
    return TestClient(app, follow_redirects=False)


# ── broadcast ───────────────────────────────────────────────────────────────

def test_broadcast_form_renders_for_superadmin(client, seeded):
    _login(client)
    r = client.get("/broadcast")
    assert r.status_code == 200
    assert "csrf_token" in r.text  # the POST form is present


def test_broadcast_form_forbidden_for_plain_admin(client, seeded):
    _login(client, username="plainadmin", password="pw123!")
    r = client.get("/broadcast")
    assert r.status_code == 403  # require_superadmin gate


def test_broadcast_post_enqueues_command_rows(client, seeded):
    from sqlalchemy import select

    # seed two extra users in different states/languages
    with SessionLocal() as s:
        s.add(User(telegram_id=7001, username="enuser", language="en",
                   status=UserStatus.ACTIVE.value))
        s.add(User(telegram_id=7002, username="banned", language="en",
                   status=UserStatus.BANNED.value))
        s.commit()

    _login(client)
    tok = _form_csrf(client, "/broadcast")
    r = client.post("/broadcast", data={"message": "hello everyone", "csrf_token": tok})
    assert r.status_code == 200  # renders broadcast.html with sent_count

    with SessionLocal() as s:
        cmds = list(s.scalars(select(Command).where(Command.action == "BROADCAST")))
        # one Command per (all) user — victim + enuser + banned
        assert len(cmds) == 3
        assert all(c.status == "pending" for c in cmds)
        assert all(c.payload == {"message": "hello everyone"} for c in cmds)
        # audit row recorded with the count
        al = s.scalars(select(AuditLog).where(
            AuditLog.event == AuditEvent.BROADCAST_SENT.value)).first()
        assert al is not None and al.detail["count"] == 3


def test_broadcast_post_only_active_filters_targets(client, seeded):
    from sqlalchemy import select

    with SessionLocal() as s:
        s.add(User(telegram_id=7101, username="act", language="en",
                   status=UserStatus.ACTIVE.value))
        s.add(User(telegram_id=7102, username="susp", language="en",
                   status=UserStatus.SUSPENDED.value))
        s.commit()

    _login(client)
    tok = _form_csrf(client, "/broadcast")
    # only_active + language filter -> victim(active,en) + act(active,en)
    r = client.post("/broadcast",
                    data={"message": "hi", "only_active": "true", "language": "en",
                          "csrf_token": tok})
    assert r.status_code == 200
    with SessionLocal() as s:
        cmds = list(s.scalars(select(Command).where(Command.action == "BROADCAST")))
        # suspended 'susp' excluded; default victim user is active+en
        assert len(cmds) == 2


def test_broadcast_post_requires_csrf(client, seeded):
    _login(client)
    r = client.post("/broadcast", data={"message": "x"})
    assert r.status_code == 400  # missing csrf token


def test_broadcast_post_forbidden_for_plain_admin(client, seeded):
    # plain admin still has a session csrf token from /login; reuse it
    _login(client, username="plainadmin", password="pw123!")
    tok = _form_csrf(client, "/settings")  # an allowed page that carries the token
    r = client.post("/broadcast", data={"message": "x", "csrf_token": tok})
    assert r.status_code == 403


# ── user moderation: POST /users/{id}/status ────────────────────────────────

def test_user_set_status_suspend_then_activate(client, seeded):
    from sqlalchemy import select

    uid = seeded["user_id"]
    _login(client)
    tok = _form_csrf(client, f"/users/{uid}")

    r = client.post(f"/users/{uid}/status", data={"status": "suspended", "csrf_token": tok})
    assert r.status_code in (302, 303)
    assert r.headers.get("location") == f"/users/{uid}"
    with SessionLocal() as s:
        assert s.get(User, uid).status == UserStatus.SUSPENDED.value
        # an audit row was written for the suspension
        ev = s.scalars(select(AuditLog).where(
            AuditLog.event == AuditEvent.USER_SUSPENDED.value)).first()
        assert ev is not None and ev.user_id == uid

    r = client.post(f"/users/{uid}/status", data={"status": "active", "csrf_token": tok})
    assert r.status_code in (302, 303)
    with SessionLocal() as s:
        assert s.get(User, uid).status == UserStatus.ACTIVE.value


def test_user_set_status_ban(client, seeded):
    uid = seeded["user_id"]
    _login(client)
    tok = _form_csrf(client, f"/users/{uid}")
    r = client.post(f"/users/{uid}/status", data={"status": "banned", "csrf_token": tok})
    assert r.status_code in (302, 303)
    with SessionLocal() as s:
        assert s.get(User, uid).status == UserStatus.BANNED.value


def test_user_set_status_invalid_value_400(client, seeded):
    uid = seeded["user_id"]
    _login(client)
    tok = _form_csrf(client, f"/users/{uid}")
    r = client.post(f"/users/{uid}/status", data={"status": "bogus", "csrf_token": tok})
    assert r.status_code == 400


def test_user_set_status_requires_csrf(client, seeded):
    uid = seeded["user_id"]
    _login(client)
    r = client.post(f"/users/{uid}/status", data={"status": "suspended"})
    assert r.status_code == 400


# ── audit log page ──────────────────────────────────────────────────────────

def test_audit_page_renders_with_entries(client, seeded):
    # logging in itself writes ADMIN_LOGIN audit rows
    _login(client)
    r = client.get("/audit")
    assert r.status_code == 200


def test_audit_page_filters_and_pagination(client, seeded):
    _login(client)
    # event + user_id filters + page>1 (page is clamped to >=1 internally)
    r = client.get("/audit?event=admin_login&user_id=1&page=2")
    assert r.status_code == 200
    # page=0 -> clamped to 1, still renders
    r = client.get("/audit?page=0")
    assert r.status_code == 200


# ── users list pagination / empty-state ─────────────────────────────────────

def test_users_list_empty_state_and_filters(client, seeded):
    _login(client)
    # filter that matches nothing -> empty table, still 200
    r = client.get("/users?status=banned&q=nomatch&page=3")
    assert r.status_code == 200
    # numeric q -> telegram_id lookup branch
    r = client.get("/users?q=9990001")
    assert r.status_code == 200
    assert "victim" in r.text


# ── miniapp budget (superadmin) ─────────────────────────────────────────────

def test_miniapp_budget_set(client, seeded):
    import dashboard.repo as repo

    _login(client)
    tok = _form_csrf(client, "/miniapp")
    r = client.post("/miniapp/budget", data={"weekly_budget": "42.5", "csrf_token": tok})
    assert r.status_code in (302, 303)
    assert r.headers.get("location") == "/miniapp"
    with SessionLocal() as s:
        assert repo.gemini_budget(s) == pytest.approx(42.5)
        from sqlalchemy import select
        assert s.scalars(select(AuditLog).where(
            AuditLog.event == AuditEvent.GEMINI_BUDGET_SET.value)).first()


def test_miniapp_budget_negative_rejected(client, seeded):
    _login(client)
    tok = _form_csrf(client, "/miniapp")
    r = client.post("/miniapp/budget", data={"weekly_budget": "-5", "csrf_token": tok})
    assert r.status_code == 400


def test_miniapp_budget_forbidden_for_plain_admin(client, seeded):
    _login(client, username="plainadmin", password="pw123!")
    tok = _form_csrf(client, "/miniapp")
    r = client.post("/miniapp/budget", data={"weekly_budget": "1", "csrf_token": tok})
    assert r.status_code == 403  # require_superadmin


# ── miniapp welcome prompt / upload ─────────────────────────────────────────

def test_miniapp_welcome_save_prompt(client, seeded):
    from core.gemini import WELCOME_PROMPT_KEY
    from db.repositories import appconfig

    _login(client)
    tok = _form_csrf(client, "/miniapp")
    r = client.post("/miniapp/welcome",
                    data={"prompt": "  a bold hero banner  ", "csrf_token": tok})
    assert r.status_code in (302, 303)
    assert r.headers.get("location") == "/miniapp"
    with SessionLocal() as s:
        # stored stripped
        assert appconfig.get_sync(s, WELCOME_PROMPT_KEY) == "a bold hero banner"


def test_miniapp_welcome_upload_rejects_non_image(client, seeded):
    _login(client)
    tok = _form_csrf(client, "/miniapp")
    r = client.post("/miniapp/welcome/upload",
                    files={"image": ("x.txt", b"not an image at all", "text/plain")},
                    data={"csrf_token": tok})
    assert r.status_code == 400


def test_miniapp_welcome_upload_accepts_png(client, seeded, monkeypatch, tmp_path):
    # redirect the cards dir so we don't pollute the real data/cards folder
    monkeypatch.setattr("core.gemini.cards_dir", lambda: tmp_path)
    from core.gemini import WELCOME_PATH_KEY, WELCOME_SLUG
    from db.repositories import appconfig

    _login(client)
    tok = _form_csrf(client, "/miniapp")
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    r = client.post("/miniapp/welcome/upload",
                    files={"image": ("hero.png", png, "image/png")},
                    data={"csrf_token": tok})
    assert r.status_code in (302, 303)
    assert (tmp_path / f"{WELCOME_SLUG}.png").read_bytes() == png
    with SessionLocal() as s:
        assert appconfig.get_sync(s, WELCOME_PATH_KEY) == f"/cards/{WELCOME_SLUG}.png"


# ── miniapp category edit / curate ──────────────────────────────────────────

@pytest.fixture
def category():
    with SessionLocal() as s:
        cat = Category(slug="sports", title="Sports", volume=100)
        s.add(cat)
        s.commit()
        return cat.id


def test_category_edit_page_renders(client, seeded, category):
    _login(client)
    r = client.get(f"/miniapp/categories/{category}")
    assert r.status_code == 200


def test_category_edit_page_404(client, seeded):
    _login(client)
    r = client.get("/miniapp/categories/999999")
    assert r.status_code == 404


def test_category_save_updates_fields(client, seeded, category):
    _login(client)
    tok = _form_csrf(client, f"/miniapp/categories/{category}")
    r = client.post(f"/miniapp/categories/{category}/edit",
                    data={"title": "New Title", "prompt_override": "custom prompt",
                          "csrf_token": tok})
    assert r.status_code in (302, 303)
    with SessionLocal() as s:
        cat = s.get(Category, category)
        assert cat.title == "New Title"
        assert cat.prompt_override == "custom prompt"


def test_category_save_404_for_missing(client, seeded):
    _login(client)
    tok = _form_csrf(client, "/miniapp")
    r = client.post("/miniapp/categories/999999/edit",
                    data={"title": "x", "csrf_token": tok})
    assert r.status_code == 404


def test_category_curate_pin_and_hide(client, seeded, category):
    _login(client)
    tok = _form_csrf(client, f"/miniapp/categories/{category}")
    r = client.post(f"/miniapp/categories/{category}",
                    data={"action": "pin", "csrf_token": tok})
    assert r.status_code in (302, 303)
    assert r.headers.get("location") == "/miniapp"
    with SessionLocal() as s:
        assert s.get(Category, category).pinned is True

    r = client.post(f"/miniapp/categories/{category}",
                    data={"action": "hide", "csrf_token": tok})
    assert r.status_code in (302, 303)
    with SessionLocal() as s:
        assert s.get(Category, category).hidden is True


def test_category_curate_invalid_action_400(client, seeded, category):
    _login(client)
    tok = _form_csrf(client, f"/miniapp/categories/{category}")
    r = client.post(f"/miniapp/categories/{category}",
                    data={"action": "explode", "csrf_token": tok})
    assert r.status_code == 400


def test_category_image_upload_rejects_non_image(client, seeded, category):
    _login(client)
    tok = _form_csrf(client, f"/miniapp/categories/{category}")
    r = client.post(f"/miniapp/categories/{category}/upload",
                    files={"image": ("x.txt", b"garbage", "text/plain")},
                    data={"csrf_token": tok})
    assert r.status_code == 400


def test_category_image_upload_accepts_jpeg(client, seeded, category, monkeypatch, tmp_path):
    monkeypatch.setattr("core.gemini.cards_dir", lambda: tmp_path)
    _login(client)
    tok = _form_csrf(client, f"/miniapp/categories/{category}")
    jpeg = b"\xff\xd8\xff" + b"\x00" * 16
    r = client.post(f"/miniapp/categories/{category}/upload",
                    files={"image": ("c.jpg", jpeg, "image/jpeg")},
                    data={"csrf_token": tok})
    assert r.status_code in (302, 303)
    with SessionLocal() as s:
        cat = s.get(Category, category)
        assert cat.image_status == "ready"
        assert cat.image_path == "/cards/sports.png"
    assert (tmp_path / "sports.png").read_bytes() == jpeg


# ── settings page ───────────────────────────────────────────────────────────

def test_settings_page_renders(client, seeded):
    _login(client)
    r = client.get("/settings")
    assert r.status_code == 200


def test_index_redirects_to_metrics(client, seeded):
    _login(client)
    r = client.get("/")
    assert r.status_code in (302, 303)
    assert r.headers.get("location") == "/metrics"


# ── no endpoint leaks key material on account-bearing pages ─────────────────

def test_user_detail_no_key_material(client, seeded):
    _login(client)
    body = client.get(f"/users/{seeded['user_id']}").text
    assert PLAINTEXT_KEY not in body
    assert seeded["ciphertext"] not in body
    assert "f" * 64 not in body
    assert "0x" + "1" * 40 in body  # public wallet ok


# ════════════════════════════════════════════════════════════════════════════
# Direct unit calls into dashboard.repo helpers (sync session).
# ════════════════════════════════════════════════════════════════════════════

def _seed_repo_graph(s):
    """inviter -> invitee referral graph with stats/points/account/audit."""
    from datetime import datetime, timezone

    inviter = User(telegram_id=10, username="inviter", language="en", referral_code="CODE1")
    s.add(inviter)
    s.flush()
    invitee = User(telegram_id=11, username="invitee", language="en",
                   status=UserStatus.SUSPENDED.value, referred_by=inviter.id)
    s.add(invitee)
    s.flush()
    s.add(Account(user_id=inviter.id, wallet_address="0x" + "a" * 40,
                  encrypted_private_key="cipher", label="Main"))
    s.add(Referral(inviter_id=inviter.id, invitee_id=invitee.id, status="unlocked"))
    s.add(UserStats(user_id=invitee.id, total_bets=5))
    s.add(PointsLedger(user_id=inviter.id, delta=100, reason="referral"))
    s.add(AuditLog(actor_type="admin", event="admin_login",
                   ts=datetime.now(timezone.utc)))
    s.commit()
    return inviter.id, invitee.id


def test_repo_metrics_summary():
    import dashboard.repo as repo

    with SessionLocal() as s:
        _seed_repo_graph(s)
        m = repo.metrics_summary(s)
    assert m["total_users"] == 2
    assert m["suspended_users"] == 1
    assert m["active_users"] == 1
    assert m["total_accounts"] == 1
    assert m["trades_today"] == 0


def test_repo_user_listing_and_counts():
    import dashboard.repo as repo

    with SessionLocal() as s:
        inviter_id, invitee_id = _seed_repo_graph(s)
        assert repo.count_users(s) == 2
        assert repo.count_users(s, status="suspended") == 1
        assert repo.count_users(s, q="inviter") == 1
        assert repo.count_users(s, q="11") == 1  # numeric -> telegram_id lookup
        listed = repo.list_users(s, q="inviter")
        assert listed[0]["username"] == "inviter"
        assert listed[0]["account_count"] == 1
        # negative/odd numeric q still routes via isdigit branch
        assert repo.count_users(s, q="-5") == 0
        # get_user happy + missing
        assert repo.get_user(s, inviter_id).id == inviter_id
        assert repo.get_user(s, 999999) is None


def test_repo_set_user_status_guards():
    import dashboard.repo as repo

    with SessionLocal() as s:
        inviter_id, _ = _seed_repo_graph(s)
        assert repo.set_user_status(s, inviter_id, "not-a-status") is False
        assert repo.set_user_status(s, 999999, "active") is False
        assert repo.set_user_status(s, inviter_id, "banned") is True
        s.commit()
        assert s.get(User, inviter_id).status == UserStatus.BANNED.value


def test_repo_user_detail_and_none():
    import dashboard.repo as repo

    with SessionLocal() as s:
        inviter_id, _ = _seed_repo_graph(s)
        d = repo.user_detail(s, inviter_id)
        assert d["user"]["username"] == "inviter"
        assert len(d["accounts"]) == 1
        # account dict must NOT carry any key material
        acc = d["accounts"][0]
        assert "encrypted_private_key" not in acc
        assert "encrypted_api_creds" not in acc
        assert acc["wallet_address"] == "0x" + "a" * 40
        assert repo.user_detail(s, 999999) is None


def test_repo_audit_listing_and_filter():
    import dashboard.repo as repo

    with SessionLocal() as s:
        _seed_repo_graph(s)
        assert len(repo.list_audit(s)) == 1
        assert len(repo.list_audit(s, event="admin_login")) == 1
        assert len(repo.list_audit(s, event="nonexistent")) == 0
        assert len(repo.list_audit(s, user_id=999)) == 0


def test_repo_rewards_and_referrals():
    import dashboard.repo as repo

    with SessionLocal() as s:
        inviter_id, invitee_id = _seed_repo_graph(s)

        rw_inviter = repo.user_rewards(s, inviter_id)
        assert rw_inviter["referral_code"] == "CODE1"
        assert rw_inviter["points"] == 100
        assert rw_inviter["direct"] == 1
        assert rw_inviter["unlocked"] == 1
        assert rw_inviter["inviter"] is None  # inviter has no upstream inviter

        rw_invitee = repo.user_rewards(s, invitee_id)
        assert rw_invitee["inviter"]["username"] == "inviter"

        ov = repo.referral_overview(s)
        assert ov["edges"] == 1 and ov["unlocked"] == 1 and ov["pending"] == 0
        assert ov["with_code"] == 1
        assert ov["total_points"] == 100

        leaders = repo.top_referrers(s)
        assert leaders[0]["user_id"] == inviter_id
        assert leaders[0]["direct"] == 1 and leaders[0]["unlocked"] == 1

        edges = repo.referral_edges(s)
        assert edges[0]["inviter_username"] == "inviter"
        assert edges[0]["invitee_username"] == "invitee"
        assert edges[0]["bets"] == 5

        referees = repo.user_referees(s, inviter_id)
        assert referees[0]["username"] == "invitee"
        assert referees[0]["bets"] == 5
        assert referees[0]["status"] == "unlocked"

        assert repo.wallet_addresses_for_user(s, inviter_id) == ["0x" + "a" * 40]


def test_repo_category_helpers():
    import dashboard.repo as repo

    with SessionLocal() as s:
        cat = Category(slug="crypto", title="Crypto", volume=50)
        s.add(cat)
        s.commit()
        cid = cat.id

        # list returns the category
        assert any(c.id == cid for c in repo.list_categories(s))
        assert repo.get_category(s, cid).slug == "crypto"
        assert repo.get_category(s, 999999) is None

        # update happy path (title stripped, prompt set, regenerate clears image)
        assert repo.update_category(s, cid, title="  Crypto2 ",
                                    prompt_override="  p  ", regenerate=True) is True
        s.commit()
        cat2 = s.get(Category, cid)
        assert cat2.title == "Crypto2"
        assert cat2.prompt_override == "p"
        assert cat2.image_status == "none" and cat2.image_path is None
        # update on missing id
        assert repo.update_category(s, 999999, title="x") is False

        # curate actions
        for action, attr, expected in (("pin", "pinned", True), ("unpin", "pinned", False),
                                       ("hide", "hidden", True), ("unhide", "hidden", False)):
            assert repo.curate_category(s, cid, action) is True
            s.commit()
            assert getattr(s.get(Category, cid), attr) is expected
        # regen resets image fields
        s.get(Category, cid).image_status = "ready"
        s.commit()
        assert repo.curate_category(s, cid, "regen") is True
        s.commit()
        assert s.get(Category, cid).image_status == "none"
        # bad action / missing id
        assert repo.curate_category(s, cid, "nope") is False
        assert repo.curate_category(s, 999999, "pin") is False


def test_repo_gemini_budget_and_stats():
    import dashboard.repo as repo

    with SessionLocal() as s:
        repo.set_gemini_budget(s, 33.0)
        s.commit()
        assert repo.gemini_budget(s) == pytest.approx(33.0)
        stats = repo.gemini_stats(s)
        # structure assertions (configured reflects GEMINI_API_KEY="" in tests)
        assert set(stats) == {"budget", "spent", "images_this_week", "configured"}
        assert stats["budget"] == pytest.approx(33.0)
        assert stats["spent"] == 0.0
        assert stats["images_this_week"] == 0


def test_repo_welcome_banner_and_prompt(monkeypatch, tmp_path):
    import dashboard.repo as repo
    from core.gemini import WELCOME_PATH_KEY, WELCOME_PROMPT_KEY, WELCOME_SLUG
    from db.repositories import appconfig

    # isolate the cards dir so writes/unlinks don't touch real data/cards
    monkeypatch.setattr("core.gemini.cards_dir", lambda: tmp_path)

    with SessionLocal() as s:
        # before anything: no file, empty prompt, default present
        wb = repo.welcome_banner(s)
        assert wb["exists"] is False
        assert wb["prompt"] == ""
        assert wb["default_prompt"]

        # set prompt (stripped)
        repo.set_welcome_prompt(s, "  glossy banner  ")
        s.commit()
        assert appconfig.get_sync(s, WELCOME_PROMPT_KEY) == "glossy banner"

        # save an image -> file exists + path config set
        repo.save_welcome_image(s, b"\x89PNG\r\n\x1a\n123")
        s.commit()
        assert (tmp_path / f"{WELCOME_SLUG}.png").exists()
        assert appconfig.get_sync(s, WELCOME_PATH_KEY) == f"/cards/{WELCOME_SLUG}.png"
        wb2 = repo.welcome_banner(s)
        assert wb2["exists"] is True
        assert wb2["prompt"] == "glossy banner"

        # set_welcome_prompt with regenerate -> removes the file + blanks the path
        repo.set_welcome_prompt(s, "new", regenerate=True)
        s.commit()
        assert not (tmp_path / f"{WELCOME_SLUG}.png").exists()
        assert appconfig.get_sync(s, WELCOME_PATH_KEY) == ""
