"""Начисление по прямому StoreKit-пути (ADR-039 §C/§D, docs billing §13.2).

Верификация JWS — в app/billing/storekit (чистая крипто). Сюда приходит уже верифицированная
VerifiedTransaction; модуль применяет её к БД: глобальная идемпотентность по
store_transactions.transaction_id (PK) + переиспользование существующей grant-механики
(grant_tokens для токен-паков, apply_storekit_subscription для подписки) — write-path НЕ
дублируется.

Начисление — на аутентифицированного user_id (Bearer), НЕ на account из payload (docs/05-security
→ StoreKit). Сосуществование с Adapty (тот же bonus_generations_balance/subscriptions) — по
разным ключам дедупа, контракт разграничения каналов (docs §13.4). Реальный сбой БД →
StoreKitProcessingError (роутер → 5xx, клиент повторит).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing import subscription_state
from app.billing.storekit import VerifiedTransaction
from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.models import StoreTransaction

logger = get_logger(__name__)

# Значения store_transactions.kind (docs §13.2).
KIND_TOKENS_PURCHASE = "tokens_purchase"
KIND_SUBSCRIPTION_SYNC = "subscription_sync"


class StoreKitProcessingError(Exception):
    """Реальный внутренний сбой (БД) при начислении → 5xx (клиент повторит)."""


@dataclass(frozen=True)
class PurchaseResult:
    """Бизнес-исход начисления (роутер транслирует в тело {status, reason?, ...})."""

    status: str  # applied / duplicate / ignored
    reason: str | None = None
    tokens_granted: int | None = None
    access_level: str | None = None
    expires_at: datetime | None = None


async def apply_tokens_purchase(
    session: AsyncSession,
    *,
    user_id: str,
    txn: VerifiedTransaction,
) -> PurchaseResult:
    """POST /v1/tokens/purchase — consumable токен-пак (docs §4.1/§13.2).

    Порядок исходов по нормативной таблице api-contracts §4.1: неизвестный product_id →
    ignored:unknown_token_product; revoked → ignored:revoked; далее — глобальный дедуп по
    transaction_id (конфликт PK → duplicate), иначе applied. Начисление + insert
    store_transactions — в ОДНОЙ транзакции.
    """
    settings = get_settings()
    amount = subscription_state.resolve_consumable_tokens(txn.product_id, settings)
    if amount is None:
        # Неизвестный SKU — не угадываем (docs §11.3/§4.1). Не записываем store_transactions.
        logger.info(
            "storekit_ignored",
            extra={
                "reason": "unknown_token_product",
                "transaction_id": txn.transaction_id,
                "environment": txn.environment,
            },
        )
        return PurchaseResult(status="ignored", reason="unknown_token_product")

    if txn.revoked:
        logger.info(
            "storekit_ignored",
            extra={
                "reason": "revoked",
                "transaction_id": txn.transaction_id,
                "environment": txn.environment,
            },
        )
        return PurchaseResult(status="ignored", reason="revoked")

    # Глобальный дедуп: insert store_transactions(PK transaction_id) + grant_tokens в ОДНОЙ
    # транзакции. Конфликт PK (любым user_id) → IntegrityError → duplicate, начисление не
    # повторяется.
    session.add(
        StoreTransaction(
            transaction_id=txn.transaction_id,
            original_transaction_id=txn.original_transaction_id,
            user_id=user_id,
            product_id=txn.product_id,
            kind=KIND_TOKENS_PURCHASE,
            environment=txn.environment,
            amount=amount,
        )
    )
    try:
        await subscription_state.grant_tokens(
            session,
            user_id=user_id,
            event_id=txn.transaction_id,
            event_type=KIND_TOKENS_PURCHASE,
            amount=amount,
            created_by="storekit",
            reason="storekit:tokens_purchase",
            idempotency_key=f"storekit:{txn.transaction_id}",
        )
        await session.commit()
    except IntegrityError:
        # Конфликт PK store_transactions / партиального UNIQUE credit_grants → duplicate.
        await session.rollback()
        logger.info(
            "storekit_duplicate",
            extra={"kind": KIND_TOKENS_PURCHASE, "transaction_id": txn.transaction_id},
        )
        return PurchaseResult(status="duplicate")
    except Exception as exc:  # noqa: BLE001 - реальный сбой БД → 5xx (клиент повторит)
        await session.rollback()
        logger.error(
            "storekit_apply_failed",
            extra={"kind": KIND_TOKENS_PURCHASE, "transaction_id": txn.transaction_id},
        )
        raise StoreKitProcessingError(
            f"Failed to apply StoreKit tokens purchase {txn.transaction_id}: {exc}"
        ) from exc

    logger.info(
        "storekit_applied",
        extra={
            "kind": KIND_TOKENS_PURCHASE,
            "transaction_id": txn.transaction_id,
            "environment": txn.environment,
            "amount": amount,
        },
    )
    return PurchaseResult(status="applied", tokens_granted=amount)


async def apply_subscription_sync(
    session: AsyncSession,
    *,
    user_id: str,
    txn: VerifiedTransaction,
) -> PurchaseResult:
    """POST /v1/subscription/sync — подписка → pro (docs §4.2/§13.2). Токены НЕ начисляет.

    Порядок исходов по api-contracts §4.2: revoked → ignored:revoked; expires_at в прошлом →
    ignored:expired; далее — insert store_transactions ON CONFLICT (transaction_id) DO NOTHING
    (повтор → duplicate) + apply_storekit_subscription (state-set, natural-idempotent), в ОДНОЙ
    транзакции. Renewal = новая transaction_id → новая строка → обновление expires_at.
    """
    if txn.revoked:
        logger.info(
            "storekit_ignored",
            extra={
                "reason": "revoked",
                "transaction_id": txn.transaction_id,
                "environment": txn.environment,
            },
        )
        return PurchaseResult(status="ignored", reason="revoked")

    now = datetime.now(UTC)
    if txn.expires_at is not None and txn.expires_at < now:
        logger.info(
            "storekit_ignored",
            extra={
                "reason": "expired",
                "transaction_id": txn.transaction_id,
                "environment": txn.environment,
            },
        )
        return PurchaseResult(status="ignored", reason="expired")

    # Глобальный дедуп: ON CONFLICT (transaction_id) DO NOTHING + RETURNING. Пустой RETURNING
    # (конфликт PK) → уже обработана (любым user_id) → duplicate, подписку повторно не применяем.
    insert_stmt = (
        pg_insert(StoreTransaction)
        .values(
            transaction_id=txn.transaction_id,
            original_transaction_id=txn.original_transaction_id,
            user_id=user_id,
            product_id=txn.product_id,
            kind=KIND_SUBSCRIPTION_SYNC,
            environment=txn.environment,
            amount=None,
        )
        .on_conflict_do_nothing(index_elements=["transaction_id"])
        .returning(StoreTransaction.transaction_id)
    )
    try:
        result = await session.execute(insert_stmt)
        if result.scalar_one_or_none() is None:
            await session.rollback()
            logger.info(
                "storekit_duplicate",
                extra={"kind": KIND_SUBSCRIPTION_SYNC, "transaction_id": txn.transaction_id},
            )
            return PurchaseResult(status="duplicate")

        sub = await subscription_state.apply_storekit_subscription(
            session,
            user_id=user_id,
            expires_at=txn.expires_at,
            environment=txn.environment,
            transaction_id=txn.transaction_id,
            original_transaction_id=txn.original_transaction_id,
            product_id=txn.product_id,
        )
        await session.commit()
    except Exception as exc:  # noqa: BLE001 - реальный сбой БД → 5xx (клиент повторит)
        await session.rollback()
        logger.error(
            "storekit_apply_failed",
            extra={"kind": KIND_SUBSCRIPTION_SYNC, "transaction_id": txn.transaction_id},
        )
        raise StoreKitProcessingError(
            f"Failed to apply StoreKit subscription sync {txn.transaction_id}: {exc}"
        ) from exc

    logger.info(
        "storekit_applied",
        extra={
            "kind": KIND_SUBSCRIPTION_SYNC,
            "transaction_id": txn.transaction_id,
            "environment": txn.environment,
        },
    )
    return PurchaseResult(
        status="applied",
        access_level=sub.access_level,
        expires_at=sub.expires_at,
    )
