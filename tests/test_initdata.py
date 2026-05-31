"""Telegram Mini App initData validation — the auth crux. We build a correctly
signed initData with the test bot token and confirm accept/reject behavior."""

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest

from webapp import initdata
from webapp.initdata import InitDataError, validate

TOKEN = "test-token"  # matches conftest TELEGRAM_BOT_TOKEN


def _sign(fields: dict) -> str:
    dcs = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    h = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": h})


def _valid(auth_date: int | None = None) -> str:
    return _sign({
        "user": json.dumps({"id": 777, "username": "sam", "first_name": "Sam", "language_code": "fa"}),
        "auth_date": str(auth_date if auth_date is not None else int(time.time())),
        "query_id": "AAA",
    })


def test_valid_initdata_accepted():
    u = validate(_valid())
    assert u.id == 777 and u.username == "sam" and u.language_code == "fa"


def test_tampered_hash_rejected():
    raw = _valid()
    # flip the last hex char of the hash
    bad = raw[:-1] + ("0" if raw[-1] != "0" else "1")
    with pytest.raises(InitDataError):
        validate(bad)


def test_tampered_user_rejected():
    raw = _valid()
    with pytest.raises(InitDataError):
        validate(raw.replace("777", "888"))  # changes signed data, hash no longer matches


def test_expired_initdata_rejected():
    old = int(time.time()) - 10_000
    with pytest.raises(InitDataError):
        validate(_valid(auth_date=old), max_age_seconds=3600)


def test_missing_initdata_rejected():
    with pytest.raises(InitDataError):
        validate("")
