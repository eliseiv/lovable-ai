"""Сидинг единственного S1-пользователя с seeded Bearer-ключом (docs/05-security.md).

Запуск: python -m app.db.seed
Ключ берётся из SEED_API_KEY (env). В БД пишется только argon2id-хэш.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

from sqlalchemy import select

from app.core.config import get_settings
from app.core.ids import new_user_id
from app.core.logging import configure_logging, get_logger
from app.core.security import hash_api_key
from app.db.models import User
from app.db.session import session_scope

logger = get_logger(__name__)


async def seed_user() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)

    plaintext = settings.seed_api_key.get_secret_value()
    if not plaintext:
        raise SystemExit("SEED_API_KEY is empty — set it in env before seeding.")

    key_hash = hash_api_key(plaintext)
    async with session_scope() as session:
        existing = (
            (await session.execute(select(User).where(User.status == "active"))).scalars().all()
        )
        for user in existing:
            from app.core.security import verify_api_key

            # api_key_hash стал nullable (Sprint 3, ADR-008): Apple-юзеры без legacy-хэша
            # пропускаются — сравниваем только seeded-юзеров с непустым хэшем.
            if user.api_key_hash is not None and verify_api_key(plaintext, user.api_key_hash):
                logger.info("seed_user_exists", extra={"user_id": user.id})
                return
        user = User(
            id=new_user_id(),
            api_key_hash=key_hash,
            monthly_budget_usd=Decimal(settings.user_monthly_budget_usd),
            status="active",
        )
        session.add(user)
        await session.commit()
        logger.info("seed_user_created", extra={"user_id": user.id})


if __name__ == "__main__":
    asyncio.run(seed_user())
