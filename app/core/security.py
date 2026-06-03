"""Хэш/верификация API-key через argon2id (docs/05-security.md, docs/02-tech-stack.md).

В БД хранится только argon2id-хэш opaque Bearer-ключа; сам ключ не восстановим.
Сравнение — constant-time через argon2 verify.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_hasher = PasswordHasher()


def hash_api_key(plaintext_key: str) -> str:
    """argon2id-хэш opaque ключа."""
    return _hasher.hash(plaintext_key)


def verify_api_key(plaintext_key: str, stored_hash: str) -> bool:
    """Constant-time verify. False при несовпадении/повреждённом хэше."""
    try:
        return _hasher.verify(stored_hash, plaintext_key)
    except (VerifyMismatchError, ValueError, TypeError):
        return False
