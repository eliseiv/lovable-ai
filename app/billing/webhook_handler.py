"""Обработчик вебхука Adapty (docs/modules/billing/02-api-contracts.md §1, 03 §2, ADR-027/040).

Авторизация (Bearer constant-time) — в роутере (app/api/routers/billing). Сюда приходит уже
авторизованный сырой payload (dict) + сырое тело (bytes, для tier-3 хэша ключа). Always-200-on-
bad-input (ADR-027 §B): после авторизации любой кривой payload → 200 {"status":"ignored",...};
5xx — ТОЛЬКО при реальном сбое БД.

Ключ дедупа (→ billing_events.adapty_event_id, UNIQUE) выводится ВСЕГДА по ADR-040 §A
(event_properties.profile_event_id → adapty-syn:{event_type}:{txid} → adapty-syn:body:{sha256}) —
денежное событие без profile_event_id больше НЕ дропается тихо (§B, устранён прод-инцидент
nexoraweb.shop 2026-07-10; ветка missing_event_id упразднена). Классификация event_type по
трём множествам (§C): handled → apply_webhook_event; consumable → _process_consumable;
consciously-ignored → billing_events(processed_at=NULL) no-op; неизвестный (вне 18) → 200 ignored.
Маппинг customer_user_id → user → апдейт subscriptions + (started/renewed) token-grant по тиру —
в ОДНОЙ транзакции с processed_at=now. Неизвестный customer_user_id → billing_events(user_id=NULL,
processed_at=NULL) без потери. Реальный сбой БД при коммите → 5xx (Adapty retry). Диагностика
отброшенных событий — WARN/INFO по §D (значения payload/PII не логируются).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing import subscription_state
from app.core.config import get_settings
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


def _resolve_dedup_key(
    payload: dict[str, Any], raw_body: bytes, event_type: str | None
) -> tuple[str, bool]:
    """Ключ дедупликации события (→ billing_events.adapty_event_id, UNIQUE), ADR-040 §A.

    Резолюция по приоритету (первый непустой), возвращает `(key, used_fallback)`:
      1. `event_properties.profile_event_id` — канонический ключ провайдера (UUID), как есть.
      2. `adapty-syn:{event_type}:{txid}`, где `txid = event_properties.transaction_id ||
         event_properties.original_transaction_id` (endorsed провайдером, скоуп префиксом
         event_type исключает кросс-типовую коллизию одной транзакции).
      3. `adapty-syn:body:{sha256(raw_body)}` — последний резерв (гарантия «никогда не
         дропнуть тихо»): детерминирован ⇒ идентичная переотправка → тот же ключ → дедуп.

    Ключ выводится ВСЕГДА (тир 3 на непустом теле не пуст ⇒ ветки `missing_event_id` нет,
    ADR-040 §B). `used_fallback=True` при тире 2/3 (profile_event_id отсутствовал) —
    диагностический WARN-сигнал рассинхрона схемы провайдера (§D).
    """
    props = payload.get("event_properties")
    props = props if isinstance(props, dict) else {}
    profile_event_id = props.get("profile_event_id")
    if profile_event_id:
        return str(profile_event_id), False
    txid = _first_nonempty(props.get("transaction_id"), props.get("original_transaction_id"))
    if txid:
        return f"adapty-syn:{event_type or ''}:{txid}", True
    return f"adapty-syn:body:{hashlib.sha256(raw_body).hexdigest()}", True


def _extract_event_type(payload: dict[str, Any]) -> str | None:
    """event_type → .lower() (ADR-027 §C)."""
    value = payload.get("event_type")
    return value.lower() if isinstance(value, str) and value else None


def _extract_customer_user_id(payload: dict[str, Any]) -> str | None:
    """customer_user_id = customer_user_id || user_id (ADR-041 §A).

    Верхнеуровневый `customer_user_id` — единственный реальный источник (identity-контракт
    ADR-027 §G, Q-BILLING-3). Fallback `profile.customer_user_id` УДАЛЁН: объекта `profile`
    в payload Adapty нет (ADR-041 §A, факт №1) — чтение было dead-путём. Legacy-fallback на
    верхнеуровневый `user_id` сохранён как no-op-совместимость.
    """
    value = _first_nonempty(payload.get("customer_user_id"), payload.get("user_id"))
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


def _profile_id(payload: dict[str, Any]) -> str | None:
    """Adapty profile_id (псевдонимный идентификатор профиля, §D — разрешён в диагностике)."""
    value = payload.get("profile_id")
    return value if isinstance(value, str) and value else None


async def process_webhook(session: AsyncSession, payload: Any, raw_body: bytes) -> WebhookResult:
    """Обрабатывает авторизованный (Bearer проверен в роутере) payload вебхука.

    Always-200-on-bad-input (ADR-027 §B): кривой/неприменимый вход → IGNORED (reason/
    event_type); применённое событие → APPLIED; повтор ключа дедупа → DUPLICATE. 5xx — ТОЛЬКО
    при реальном сбое БД (WebhookProcessingError). Insert billing_events + апдейт subscriptions
    + token-grant (started/renewed) — в ОДНОЙ транзакции с processed_at=now.

    Ключ дедупа (→ billing_events.adapty_event_id) выводится ВСЕГДА (ADR-040 §A/§B:
    profile_event_id → syn:txid → syn:body-hash) ⇒ денежное событие без profile_event_id больше
    НЕ дропается тихо (устранён прод-инцидент nexoraweb.shop 2026-07-10). Диагностика
    отброшенных событий — WARN/INFO по §D (значения payload/PII не логируются).
    """
    if not isinstance(payload, dict):
        # not-an-object покрыто здесь; пустое тело/не-JSON отбивается в роутере до вызова.
        # Логируем факт и ТИП полученного объекта (не содержимое — PII/платёжные данные,
        # 05-security §Секреты), чтобы по прод-логам была видна причина отказа (ранее — молча).
        logger.warning(
            "billing_webhook_ignored",
            extra={"reason": "not_an_object", "payload_type": type(payload).__name__},
        )
        return WebhookResult(outcome=WebhookOutcome.IGNORED, reason="not_an_object")

    event_type = _extract_event_type(payload)

    # Неизвестный тип (вне 18 фактических типов Adapty, §C) → 200 ignored:event_type + WARN
    # (сигнал обновить KNOWN_EVENT_TYPES). НЕ персистится (api-contracts §1). Проверяем ДО
    # резолюции ключа — неизвестное событие не обрабатывается по синтетическому ключу.
    if event_type not in subscription_state.KNOWN_EVENT_TYPES:
        logger.warning(
            "billing_webhook_ignored",
            extra={
                "reason": "unknown_event",
                "event_type": event_type,
                "profile_id": _profile_id(payload),
            },
        )
        return WebhookResult(outcome=WebhookOutcome.IGNORED, event_type=event_type or "")

    dedup_key, used_fallback = _resolve_dedup_key(payload, raw_body, event_type)
    customer_user_id = _extract_customer_user_id(payload)

    if used_fallback:
        # Тир 2/3: profile_event_id отсутствовал — рассинхрон схемы провайдера (§B/§D).
        # Событие НЕ дропается (обрабатывается по синтетическому ключу), но WARN-след обязателен.
        logger.warning(
            "billing_webhook_fallback_key",
            extra={
                "reason": "profile_event_id_absent",
                "event_type": event_type,
                "customer_user_id": customer_user_id,
                "profile_id": _profile_id(payload),
                "adapty_event_id": dedup_key,
            },
        )

    # Осознанно игнорируемый известный тип (§C.3): персистим billing_events(processed_at=NULL),
    # никаких денежных/state-эффектов, 200 ignored:event_type + INFO unhandled_known_event.
    if event_type in subscription_state.CONSCIOUSLY_IGNORED_EVENT_TYPES:
        return await _persist_ignored_known(
            session,
            dedup_key=dedup_key,
            event_type=event_type,
            payload=payload,
            customer_user_id=customer_user_id,
        )

    # Идемпотентность: уже обработанное событие → DUPLICATE no-op (начисление не повторяется).
    existing = await session.execute(
        select(BillingEvent).where(BillingEvent.adapty_event_id == dedup_key)
    )
    if existing.scalar_one_or_none() is not None:
        logger.info("billing_webhook_duplicate", extra={"adapty_event_id": dedup_key})
        return WebhookResult(outcome=WebhookOutcome.DUPLICATE)

    user = await _find_user(session, customer_user_id) if customer_user_id else None

    ledger = BillingEvent(
        adapty_event_id=dedup_key,
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
            raise WebhookProcessingError(f"Failed to persist webhook {dedup_key}: {exc}") from exc
        logger.warning(
            "billing_webhook_unknown_user",
            extra={
                "reason": "missing_customer_user_id",
                "event_type": event_type,
                "customer_user_id": customer_user_id,
                "profile_id": _profile_id(payload),
                "adapty_event_id": dedup_key,
            },
        )
        return WebhookResult(outcome=WebhookOutcome.IGNORED, reason="missing_customer_user_id")

    # Consumable-покупка токен-пака (non_subscription_purchase, ADR-038 §A): ОТДЕЛЬНАЯ ветка,
    # МИНУЯ apply_webhook_event — subscriptions/access_level НЕ трогаются, эффект только
    # начисление токенов по vendor_product_id (docs §11.3).
    if event_type in subscription_state.CONSUMABLE_EVENT_TYPES:
        return await _process_consumable(
            session,
            ledger=ledger,
            event_id=dedup_key,
            event_type=event_type,
            user_id=user.id,
            payload=payload,
        )

    # Апдейт subscriptions + (started/renewed) token-grant в ТОЙ ЖЕ транзакции, processed_at=now.
    # Подписочные поля лежат в event_properties (ADR-041 §A: объектов profile/subscription в
    # payload Adapty нет) — передаём event_properties, apply_webhook_event извлекает из них.
    event_props = payload.get("event_properties")
    event_props = event_props if isinstance(event_props, dict) else {}
    try:
        await subscription_state.apply_webhook_event(
            session,
            user_id=user.id,
            event_type=event_type,
            event_properties=event_props,
            raw_payload=payload,
        )
        if event_type in subscription_state.TOKEN_GRANT_EVENT_TYPES:
            await subscription_state.grant_subscription_tokens(
                session,
                user_id=user.id,
                event_id=dedup_key,
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
            "billing_webhook_apply_failed", extra={"adapty_event_id": dedup_key, "error": str(exc)}
        )
        raise WebhookProcessingError(f"Failed to apply webhook {dedup_key}: {exc}") from exc

    logger.info(
        "billing_webhook_processed",
        extra={"adapty_event_id": dedup_key, "event_type": event_type, "user_id": user.id},
    )
    return WebhookResult(outcome=WebhookOutcome.APPLIED)


async def _persist_ignored_known(
    session: AsyncSession,
    *,
    dedup_key: str,
    event_type: str,
    payload: dict[str, Any],
    customer_user_id: str | None,
) -> WebhookResult:
    """Осознанно игнорируемый известный тип (ADR-040 §C.3): no-op с аудит-следом.

    Персистит billing_events(adapty_event_id, event_type, user_id=NULL, processed_at=NULL) —
    денежных/state-эффектов НЕТ (subscriptions/токены не трогаются), права по этим событиям
    реконсилятся getProfile-resync (§3). Идемпотентный повтор (UNIQUE adapty_event_id) →
    no-op (строка уже есть). 200 ignored:event_type + INFO unhandled_known_event (§D).
    Реальный сбой БД → 5xx (Adapty retry).
    """
    session.add(
        BillingEvent(
            adapty_event_id=dedup_key,
            event_type=event_type,
            user_id=None,
            payload=payload,
            processed_at=None,
        )
    )
    try:
        await session.commit()
    except IntegrityError:
        # Идемпотентный повтор осознанно-игнорируемого события — строка уже персистирована.
        await session.rollback()
    except Exception as exc:  # noqa: BLE001 - реальный сбой БД → 5xx (Adapty retry)
        await session.rollback()
        raise WebhookProcessingError(
            f"Failed to persist ignored webhook {dedup_key}: {exc}"
        ) from exc
    logger.info(
        "billing_webhook_ignored",
        extra={
            "reason": "unhandled_known_event",
            "event_type": event_type,
            "customer_user_id": customer_user_id,
            "profile_id": _profile_id(payload),
            "adapty_event_id": dedup_key,
        },
    )
    return WebhookResult(outcome=WebhookOutcome.IGNORED, event_type=event_type)


async def _process_consumable(
    session: AsyncSession,
    *,
    ledger: BillingEvent,
    event_id: str,
    event_type: str,
    user_id: str,
    payload: dict[str, Any],
) -> WebhookResult:
    """Обрабатывает non_subscription_purchase (consumable токен-пак, ADR-038, docs §11.3).

    Отдельная ветка, МИНУЯ apply_webhook_event: subscriptions/access_level НЕ трогаются — эффект
    только начисление токенов по vendor_product_id через общий write-path (§11.2). Известный
    vendor_product_id → grant_tokens(amount), processed_at=now, 200 applied. Неизвестный/
    отсутствует → токены НЕ начисляются, ledger processed_at=NULL (не теряем событие — ручная
    реобработка после правки TOKEN_PACK_PRODUCTS), alert billing_unknown_token_product,
    200 ignored:unknown_token_product (§11.3/E). Реальный сбой БД → 5xx (Adapty retry).
    """
    settings = get_settings()
    vendor_product_id = _extract_vendor_product_id(payload)
    amount = subscription_state.resolve_consumable_tokens(vendor_product_id, settings)

    if amount is None:
        # Неизвестный token-product: не угадываем. Событие фиксируем с processed_at=NULL для
        # ручной реобработки после правки env; alert оператору.
        try:
            await session.commit()
        except IntegrityError:
            # Гонка дублей по UNIQUE adapty_event_id → идемпотентно DUPLICATE.
            await session.rollback()
            return WebhookResult(outcome=WebhookOutcome.DUPLICATE)
        except Exception as exc:  # noqa: BLE001 - реальный сбой БД → 5xx (Adapty retry)
            await session.rollback()
            raise WebhookProcessingError(f"Failed to persist webhook {event_id}: {exc}") from exc
        logger.warning(
            "billing_unknown_token_product",
            extra={
                "event_id": event_id,
                "user_id": user_id,
                "vendor_product_id": vendor_product_id,
            },
        )
        return WebhookResult(outcome=WebhookOutcome.IGNORED, reason="unknown_token_product")

    # Известный пак: начисление через общий write-path (§11.2) в ТОЙ ЖЕ транзакции с ledger,
    # processed_at=now. subscriptions НЕ трогаем (не подписка).
    try:
        await subscription_state.grant_tokens(
            session,
            user_id=user_id,
            event_id=event_id,
            event_type=event_type,
            amount=amount,
        )
        ledger.processed_at = datetime.now(UTC)
        await session.commit()
    except IntegrityError:
        # Гонка дублей по UNIQUE adapty_event_id / credit_grants(user_id,event_id) → DUPLICATE.
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
        extra={"event_id": event_id, "event_type": event_type, "user_id": user_id},
    )
    return WebhookResult(outcome=WebhookOutcome.APPLIED)
