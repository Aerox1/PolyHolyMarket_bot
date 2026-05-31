import pytest

from core import crypto


def test_encrypt_decrypt_roundtrip():
    secret = "0x" + "ab" * 32  # a plausible private key
    ct = crypto.encrypt(secret)
    assert ct != secret
    assert "ab" * 32 not in ct  # plaintext not present in ciphertext
    assert crypto.decrypt(ct) == secret


def test_decrypt_garbage_raises():
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt("not-a-valid-fernet-token")


def test_ciphertext_is_nondeterministic():
    # Fernet embeds a random IV + timestamp, so two encryptions differ.
    assert crypto.encrypt("same") != crypto.encrypt("same")


def test_password_hash_and_verify():
    h = crypto.hash_password("hunter2")
    assert h != "hunter2"
    assert crypto.verify_password(h, "hunter2")
    assert not crypto.verify_password(h, "wrong")


def test_verify_bad_hash_returns_false():
    assert not crypto.verify_password("garbage", "anything")
