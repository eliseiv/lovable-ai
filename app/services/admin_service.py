"""Админ-плоскость: login-as + бонус-кредиты (ADR-021, docs/modules/admin/03-architecture.md).

- login-as: выпуск пользовательского Bearer за указанного user_id через token_service;
  upsert юзера БЕЗ apple_sub (apple_sub=NULL, adapty_customer_user_id=users.id), docs auth §7.
- credits: атомарное начисление/коррекция бонус-генераций (insert credit_grants +
  UPDATE users.bonus_generations_balance), идемпотентность по Idempotency-Key, инвариант >= 0.

API не делает ничего инлайн кроме Postgres-операций (как auth_service). Защита эндпоинтов —
require_admin (X-Admin-Key), не Bearer.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import conflict, not_found, unprocessable
from app.auth.token_service import issue_token
from app.core.config import get_settings
from app.core.ids import new_credit_grant_id, new_user_id
from app.core.logging import get_logger
from app.db.models import CreditGrant, User

logger = get_logger(__name__)

_DEFAULT_DEVICE_LABEL = "admin-login"


@dataclass(frozen=True)
class LoginAsResult:
    api_key: str
    token_id: str
    user_id: str


@dataclass(frozen=True)
class GrantResult:
    user_id: str
    amount_applied: int
    bonus_generations_balance: int


async def _get_user(session: AsyncSession, user_id: str) -> User | None:
    return await session.get(User, user_id)


async def get_user(session: AsyncSession, user_id: str) -> User | None:
    """Читает пользователя по id (для админ GET /admin/users/{user_id}). None, если нет."""
    return await _get_user(session, user_id)


async def _create_admin_user(session: AsyncSession, user_id: str) -> User:
    """Создаёт юзера без Apple-якоря (apple_sub=NULL, adapty_customer_user_id=users.id).

    Минимальный upsert, зеркалит создание в /auth/apple, но без Apple Sign-In (ADR-021 §B).
    Гонка параллельных созданий одного id → IntegrityError → перечитываем строку.
    """
    settings = get_settings()
    user = User(
        id=user_id,
        apple_sub=None,  # admin-created юзер без Apple-якоря (ADR-021 §B; NULL вне UNIQUE).
        api_key_hash=None,
        adapty_customer_user_id=user_id,  # = users.id (как при Apple-входе).
        monthly_budget_usd=Decimal(settings.user_monthly_budget_usd),
        status="active",
    )
    session.add(user)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        existing = await _get_user(session, user_id)
        if existing is None:
            raise
        return existing
    return user


async def login_as(
    session: AsyncSession,
    *,
    user_id: str | None,
    device_label: str | None,
) -> LoginAsResult:
    """Выпускает свежий пользовательский Bearer за user_id (создаёт юзера без Apple, если нет).

    user_id задан и есть → токен за него; задан и нет → создать с этим id; опущен →
    сгенерировать новый u_... и создать. device_label по умолчанию "admin-login".
    Ключ возвращается ОДИН раз (как /auth/apple). Коммит — здесь (после issue_token).
    """
    if user_id is None:
        target_id = new_user_id()
        user = await _create_admin_user(session, target_id)
    else:
        existing = await _get_user(session, user_id)
        user = existing if existing is not None else await _create_admin_user(session, user_id)

    issued = await issue_token(
        session,
        user_id=user.id,
        device_label=device_label or _DEFAULT_DEVICE_LABEL,
    )
    await session.commit()

    logger.info("admin_login_as", extra={"user_id": user.id, "key_id": issued.key_id})
    return LoginAsResult(api_key=issued.api_key, token_id=issued.token_id, user_id=user.id)


async def grant_credits(
    session: AsyncSession,
    *,
    user_id: str,
    amount: int,
    reason: str | None,
    idempotency_key: str | None,
) -> GrantResult:
    """Начисляет/корректирует бонус-генерации (insert credit_grants + UPDATE баланса).

    Атомарно в одной транзакции. amount==0 → 422. amount<0 и баланс ушёл бы < 0 → 409
    (rollback, строка не пишется). Идемпотентность: повтор с тем же Idempotency-Key → no-op,
    возврат текущего баланса (партиальный UNIQUE (user_id, idempotency_key)). Нет юзера → 404.
    """
    if amount == 0:
        raise unprocessable("amount must be non-zero.")

    user = await _get_user(session, user_id)
    if user is None:
        raise not_found("User not found.")

    # Идемпотентность: если строка с этим (user_id, idempotency_key) уже есть — no-op.
    if idempotency_key is not None:
        existing_grant = await _find_grant_by_idempotency(session, user_id, idempotency_key)
        if existing_grant is not None:
            logger.info(
                "admin_grant_credits_replay",
                extra={"user_id": user_id, "idempotency_key": idempotency_key},
            )
            return GrantResult(
                user_id=user_id,
                amount_applied=existing_grant.amount,
                bonus_generations_balance=user.bonus_generations_balance,
            )

    new_balance = user.bonus_generations_balance + amount
    # Инвариант >= 0: отрицательная коррекция не уводит баланс в минус (409, rollback).
    if new_balance < 0:
        raise conflict(
            f"Correction would make balance negative "
            f"(current={user.bonus_generations_balance}, amount={amount})."
        )

    grant = CreditGrant(
        id=new_credit_grant_id(),
        user_id=user_id,
        amount=amount,
        reason=reason,
        idempotency_key=idempotency_key,
        created_by="admin",
    )
    session.add(grant)
    user.bonus_generations_balance = new_balance
    try:
        await session.commit()
    except IntegrityError:
        # Гонка идемпотентности: параллельный запрос с тем же ключом успел вставить строку.
        await session.rollback()
        if idempotency_key is not None:
            replayed = await _find_grant_by_idempotency(session, user_id, idempotency_key)
            fresh = await _get_user(session, user_id)
            if replayed is not None and fresh is not None:
                return GrantResult(
                    user_id=user_id,
                    amount_applied=replayed.amount,
                    bonus_generations_balance=fresh.bonus_generations_balance,
                )
        raise

    logger.info(
        "admin_grant_credits",
        extra={"user_id": user_id, "amount": amount, "balance": new_balance},
    )
    return GrantResult(
        user_id=user_id,
        amount_applied=amount,
        bonus_generations_balance=new_balance,
    )


async def _find_grant_by_idempotency(
    session: AsyncSession, user_id: str, idempotency_key: str
) -> CreditGrant | None:
    result = await session.execute(
        select(CreditGrant).where(
            CreditGrant.user_id == user_id,
            CreditGrant.idempotency_key == idempotency_key,
        )
    )
    return result.scalar_one_or_none()


__all__ = ["GrantResult", "LoginAsResult", "get_user", "grant_credits", "login_as"]
