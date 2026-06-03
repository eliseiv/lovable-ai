"""Integration: индексируемый O(1) token lookup, TD-004 (один argon2-verify), revoke.

docs/modules/auth/03-architecture.md §2, ADR-008, docs/100-known-tech-debt §TD-004.
Реальный Postgres (api_tokens). Проверяет:
- валидный lv_-ключ → строка токена; неверный secret при верном key_id → None;
- отозванный токен (revoked_at) → None (lookup фильтрует);
- TD-004: число argon2-verify НЕ растёт с числом юзеров/токенов (ровно 1 на запрос);
- revoke идемпотентен; чужой токен → False (роутер → 404).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.auth import token_service
from app.db.models import User

pytestmark = pytest.mark.asyncio


async def _new_user(session, uid: str) -> User:  # noqa: ANN001
    user = User(
        id=uid,
        apple_sub=f"sub-{uid}",
        api_key_hash=None,
        monthly_budget_usd=Decimal("50.0000"),
        status="active",
    )
    session.add(user)
    await session.flush()
    return user


# --- happy lookup ---


async def test_issue_then_authenticate_valid_key(session):
    user = await _new_user(session, "u_lookupok0000000000001")
    issued = await token_service.issue_token(session, user_id=user.id, device_label="iPhone")
    await session.flush()

    token = await token_service.authenticate(session, issued.api_key)
    assert token is not None
    assert token.user_id == user.id
    assert token.key_id == issued.key_id


async def test_wrong_secret_with_valid_key_id_rejected(session):
    user = await _new_user(session, "u_lookupbad0000000000001")
    issued = await token_service.issue_token(session, user_id=user.id)
    await session.flush()

    # Верный key_id, но подменённый secret → один argon2-verify провалится → None.
    tampered = f"lv_{issued.key_id}_wrongsecretwrongsecret"
    assert await token_service.authenticate(session, tampered) is None


async def test_unknown_key_id_rejected(session):
    assert await token_service.authenticate(session, "lv_nonexistentkeyid_secret") is None


async def test_non_prefixed_key_rejected(session):
    assert await token_service.authenticate(session, "legacy-plain-key") is None


# --- revoke фильтруется в lookup ---


async def test_revoked_token_rejected(session):
    user = await _new_user(session, "u_revoked00000000000001")
    issued = await token_service.issue_token(session, user_id=user.id)
    await session.flush()

    ok = await token_service.revoke_token(session, user_id=user.id, token_id=issued.token_id)
    assert ok is True
    # Отозванный ключ сразу → None (revoked_at IS NOT NULL отфильтрован).
    assert await token_service.authenticate(session, issued.api_key) is None


async def test_revoke_idempotent_and_cross_tenant(session):
    owner = await _new_user(session, "u_revowner0000000000001")
    other = await _new_user(session, "u_revother0000000000001")
    issued = await token_service.issue_token(session, user_id=owner.id)
    await session.flush()

    # Повтор revoke по уже отозванному → True (идемпотентно).
    assert await token_service.revoke_token(session, user_id=owner.id, token_id=issued.token_id)
    assert await token_service.revoke_token(session, user_id=owner.id, token_id=issued.token_id)
    # Чужой пытается отозвать → False (роутер транслирует в 404, не раскрывая существование).
    assert (
        await token_service.revoke_token(session, user_id=other.id, token_id=issued.token_id)
        is False
    )


# --- TD-004: один argon2-verify, число НЕ зависит от количества юзеров/токенов ---


@pytest.mark.parametrize("n_users", [1, 5, 20])
async def test_argon2_verify_count_independent_of_user_count(session, monkeypatch, n_users):
    """Ровно ОДИН argon2-verify на Bearer-запрос независимо от N юзеров/токенов (TD-004 closed).

    Спай на verify_api_key в точке вызова token_service. O(1) lookup по UNIQUE key_id →
    одна строка → один constant-time verify. O(N)-перебор S1 (verify на каждого юзера) снят.
    """
    real_verify = token_service.verify_api_key
    calls = {"n": 0}

    def _spy(plaintext, stored_hash):  # noqa: ANN001, ANN202
        calls["n"] += 1
        return real_verify(plaintext, stored_hash)

    monkeypatch.setattr(token_service, "verify_api_key", _spy)

    # N юзеров, у каждого по 2 токена (= 2N строк api_tokens).
    target_key: str | None = None
    for i in range(n_users):
        user = await _new_user(session, f"u_td004_{i:016d}")
        for _ in range(2):
            issued = await token_service.issue_token(session, user_id=user.id)
            if i == 0 and target_key is None:
                target_key = issued.api_key
    await session.flush()

    assert target_key is not None
    token = await token_service.authenticate(session, target_key)
    assert token is not None
    # Ключевая инварианта TD-004: ровно один verify, НЕ зависит от n_users/числа токенов.
    assert calls["n"] == 1, f"ожидался 1 argon2-verify, получено {calls['n']} при N={n_users}"


async def test_list_active_tokens_excludes_revoked(session):
    user = await _new_user(session, "u_listactive000000000001")
    t1 = await token_service.issue_token(session, user_id=user.id, device_label="iPhone")
    t2 = await token_service.issue_token(session, user_id=user.id, device_label="iPad")
    await session.flush()

    await token_service.revoke_token(session, user_id=user.id, token_id=t1.token_id)
    active = await token_service.list_active_tokens(session, user.id)
    ids = {t.id for t in active}
    assert t2.token_id in ids
    assert t1.token_id not in ids  # отозванный исключён
