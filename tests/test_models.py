import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from core import crypto
from db.models import Account, User, UserSettings


def _make_user(session, telegram_id=1001, **kw):
    u = User(telegram_id=telegram_id, username="u", language="en", **kw)
    session.add(u)
    session.flush()
    return u


def test_create_user_defaults(session):
    u = _make_user(session, telegram_id=2001)
    assert u.id is not None
    assert u.status == "active"
    assert u.is_admin is False


def test_account_stores_ciphertext_only(session):
    u = _make_user(session, telegram_id=2002)
    ct = crypto.encrypt("0xPRIVATEKEY")
    a = Account(user_id=u.id, wallet_address="0xWALLET", encrypted_private_key=ct, label="Main")
    session.add(a)
    session.flush()
    assert "PRIVATEKEY" not in a.encrypted_private_key
    assert crypto.decrypt(a.encrypted_private_key) == "0xPRIVATEKEY"
    assert a.mode == "live"
    assert a.signature_type == 0


def test_unique_account_label_per_user(session):
    u = _make_user(session, telegram_id=2003)
    ct = crypto.encrypt("k")
    session.add(Account(user_id=u.id, wallet_address="0xA", encrypted_private_key=ct, label="Main"))
    session.flush()
    session.add(Account(user_id=u.id, wallet_address="0xB", encrypted_private_key=ct, label="Main"))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_invalid_status_rejected_by_check_constraint(session):
    with pytest.raises(IntegrityError):
        session.add(User(telegram_id=2004, status="bogus"))
        session.flush()
    session.rollback()


def test_cascade_delete_user_removes_accounts(session):
    u = _make_user(session, telegram_id=2005)
    ct = crypto.encrypt("k")
    session.add(Account(user_id=u.id, wallet_address="0xA", encrypted_private_key=ct, label="Main"))
    session.add(UserSettings(user_id=u.id))
    session.flush()
    uid = u.id
    session.delete(u)
    session.flush()
    assert session.scalar(select(Account).where(Account.user_id == uid)) is None
    assert session.scalar(select(UserSettings).where(UserSettings.user_id == uid)) is None
