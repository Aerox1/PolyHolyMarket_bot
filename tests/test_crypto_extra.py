"""Extra coverage for core.crypto beyond the basic round-trip in test_crypto.py.

WARNING: core.crypto._fernet is a lazy module-global MultiFernet cache built by
_vault(). Any test that changes settings.encryption_keys MUST reset the cache
before AND after, or it leaks into other tests. The reset_fernet fixture does
this with try/finally.
"""

import pytest

from core import crypto
from core.config import settings
from cryptography.fernet import Fernet


@pytest.fixture
def reset_fernet():
    """Clear the lazy MultiFernet cache around a test that mutates keys."""
    crypto._fernet = None
    try:
        yield
    finally:
        crypto._fernet = None


def _patch_keys(monkeypatch, keys):
    """Monkeypatch settings.encryption_keys (a property) to a fixed list."""
    # settings is an instance; encryption_keys is a property on its class.
    monkeypatch.setattr(type(settings), "encryption_keys", property(lambda self: keys))


# ── rotate ────────────────────────────────────────────────────────────────────

def test_rotate_preserves_plaintext():
    ct = crypto.encrypt("rotate-me")
    rotated = crypto.rotate(ct)
    # Rotation re-wraps under the (single) primary key; payload still decrypts.
    assert crypto.decrypt(rotated) == "rotate-me"
    assert "rotate-me" not in rotated


# ── verify_password ─────────────────────────────────────────────────────────--

def test_verify_password_correct():
    h = crypto.hash_password("s3cret-pw")
    assert crypto.verify_password(h, "s3cret-pw") is True


def test_verify_password_wrong():
    h = crypto.hash_password("s3cret-pw")
    assert crypto.verify_password(h, "nope") is False


def test_verify_password_non_argon2_hash():
    # A non-argon2 string triggers InvalidHashError internally -> False, no raise.
    assert crypto.verify_password("$totally$not$argon2$", "anything") is False


# ── needs_rehash ────────────────────────────────────────────────────────────--

def test_needs_rehash_real_hash_returns_bool():
    h = crypto.hash_password("pw")
    res = crypto.needs_rehash(h)
    assert isinstance(res, bool)
    # A freshly-made hash with default params should not need a rehash.
    assert res is False


def test_needs_rehash_garbage_returns_false():
    # Garbage hash must never raise; the except-Exception guard returns False.
    assert crypto.needs_rehash("not-a-hash") is False


# ── encryption_available ──────────────────────────────────────────────────────

def test_encryption_available_with_configured_key():
    assert crypto.encryption_available() is True


# ── EncryptionUnavailable: no keys ────────────────────────────────────────────

def test_encrypt_without_keys_raises(monkeypatch, reset_fernet):
    _patch_keys(monkeypatch, [])
    with pytest.raises(crypto.EncryptionUnavailable):
        crypto.encrypt("x")


def test_encryption_available_false_without_keys(monkeypatch, reset_fernet):
    _patch_keys(monkeypatch, [])
    assert crypto.encryption_available() is False


# ── EncryptionUnavailable: malformed key ──────────────────────────────────────

def test_malformed_key_raises_encryption_unavailable(monkeypatch, reset_fernet):
    _patch_keys(monkeypatch, ["not-a-valid-fernet-key"])
    with pytest.raises(crypto.EncryptionUnavailable):
        crypto.encrypt("x")


def test_malformed_key_vault_raises(monkeypatch, reset_fernet):
    _patch_keys(monkeypatch, ["not-a-valid-fernet-key"])
    with pytest.raises(crypto.EncryptionUnavailable):
        crypto._vault()


# ── DecryptionError ───────────────────────────────────────────────────────────

def test_decrypt_garbage_message_omits_token():
    token = "this-is-not-a-real-token"
    with pytest.raises(crypto.DecryptionError) as ei:
        crypto.decrypt(token)
    # The error message must never leak the (possibly sensitive) token.
    assert token not in str(ei.value)


# ── CLI _main ────────────────────────────────────────────────────────────────

def test_main_gen_key_prints_parseable_key(capsys):
    rc = crypto._main(["core.crypto", "gen-key"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    # Output must be a usable Fernet key (constructor validates length/base64).
    Fernet(out.encode())


def test_main_hash_password_prints_hash(capsys):
    rc = crypto._main(["core.crypto", "hash-password", "pw"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    # Printed value is a real argon2 hash that verifies.
    assert crypto.verify_password(out, "pw") is True


def test_main_hash_password_missing_arg():
    assert crypto._main(["core.crypto", "hash-password"]) == 1


def test_main_no_command_prints_doc(capsys):
    rc = crypto._main(["core.crypto"])
    assert rc == 1
    out = capsys.readouterr().out
    # Falls back to printing the module docstring.
    assert "gen-key" in out


def test_main_unknown_command():
    assert crypto._main(["core.crypto", "bogus"]) == 1
