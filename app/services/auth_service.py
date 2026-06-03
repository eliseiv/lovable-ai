"""Sign in with Apple: верификация → upsert user по apple_sub → выдача токена.

docs/modules/auth/03-architecture.md §1, ADR-007. API не делает ничего инлайн кроме
Postgres-операций; Apple-верификация изолирована в app.auth.apple_verify (мокается qa).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.apple_verify import AppleTokenError, verify_apple_identity_token
from app.auth.token_service import issue_token
from app.core.config import get_settings
from app.core.ids import new_user_id
from app.core.logging import get_logger
from app.db.models import User

logger = get_logger(__name__)


@dataclass(frozen=True)
class AppleSignInResult:
    api_key: str
    token_id: str
    user_id: str


async def sign_in_with_apple(
    session: AsyncSession,
    *,
    identity_token: str,
    nonce: str | None,
    device_label: str | None,
) -> AppleSignInResult:
    """Верифицирует Apple identity token, upsert user по apple_sub, выдаёт наш Bearer.

    Любой провал верификации → AppleTokenError (роутер транслирует в 401, без деталей).
    """
    apple_sub = verify_apple_identity_token(identity_token, nonce=nonce)

    user = await _upsert_user_by_apple_sub(session, apple_sub)
    issued = await issue_token(session, user_id=user.id, device_label=device_label)
    await session.commit()

    logger.info("apple_sign_in", extra={"user_id": user.id, "key_id": issued.key_id})
    return AppleSignInResult(
        api_key=issued.api_key,
        token_id=issued.token_id,
        user_id=user.id,
    )


async def _upsert_user_by_apple_sub(session: AsyncSession, apple_sub: str) -> User:
    """Найти user по apple_sub (UNIQUE) либо создать нового (+ adapty_customer_user_id=id)."""
    existing = await _find_by_apple_sub(session, apple_sub)
    if existing is not None:
        return existing

    settings = get_settings()
    user = User(
        id=new_user_id(),
        apple_sub=apple_sub,
        api_key_hash=None,  # реальные токены — в api_tokens; legacy-поле не используется.
        # Маппинг user ↔ Adapty создаётся при первом входе iOS (= users.id), docs §auth.
        adapty_customer_user_id=None,
        monthly_budget_usd=Decimal(settings.user_monthly_budget_usd),
        status="active",
    )
    user.adapty_customer_user_id = user.id
    session.add(user)
    try:
        await session.flush()
    except IntegrityError:
        # Гонка двух параллельных первых логинов одного apple_sub: UNIQUE отклонил второй.
        await session.rollback()
        winner = await _find_by_apple_sub(session, apple_sub)
        if winner is None:
            raise
        return winner
    return user


async def _find_by_apple_sub(session: AsyncSession, apple_sub: str) -> User | None:
    result = await session.execute(select(User).where(User.apple_sub == apple_sub))
    return result.scalar_one_or_none()


__all__ = ["AppleSignInResult", "AppleTokenError", "sign_in_with_apple"]
