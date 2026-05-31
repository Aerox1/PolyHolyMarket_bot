"""Encryption at rest for wallet private keys + admin password hashing.

Security model
--------------
* Wallet private keys / API creds are encrypted with **Fernet** (AES-128-CBC +
  HMAC) using the master key from ``ENCRYPTION_KEY``. Rotation is supported via
  ``MultiFernet`` (current key encrypts; any listed key can decrypt).
* Encryption/decryption is performed **explicitly** in the credential/account
  repository layer — NOT via a SQLAlchemy TypeDecorator. This guarantees the
  dashboard process (which runs WITHOUT ``ENCRYPTION_KEY``) physically cannot
  decrypt key material even if it loads an Account row.
* The plaintext key is never logged. Callers must never put it in exceptions or
  log lines.

CLI
---
    python -m core.crypto gen-key                 # new Fernet master key
    python -m core.crypto hash-password 'secret'  # argon2id hash for an admin
"""

from __future__ import annotations

import sys

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from core.config import settings


class EncryptionUnavailable(RuntimeError):
    """Raised when encryption is attempted without ENCRYPTION_KEY configured.

    By design this happens in the dashboard process — it should never need to
    encrypt or decrypt key material.
    """


class DecryptionError(RuntimeError):
    """Raised when a ciphertext cannot be decrypted with any configured key."""


_fernet: MultiFernet | None = None


def _vault() -> MultiFernet:
    global _fernet
    if _fernet is None:
        keys = settings.encryption_keys
        if not keys:
            raise EncryptionUnavailable(
                "ENCRYPTION_KEY is not set. This process cannot encrypt/decrypt "
                "wallet keys (expected for the dashboard process)."
            )
        try:
            _fernet = MultiFernet([Fernet(k.encode() if isinstance(k, str) else k) for k in keys])
        except (ValueError, TypeError) as exc:  # malformed key
            raise EncryptionUnavailable(f"Invalid ENCRYPTION_KEY format: {type(exc).__name__}") from exc
    return _fernet


def encryption_available() -> bool:
    """True if this process can encrypt/decrypt (has a valid master key)."""
    try:
        _vault()
        return True
    except EncryptionUnavailable:
        return False


def encrypt(plaintext: str) -> str:
    """Encrypt a secret string. Returns urlsafe-base64 ciphertext (str)."""
    return _vault().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    """Decrypt ciphertext produced by :func:`encrypt`."""
    try:
        return _vault().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        # Never include the token or any key material in the message.
        raise DecryptionError("Could not decrypt value with configured key(s).") from exc


def rotate(token: str) -> str:
    """Re-encrypt a ciphertext under the current (primary) key."""
    return _vault().rotate(token.encode("ascii")).decode("ascii")


# ── Admin password hashing (argon2id) ────────────────────────────────────────

def _password_hasher():
    from argon2 import PasswordHasher

    return PasswordHasher()


def hash_password(password: str) -> str:
    return _password_hasher().hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError

    try:
        return _password_hasher().verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    try:
        return _password_hasher().check_needs_rehash(stored_hash)
    except Exception:
        return False


# ── CLI ──────────────────────────────────────────────────────────────────────

def _main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 1
    cmd = argv[1]
    if cmd == "gen-key":
        print(Fernet.generate_key().decode())
        return 0
    if cmd == "hash-password":
        if len(argv) < 3:
            print("usage: python -m core.crypto hash-password '<password>'", file=sys.stderr)
            return 1
        print(hash_password(argv[2]))
        return 0
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
