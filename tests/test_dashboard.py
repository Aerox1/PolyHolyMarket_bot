"""End-to-end dashboard tests via Starlette TestClient.

Verifies the auth gate, the login flow, and — most importantly — that NO
endpoint leaks wallet key material (plaintext or ciphertext).
"""

import re

import pytest
from starlette.testclient import TestClient

from core import crypto
from db.engine import SessionLocal
from db.models import Account, Admin, User

PLAINTEXT_KEY = "0x" + "f" * 64


def _csrf(client) -> str:
    """GET /login (sets the session cookie) and extract the CSRF token."""
    html = client.get("/login").text
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', html)
    assert m, "no csrf token in login form"
    return m.group(1)


def _login(client, password: str):
    token = _csrf(client)
    return client.post("/login", data={"username": "dashadmin", "password": password, "csrf_token": token})


@pytest.fixture
def seeded():
    """Create an admin + a user with an encrypted account (per test)."""
    from sqlalchemy import select

    with SessionLocal() as s:
        admin = s.scalar(select(Admin).where(Admin.username == "dashadmin"))
        if admin is None:
            s.add(Admin(username="dashadmin", password_hash=crypto.hash_password("s3cret!"), is_superadmin=True))
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


def test_protected_route_redirects_to_login(client):
    r = client.get("/metrics")
    assert r.status_code in (302, 303)
    assert "/login" in r.headers.get("location", "")


def test_login_page_renders(client):
    r = client.get("/login")
    assert r.status_code == 200
    assert "login" in r.text.lower() or "<form" in r.text.lower()


def test_login_flow_and_metrics(client, seeded):
    bad = _login(client, "wrong")
    assert bad.status_code == 401

    ok = _login(client, "s3cret!")
    assert ok.status_code in (302, 303)

    r = client.get("/metrics")
    assert r.status_code == 200

    r = client.get("/users")
    assert r.status_code == 200
    assert "victim" in r.text  # the seeded user shows up


def test_login_rejected_without_csrf(client, seeded):
    # No csrf token -> 400 (CSRF protection active)
    r = client.post("/login", data={"username": "dashadmin", "password": "s3cret!"})
    assert r.status_code == 400


def test_miniapp_admin_page(client, seeded):
    _login(client, "s3cret!")
    r = client.get("/miniapp")
    assert r.status_code == 200
    assert "Gemini" in r.text or "budget" in r.text.lower()


def test_referrals_page_renders(client, seeded):
    _login(client, "s3cret!")
    r = client.get("/referrals")
    assert r.status_code == 200
    assert "referr" in r.text.lower()  # leaderboard / overview rendered


def test_referrals_csv_export(client, seeded):
    _login(client, "s3cret!")
    r = client.get("/referrals/export.csv")
    assert r.status_code == 200
    assert "text/csv" in r.headers.get("content-type", "")
    assert "inviter_id,inviter_username" in r.text  # header row present


def test_user_detail_lists_referees(client, seeded):
    from sqlalchemy import select

    from db.models import Referral, User
    inviter_id = seeded["user_id"]
    with SessionLocal() as s:
        invitee = s.scalar(select(User).where(User.telegram_id == 9990002))
        if invitee is None:
            invitee = User(telegram_id=9990002, username="referee1", language="en", referred_by=inviter_id)
            s.add(invitee)
            s.flush()
        if s.scalar(select(Referral).where(Referral.invitee_id == invitee.id)) is None:
            s.add(Referral(inviter_id=inviter_id, invitee_id=invitee.id, status="pending"))
        s.commit()
    _login(client, "s3cret!")
    r = client.get(f"/users/{inviter_id}")
    assert r.status_code == 200
    assert "referee1" in r.text  # the referee appears in the Referees card


def test_user_detail_never_leaks_key_material(client, seeded):
    _login(client, "s3cret!")
    r = client.get(f"/users/{seeded['user_id']}")
    assert r.status_code == 200
    body = r.text
    # neither the plaintext private key nor its ciphertext may ever appear
    assert PLAINTEXT_KEY not in body
    assert seeded["ciphertext"] not in body
    assert "f" * 64 not in body
    # but the public wallet address is fine to show
    assert "0x" + "1" * 40 in body
