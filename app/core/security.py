"""Хэш/верификация API-key через argon2id (docs/05-security.md, docs/02-tech-stack.md).

В БД хранится только argon2id-хэш opaque Bearer-ключа; сам ключ не восстановим.
Сравнение — constant-time через argon2 verify.
"""

from __future__ import annotations

import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_hasher = PasswordHasher()

# Предвычисленный валидный argon2id-хэш фиксированной случайной строки (один раз на импорт
# модуля, тем же `_hasher` → те же параметры). Назначение — устранить timing side-channel /
# user-enumeration-оракул на `/auth/login` (ADR-024 §4, docs/05-security.md: «ровно один
# argon2.verify на запрос», «неотличимость веток»): когда юзера нет или `auth_secret_hash IS
# NULL`, login делает полноценный `verify_api_key(secret, DUMMY_ARGON2_HASH)` (результат
# игнорируется), чтобы латентность не зависела от существования user_id/наличия секрета.
# Случайный плейнтекст + один хэш на старте → реальный verify не может случайно совпасть и
# не тратит ресурсы на хэширование в hot-path.
DUMMY_ARGON2_HASH = _hasher.hash(secrets.token_urlsafe(32))


def hash_api_key(plaintext_key: str) -> str:
    """argon2id-хэш opaque ключа."""
    return _hasher.hash(plaintext_key)


def verify_api_key(plaintext_key: str, stored_hash: str) -> bool:
    """Constant-time verify. False при несовпадении/повреждённом хэше."""
    try:
        return _hasher.verify(stored_hash, plaintext_key)
    except (VerifyMismatchError, ValueError, TypeError):
        return False
