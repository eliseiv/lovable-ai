"""Обработчик вебхука Adapty (docs/modules/billing/02-api-contracts.md §1, 03 §2, ADR-027).

Авторизация (Bearer constant-time) — в роутере (app/api/routers/billing). Сюда приходит уже
авторизованный сырой payload. Always-200-on-bad-input (ADR-027 §B): после авторизации любой
кривой payload → 200 {"status":"ignored",...}; 5xx — ТОЛЬКО при реальном сбое БД.

Дефенсивный парсинг (ADR-027 §C, поля разбросаны по версиям SDK) → идемпотентность по
billing_events.adapty_event_id (UNIQUE) → маппинг customer_user_id → user → апдейт
subscriptions + (для started/renewed) token-grant по тиру — в ОДНОЙ транзакции с
processed_at=now. Неизвестный customer_user_id → billing_events(user_id=NULL,
processed_at=NULL) без потери. Реальный сбой БД при коммите → 5xx (Adapty retry).
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
    """Исход обработки вебхука (роутер транслирует в тело {status, reason?, event_type?})."""

    APPLIED = "applied"  # 200: применено к subscriptions
    DUPLICATE = "duplicate"  # 200: идемпотентный повтор (no-op)
    IGNORED = "ignored"  # 200: кривой/неприменимый вход (reason/event_type)


@dataclass(frozen=True)
class WebhookResult:
    """Исход + опциональные reason/event_type для тела ответа (docs §1 response-схема)."""

    outcome: WebhookOutcome
    reason: str | None = None
    event_type: str | None = None


class WebhookProcessingError(Exception):
    """Реальный внутренний сбой (БД) после авторизации → 5xx (Adapty повторит доставку)."""


def _first_nonempty(*values: Any) -> Any | None:
    """Первое непустое значение из цепочки (дефенсив-извлечение, ADR-027 §C)."""
    for value in values:
        if value:
            return value
    return None


def _extract_event_id(payload: dict[str, Any]) -> str | None:
    """event_id = event_id || id (ADR-027 §C)."""
    value = _first_nonempty(payload.get("event_id"), payload.get("id"))
    return str(value) if value is not None else None


def _extract_event_type(payload: dict[str, Any]) -> str | None:
    """event_type → .lower() (ADR-027 §C)."""
    value = payload.get("event_type")
    return value.lower() if isinstance(value, str) and value else None


def _extract_customer_user_id(payload: dict[str, Any]) -> str | None:
    """customer_user_id = customer_user_id || profile.customer_user_id || user_id (ADR-027 §C)."""
    profile = payload.get("profile")
    profile_cuid = profile.get("customer_user_id") if isinstance(profile, dict) else None
    value = _first_nonempty(payload.get("customer_user_id"), profile_cuid, payload.get("user_id"))
    return str(value) if value is not None else None


def _extract_vendor_product_id(payload: dict[str, Any]) -> str | None:
    """vendor_product_id = event_properties.vendor_product_id || event_properties.product_id ||
    vendor_product_id || product_id (ADR-027 §C, тир-маппинг токенов docs §11.1).
    """
    props = payload.get("event_properties")
    props = props if isinstance(props, dict) else {}
    value = _first_nonempty(
        props.get("vendor_product_id"),
        props.get("product_id"),
        payload.get("vendor_product_id"),
        payload.get("product_id"),
    )
    return str(value) if value is not None else None


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


async def process_webhook(session: AsyncSession, payload: Any) -> WebhookResult:
    """Обрабатывает авторизованный (Bearer проверен в роутере) payload вебхука.

    Always-200-on-bad-input (ADR-027 §B): кривой/неприменимый вход → IGNORED (reason/
    event_type); применённое событие → APPLIED; повтор event_id → DUPLICATE. 5xx — ТОЛЬКО
    при реальном сбое БД (WebhookProcessingError). Insert billing_events + апдейт subscriptions
    + token-grant (started/renewed) — в ОДНОЙ транзакции с processed_at=now.
    """
    if not isinstance(payload, dict):
        # not-an-object покрыто здесь; пустое тело/не-JSON отбивается в роутере до вызова.
        return WebhookResult(outcome=WebhookOutcome.IGNORED, reason="not_an_object")

    event_id = _extract_event_id(payload)
    if not event_id:
        return WebhookResult(outcome=WebhookOutcome.IGNORED, reason="missing_event_id")

    event_type = _extract_event_type(payload)
    if not event_type or event_type not in subscription_state.KNOWN_EVENT_TYPES:
        # Неизвестный event_type → ignored с самим типом (docs §1: event_type, не reason).
        return WebhookResult(outcome=WebhookOutcome.IGNORED, event_type=event_type or "")

    # Идемпотентность: уже обработанное событие → DUPLICATE no-op (начисление не повторяется).
    existing = await session.execute(
        select(BillingEvent).where(BillingEvent.adapty_event_id == event_id)
    )
    if existing.scalar_one_or_none() is not None:
        logger.info("billing_webhook_duplicate", extra={"event_id": event_id})
        return WebhookResult(outcome=WebhookOutcome.DUPLICATE)

    customer_user_id = _extract_customer_user_id(payload)
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
        # Рассинхрон identity (нет customer_user_id или юзер не найден): сохраняем событие
        # (user_id=NULL, processed_at=NULL) для ресинка/алерта — НЕ теряем (ADR-027 §G).
        try:
            await session.commit()
        except IntegrityError:
            # Гонка дублей по UNIQUE adapty_event_id — идемпотентно DUPLICATE.
            await session.rollback()
            return WebhookResult(outcome=WebhookOutcome.DUPLICATE)
        except Exception as exc:  # noqa: BLE001 - реальный сбой БД → 5xx (Adapty retry)
            await session.rollback()
            raise WebhookProcessingError(f"Failed to persist webhook {event_id}: {exc}") from exc
        logger.warning(
            "billing_webhook_unknown_user",
            extra={"event_id": event_id, "customer_user_id": customer_user_id},
        )
        return WebhookResult(outcome=WebhookOutcome.IGNORED, reason="missing_customer_user_id")

    # Апдейт subscriptions + (started/renewed) token-grant в ТОЙ ЖЕ транзакции, processed_at=now.
    try:
        await subscription_state.apply_webhook_event(
            session,
            user_id=user.id,
            event_type=event_type,
            profile=payload.get("profile", {}) or {},
            subscription_payload=payload.get("subscription", {}) or {},
            raw_payload=payload,
        )
        if event_type in subscription_state.TOKEN_GRANT_EVENT_TYPES:
            await subscription_state.grant_subscription_tokens(
                session,
                user_id=user.id,
                event_id=event_id,
                event_type=event_type,
                vendor_product_id=_extract_vendor_product_id(payload),
            )
        ledger.processed_at = datetime.now(UTC)
        await session.commit()
    except IntegrityError:
        # Гонка дублей по UNIQUE adapty_event_id / credit_grants(user_id,event_id) → DUPLICATE
        # (повторное начисление отбито, ADR-027 §E).
        await session.rollback()
        return WebhookResult(outcome=WebhookOutcome.DUPLICATE)
    except Exception as exc:  # noqa: BLE001 - реальный сбой БД → 5xx (Adapty retry)
        await session.rollback()
        logger.error(
            "billing_webhook_apply_failed", extra={"event_id": event_id, "error": str(exc)}
        )
        raise WebhookProcessingError(f"Failed to apply webhook {event_id}: {exc}") from exc

    logger.info(
        "billing_webhook_processed",
        extra={"event_id": event_id, "event_type": event_type, "user_id": user.id},
    )
    return WebhookResult(outcome=WebhookOutcome.APPLIED)
