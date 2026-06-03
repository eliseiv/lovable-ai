"""Integration: сидинг S1-пользователя идемпотентен (docs/05-security.md, app/db/seed.py)."""

from __future__ import annotations

import pytest
from sqlalchemy import delete, select

from app.core.security import verify_api_key
from app.db.models import User
from app.db.session import session_scope

pytestmark = pytest.mark.asyncio


async def _count_users_with_key(plaintext: str) -> int:
    async with session_scope() as s:
        users = (await s.execute(select(User).where(User.status == "active"))).scalars().all()
    return sum(1 for u in users if verify_api_key(plaintext, u.api_key_hash))


@pytest.fixture
def seed_env(monkeypatch):  # noqa: ANN001, ANN201
    # Уникальный seed-ключ, чтобы не конфликтовать с другими тестами.
    key = "seed-test-key-unique-xyz"
    from app.core.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s.seed_api_key, "get_secret_value", lambda: key)
    return key


async def test_seed_user_idempotent(autonomous_db, seed_env):
    from app.db.seed import seed_user

    # Чистим возможный остаток.
    async with session_scope() as s:
        users = (await s.execute(select(User).where(User.status == "active"))).scalars().all()
        for u in users:
            if verify_api_key(seed_env, u.api_key_hash):
                await s.execute(delete(User).where(User.id == u.id))
        await s.commit()

    await seed_user()
    assert await _count_users_with_key(seed_env) == 1
    # Повторный сидинг не создаёт дубль.
    await seed_user()
    assert await _count_users_with_key(seed_env) == 1

    # cleanup
    async with session_scope() as s:
        users = (await s.execute(select(User).where(User.status == "active"))).scalars().all()
        for u in users:
            if verify_api_key(seed_env, u.api_key_hash):
                await s.execute(delete(User).where(User.id == u.id))
        await s.commit()


async def test_seed_user_empty_key_raises(autonomous_db, monkeypatch):
    from app.core.config import get_settings
    from app.db.seed import seed_user

    s = get_settings()
    monkeypatch.setattr(s.seed_api_key, "get_secret_value", lambda: "")
    with pytest.raises(SystemExit):
        await seed_user()
