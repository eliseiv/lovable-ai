"""Обработчик вебхука Adapty (docs/modules/billing/03-architecture.md §2, ADR-009).

Верификация подписи (НЕ Bearer) → идемпотентность по adapty_event_id (billing_events
UNIQUE) → маппинг customer_user_id → user → апдейт subscriptions (одна транзакция с
processed_at=now). Неизвестный customer_user_id → billing_events(user_id=NULL,
processed_at=NULL) без потери. Внутренняя ошибка после валидной подписи → 5xx (Adapty retry).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing import subscription_state
from app.core.logging import get_logger
from app.db.models import BillingEvent, User

logger = get_logger(__name__)


class WebhookOutcome(Enum):
    """Исход обработки вебхука (роутер транслирует в HTTP-статус)."""

    PROCESSED = "processed"  # 200: применено к subscriptions
    DUPLICATE = "duplicate"  # 200: идемпотентный повтор (no-op)
    UNKNOWN_USER = "unknown_user"  # 200: customer_user_id неизвестен, событие сохранено


@dataclass(frozen=True)
class WebhookResult:
    outcome: WebhookOutcome


class WebhookProcessingError(Exception):
    """Внутренняя ошибка обработки после валидной подписи → 5xx (Adapty повторит доставку)."""


async def _find_user(session: AsyncSession, customer_user_id: str) -> User | None:
    """user по customer_user_id = users.adapty_customer_user_id (= users.id, Q-BILLING-3)."""
    result = await session.execute(
        select(User).where(User.adapty_customer_user_id == customer_user_id)
    )
    user = result.scalar_one_or_none()
    if user is not None:
        return user
    # Фолбэк: customer_user_id = users.id (маппинг по дизайну Q-BILLING-3).
    return await session.get(User, customer_user_id)


async def process_webhook(session: AsyncSession, payload: dict[str, Any]) -> WebhookResult:
    """Обрабатывает валидированный (по подписи) вебхук. Подпись проверена ВЫШЕ (роутер).

    Идемпотентность по billing_events.adapty_event_id (UNIQUE): повтор → DUPLICATE no-op.
    Insert ledger-строки + апдейт subscriptions — в одной транзакции с processed_at=now.
    Ошибка апдейта → откат (processed_at=NULL), WebhookProcessingError → 5xx.
    """
    event_id = payload.get("event_id")
    event_type = payload.get("event_type")
    customer_user_id = payload.get("customer_user_id")

    if not event_id or not event_type:
        # Контрактный минимум отсутствует — это не подделка (подпись валидна), но и не
        # обрабатываемое событие. 5xx, чтобы Adapty не считал доставку успешной молча.
        raise WebhookProcessingError("Webhook missing required event_id/event_type.")

    # Идемпотентность: уже обработанное событие → DUPLICATE no-op.
    existing = await session.execute(
        select(BillingEvent).where(BillingEvent.adapty_event_id == event_id)
    )
    if existing.scalar_one_or_none() is not None:
        logger.info("billing_webhook_duplicate", extra={"event_id": event_id})
        return WebhookResult(outcome=WebhookOutcome.DUPLICATE)

    user = await _find_user(session, customer_user_id) if customer_user_id else None

    ledger = BillingEvent(
        adapty_event_id=event_id,
        event_type=event_type,
        user_id=user.id if user is not None else None,
        payload=payload,
        processed_at=None,
    )
    session.add(ledger)

    if user is None:
        # Рассинхрон: customer_user_id неизвестен. Сохраняем событие (user_id=NULL,
        # processed_at=NULL) для последующей обработки/алерта — НЕ теряем (Q-BILLING-3).
        try:
            await session.commit()
        except IntegrityError:
            # Гонка дублей по UNIQUE adapty_event_id — идемпотентно DUPLICATE.
            await session.rollback()
            return WebhookResult(outcome=WebhookOutcome.DUPLICATE)
        logger.warning(
            "billing_webhook_unknown_user",
            extra={"event_id": event_id, "customer_user_id": customer_user_id},
        )
        return WebhookResult(outcome=WebhookOutcome.UNKNOWN_USER)

    # Маппинг + апдейт subscriptions в той же транзакции, затем processed_at=now.
    try:
        await subscription_state.apply_webhook_event(
            session,
            user_id=user.id,
            event_type=event_type,
            profile=payload.get("profile", {}) or {},
            subscription_payload=payload.get("subscription", {}) or {},
            raw_payload=payload,
        )
        ledger.processed_at = datetime.now(UTC)
        await session.commit()
    except IntegrityError:
        # Гонка дублей по UNIQUE adapty_event_id (два конкурентных доставки) → DUPLICATE.
        await session.rollback()
        return WebhookResult(outcome=WebhookOutcome.DUPLICATE)
    except Exception as exc:  # noqa: BLE001 - откатываем и эскалируем как 5xx (Adapty retry)
        await session.rollback()
        logger.error(
            "billing_webhook_apply_failed", extra={"event_id": event_id, "error": str(exc)}
        )
        raise WebhookProcessingError(f"Failed to apply webhook {event_id}: {exc}") from exc

    logger.info(
        "billing_webhook_processed",
        extra={"event_id": event_id, "event_type": event_type, "user_id": user.id},
    )
    return WebhookResult(outcome=WebhookOutcome.PROCESSED)
