"""Unit: сервис аутентификации по user_id + секрет (ADR-024 §1/§2/§5).

docs/modules/auth/03-architecture.md §1A, docs/modules/auth/02-api-contracts.md,
docs/adr/ADR-024-user-id-secret-authentication.md, docs/06-testing-strategy §Sprint 3.

Граница unit: реальный Postgres (через session-фикстуру), Redis НЕ задействован (сервис
лишь Postgres + argon2). Покрывает constant-time-инвариант «ровно один verify в каждой ветке
провала login» (включая несуществующий user_id — verify против DUMMY_ARGON2_HASH) — мокая
verify_api_key в точке вызова сервиса (app.services.auth_service).
"""

from __future__ import annotations

import pytest

from app.core.security import DUMMY_ARGON2_HASH, verify_api_key
from app.services.auth_service import (
    SecretAuthError,
    login_with_secret,
    register_with_secret,
    set_or_rotate_secret,
)

pytestmark = pytest.mark.asyncio


# --- register: server-generated user_id + secret, auth_secret_hash непустой ---


async def test_register_creates_user_with_nonnull_secret_hash_and_null_apple_sub(session):
    res = await register_with_secret(session, device_label="iPhone 15")
    assert res.user_id.startswith("u_")
    assert res.api_key.startswith("lv_")
    assert res.token_id.startswith("t_")
    assert res.secret  # секрет показан один раз

    from app.db.models import User

    user = await session.get(User, res.user_id)
    assert user is not None
    assert user.apple_sub is None  # admin-created-подобный (ADR-021 §B)
    assert user.auth_secret_hash is not None  # НЕПУСТОЙ хэш
    assert user.adapty_customer_user_id == user.id
    # Хэш реально верифицирует выданный секрет (argon2id), а не плейнтекст.
    assert verify_api_key(res.secret, user.auth_secret_hash) is True
    assert res.secret != user.auth_secret_hash  # хранится хэш, не секрет


async def test_register_secret_not_recoverable_from_hash(session):
    res = await register_with_secret(session, device_label=None)
    from app.db.models import User

    user = await session.get(User, res.user_id)
    # Хэш argon2id-формата (не равен секрету; начинается с $argon2).
    assert user.auth_secret_hash.startswith("$argon2")


# --- login round-trip: верный секрет → новый api_token, старые не тронуты ---


async def test_login_roundtrip_issues_new_token(session):
    reg = await register_with_secret(session, device_label="iPhone")
    login = await login_with_secret(
        session, user_id=reg.user_id, secret=reg.secret, device_label="iPad"
    )
    assert login.user_id == reg.user_id
    assert login.api_key.startswith("lv_")
    assert login.token_id != reg.token_id  # НОВЫЙ токен (мульти-устройство)

    from sqlalchemy import func, select

    from app.db.models import ApiToken

    count = await session.scalar(
        select(func.count()).select_from(ApiToken).where(ApiToken.user_id == reg.user_id)
    )
    assert count == 2  # register + login → две независимые строки (старая не отозвана)


# --- единый провал: три ветки → SecretAuthError (роутер транслирует в единый 401) ---


async def test_login_nonexistent_user_raises(session):
    with pytest.raises(SecretAuthError):
        await login_with_secret(
            session, user_id="u_nonexistent0000000000", secret="whatever", device_label=None
        )


async def test_login_user_with_null_secret_hash_raises(session, seeded_user):
    # seeded_user (S1) имеет auth_secret_hash IS NULL → провал даже с любым секретом.
    assert seeded_user.auth_secret_hash is None
    with pytest.raises(SecretAuthError):
        await login_with_secret(
            session, user_id=seeded_user.id, secret="whatever", device_label=None
        )


async def test_login_wrong_secret_raises(session):
    reg = await register_with_secret(session, device_label=None)
    with pytest.raises(SecretAuthError):
        await login_with_secret(
            session, user_id=reg.user_id, secret="wrong-secret", device_label=None
        )


# --- constant-time: РОВНО ОДИН verify в каждой ветке провала (timing-неотличимость) ---


@pytest.fixture
def spy_verify(monkeypatch):
    """Считает вызовы verify_api_key в точке использования сервисом и фиксирует stored_hash.

    auth_service делает `from app.core.security import verify_api_key` → патчим символ в
    модуле сервиса (app.services.auth_service), а не источник.
    """
    calls: list[tuple[str, str]] = []
    import app.services.auth_service as svc

    real = svc.verify_api_key

    def _spy(plaintext: str, stored_hash: str) -> bool:
        calls.append((plaintext, stored_hash))
        return real(plaintext, stored_hash)

    monkeypatch.setattr(svc, "verify_api_key", _spy)
    return calls


async def test_login_nonexistent_user_calls_verify_exactly_once_against_dummy(session, spy_verify):
    with pytest.raises(SecretAuthError):
        await login_with_secret(
            session, user_id="u_ghost00000000000000000", secret="s", device_label=None
        )
    assert len(spy_verify) == 1  # ровно один verify (timing-неотличимость)
    assert spy_verify[0][1] == DUMMY_ARGON2_HASH  # против DUMMY (нет реального хэша)


async def test_login_null_secret_hash_calls_verify_exactly_once_against_dummy(
    session, seeded_user, spy_verify
):
    with pytest.raises(SecretAuthError):
        await login_with_secret(session, user_id=seeded_user.id, secret="s", device_label=None)
    assert len(spy_verify) == 1
    assert spy_verify[0][1] == DUMMY_ARGON2_HASH  # auth_secret_hash IS NULL → DUMMY


async def test_login_wrong_secret_calls_verify_exactly_once_against_real_hash(session, spy_verify):
    reg = await register_with_secret(session, device_label=None)
    spy_verify.clear()  # сбрасываем (register сам verify не делает, но на всякий случай)
    with pytest.raises(SecretAuthError):
        await login_with_secret(session, user_id=reg.user_id, secret="wrong", device_label=None)
    assert len(spy_verify) == 1  # ровно один verify
    assert spy_verify[0][1] != DUMMY_ARGON2_HASH  # против РЕАЛЬНОГО auth_secret_hash


async def test_login_success_calls_verify_exactly_once(session, spy_verify):
    reg = await register_with_secret(session, device_label=None)
    spy_verify.clear()
    await login_with_secret(session, user_id=reg.user_id, secret=reg.secret, device_label=None)
    assert len(spy_verify) == 1  # успех — тоже ровно один verify


# --- set/rotate секрета ---


async def test_set_secret_on_null_then_login_works(session, seeded_user):
    """set: был NULL → после установки секрета login проходит новым секретом."""
    assert seeded_user.auth_secret_hash is None
    res = await set_or_rotate_secret(session, user_id=seeded_user.id)
    assert res.user_id == seeded_user.id
    assert res.secret

    login = await login_with_secret(
        session, user_id=seeded_user.id, secret=res.secret, device_label=None
    )
    assert login.user_id == seeded_user.id


async def test_rotate_secret_old_fails_new_works_tokens_preserved(session):
    """rotate: старый секрет → провал, новый → успех; существующие api_tokens НЕ отозваны."""
    reg = await register_with_secret(session, device_label="iPhone")
    old_secret = reg.secret

    rot = await set_or_rotate_secret(session, user_id=reg.user_id)
    new_secret = rot.secret
    assert new_secret != old_secret

    # Старый секрет → провал.
    with pytest.raises(SecretAuthError):
        await login_with_secret(session, user_id=reg.user_id, secret=old_secret, device_label=None)
    # Новый секрет → успех.
    ok = await login_with_secret(session, user_id=reg.user_id, secret=new_secret, device_label=None)
    assert ok.user_id == reg.user_id

    # Существующий api_token (из register) НЕ отозван — lookup всё ещё проходит.
    from sqlalchemy import select

    from app.auth.token_service import parse_api_key
    from app.db.models import ApiToken

    parsed = parse_api_key(reg.api_key)
    row = await session.scalar(select(ApiToken).where(ApiToken.key_id == parsed.key_id))
    assert row is not None
    assert row.revoked_at is None  # ротация секрета ≠ logout устройств
