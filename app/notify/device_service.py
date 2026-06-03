"""Регистрация/отписка/выборка APNs device tokens (Sprint 5, ADR-013, docs/notify §1).

Upsert по (user_id, apns_token) при POST /v1/devices (повтор сбрасывает invalidated_at).
DELETE /v1/devices/{token} → invalidated_at=now() по (user_id, apns_token) (cross-tenant:
выборка строго по user_id). Выборка для push игнорирует invalidated_at IS NOT NULL.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import new_device_token_id
from app.core.logging import get_logger
from app.db.models import DeviceToken

logger = get_logger(__name__)

_VALID_PLATFORMS = frozenset({"ios"})
_VALID_ENVIRONMENTS = frozenset({"sandbox", "production"})


def validate_registration(platform: str, environment: str) -> str | None:
    """Валидация payload POST /v1/devices. Возвращает текст ошибки или None (валидно → 422)."""
    if platform not in _VALID_PLATFORMS:
        return f"Unsupported platform: {platform!r} (only 'ios')."
    if environment not in _VALID_ENVIRONMENTS:
        return f"Invalid environment: {environment!r} (sandbox|production)."
    return None


async def register_device(
    session: AsyncSession,
    *,
    user_id: str,
    apns_token: str,
    platform: str,
    environment: str,
) -> str:
    """Upsert device_tokens по (user_id, apns_token). Возвращает id строки (dev_...).

    Повторная регистрация того же токена — идемпотентно: обновляет platform/environment и
    СБРАСЫВАЕТ invalidated_at (повторно активирует ранее отписанный/мёртвый токен).
    Коммит — на стороне вызывающего.
    """
    new_id = new_device_token_id()
    stmt = (
        pg_insert(DeviceToken)
        .values(
            id=new_id,
            user_id=user_id,
            apns_token=apns_token,
            platform=platform,
            environment=environment,
            invalidated_at=None,
        )
        .on_conflict_do_update(
            constraint="uq_device_tokens_user_token",
            set_={
                "platform": platform,
                "environment": environment,
                "invalidated_at": None,
            },
        )
        .returning(DeviceToken.id)
    )
    result = await session.execute(stmt)
    row_id = result.scalar_one()
    await session.commit()
    logger.info("device_registered", extra={"user_id": user_id, "device_id": row_id})
    return row_id


async def unregister_device(session: AsyncSession, *, user_id: str, apns_token: str) -> bool:
    """Отписка: invalidated_at=now() по (user_id, apns_token). True, если токен найден (свой).

    False — чужой/несуществующий токен (→ 404, cross-tenant: выборка по user_id).
    Идемпотентно: повторная отписка уже-invalidated токена → True (no-op путь).
    """
    result = await session.execute(
        select(DeviceToken).where(
            DeviceToken.user_id == user_id, DeviceToken.apns_token == apns_token
        )
    )
    device = result.scalar_one_or_none()
    if device is None:
        return False
    if device.invalidated_at is None:
        device.invalidated_at = datetime.now(UTC)
        await session.commit()
        logger.info("device_unregistered", extra={"user_id": user_id, "device_id": device.id})
    return True


async def active_devices_for_user(session: AsyncSession, user_id: str) -> list[DeviceToken]:
    """Активные устройства пользователя (invalidated_at IS NULL) — для отправки push."""
    result = await session.execute(
        select(DeviceToken).where(
            DeviceToken.user_id == user_id, DeviceToken.invalidated_at.is_(None)
        )
    )
    return list(result.scalars().all())


async def mark_invalidated(session: AsyncSession, device_id: str) -> None:
    """Помечает токен мёртвым (APNs 410/400 BadDeviceToken). Коммит — на стороне вызывающего."""
    device = await session.get(DeviceToken, device_id)
    if device is not None and device.invalidated_at is None:
        device.invalidated_at = datetime.now(UTC)


async def mark_pushed(session: AsyncSession, device_id: str) -> None:
    """Обновляет last_push_at (успешная доставка, аудит). Коммит — на стороне вызывающего."""
    device = await session.get(DeviceToken, device_id)
    if device is not None:
        device.last_push_at = datetime.now(UTC)
