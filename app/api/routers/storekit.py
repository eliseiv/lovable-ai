"""Роутер прямого StoreKit-пути покупок (ADR-039, docs/modules/billing/02-api-contracts §4).

POST /v1/tokens/purchase — consumable токен-пак → начисление токенов.
POST /v1/subscription/sync — подписка → access_level=pro (токены НЕ начисляет).

Оба — клиентские (пользовательский Bearer, как весь клиентский API); тело — подписанная
StoreKit 2 JWS-транзакция. Верификация — собственный верификатор app/billing/storekit
(x5c → доверенный Apple root → ES256-подпись → payload). Начисление — на аутентифицированного
user_id (Bearer), НЕ на account из payload. Любой отказ верификации / roots не сконфигурированы
→ 422 fail-closed (invalid-storekit-transaction, крипто-детали не раскрываются). Идемпотентность —
глобальная по store_transactions.transaction_id.
"""

from __future__ import annotations

from fastapi import APIRouter, status

from app.api.dependencies import CurrentUser, SessionDep
from app.api.errors import ProblemException, problem_responses
from app.billing.storekit import StoreKitVerificationError, get_storekit_verifier
from app.billing.storekit_purchase import apply_subscription_sync, apply_tokens_purchase
from app.core.logging import get_logger
from app.schemas.api import (
    StoreKitTransactionRequest,
    SubscriptionSyncResponse,
    TokensPurchaseResponse,
)

logger = get_logger(__name__)

router = APIRouter(tags=["Биллинг"])


def _invalid_storekit_transaction() -> ProblemException:
    """422 fail-closed для неверифицируемой транзакции (крипто-детали не раскрываются, §4)."""
    return ProblemException(
        status=status.HTTP_422_UNPROCESSABLE_ENTITY,
        title="Unprocessable Entity",
        detail="StoreKit transaction could not be verified.",
        problem_type="invalid-storekit-transaction",
    )


@router.post(
    "/tokens/purchase",
    response_model=TokensPurchaseResponse,
    response_model_exclude_none=True,
    summary="Начисление токенов за покупку в App Store",
    description=(
        "Принимает подписанную транзакцию покупки набора токенов и начисляет их на баланс "
        "текущего пользователя. Требуется заголовок `Authorization: Bearer <api-key>`. "
        "Повторная отправка той же транзакции идемпотентна (повтор не начисляет)."
    ),
    responses=problem_responses(401, 422),
)
async def tokens_purchase(
    body: StoreKitTransactionRequest,
    user: CurrentUser,
    session: SessionDep,
) -> TokensPurchaseResponse:
    """Верификация JWS → начисление токен-пака на текущего пользователя (docs §4.1).

    Невалидный JWS / roots не сконфигурированы → 422 (fail-closed). Неизвестный SKU / revoked →
    200 ignored; повтор transaction_id → 200 duplicate; применено → 200 applied. 5xx — только
    реальный сбой БД (StoreKitProcessingError всплывает).
    """
    verifier = get_storekit_verifier()
    try:
        txn = verifier.verify(body.jws)
    except StoreKitVerificationError:
        # Крипто-детали НЕ логируем и НЕ раскрываем (docs/05-security → StoreKit).
        logger.info("storekit_verification_failed", extra={"endpoint": "tokens_purchase"})
        raise _invalid_storekit_transaction() from None

    result = await apply_tokens_purchase(session, user_id=user.id, txn=txn)
    return TokensPurchaseResponse(
        status=result.status,
        reason=result.reason,
        tokens_granted=result.tokens_granted,
    )


@router.post(
    "/subscription/sync",
    response_model=SubscriptionSyncResponse,
    response_model_exclude_none=True,
    summary="Синхронизация подписки из App Store",
    description=(
        "Принимает подписанную транзакцию подписки и активирует платный тариф текущего "
        "пользователя до указанного срока. Требуется заголовок `Authorization: Bearer "
        "<api-key>`. Повторная отправка той же транзакции идемпотентна."
    ),
    responses=problem_responses(401, 422),
)
async def subscription_sync(
    body: StoreKitTransactionRequest,
    user: CurrentUser,
    session: SessionDep,
) -> SubscriptionSyncResponse:
    """Верификация JWS → активация pro текущему пользователю (docs §4.2). Токены НЕ начисляет.

    Невалидный JWS / roots не сконфигурированы → 422 (fail-closed). revoked / expires_at в
    прошлом → 200 ignored; повтор transaction_id → 200 duplicate; применено → 200 applied.
    5xx — только реальный сбой БД.
    """
    verifier = get_storekit_verifier()
    try:
        txn = verifier.verify(body.jws)
    except StoreKitVerificationError:
        logger.info("storekit_verification_failed", extra={"endpoint": "subscription_sync"})
        raise _invalid_storekit_transaction() from None

    result = await apply_subscription_sync(session, user_id=user.id, txn=txn)
    return SubscriptionSyncResponse(
        status=result.status,
        reason=result.reason,
        access_level=result.access_level,
        expires_at=result.expires_at,
    )
