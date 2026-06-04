"""Router /billing (docs/modules/billing/02-api-contracts.md).

POST /billing/webhook/adapty — server-to-server, верификация подписи Adapty (НЕ Bearer),
идемпотентность по adapty_event_id, маппинг → subscriptions.
GET /billing/me — Bearer; текущий access_level + остаток квоты.
"""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Header, Request, Response, status

from app.api.dependencies import CurrentUser, SessionDep
from app.api.errors import problem_responses, unauthorized
from app.billing.adapty_client import verify_webhook_signature
from app.billing.webhook_handler import (
    WebhookProcessingError,
    process_webhook,
)
from app.core.config import get_settings
from app.core.logging import get_logger
from app.schemas.api import BillingMeResponse
from app.services import billing_service

logger = get_logger(__name__)

router = APIRouter(prefix="/billing", tags=["Биллинг"])

# Заголовок подписи вебхука Adapty (HMAC-SHA256 raw_body). Имя — по конфигурации Adapty
# webhook v2; берём канонический header. Невалидно/отсутствует → 401 без раскрытия.
_SIGNATURE_HEADER = "adapty-signature"


@router.post(
    "/webhook/adapty",
    status_code=status.HTTP_200_OK,
    summary="Приём событий магазина подписок (server-to-server)",
    description=(
        "Служебный эндпоинт server-to-server: вызывается провайдером подписок, **не** "
        "предназначен для клиента iOS. Авторизация — не Bearer, а проверка секрета/подписи "
        "провайдера (некорректная подпись → `401`). Обработка идемпотентна (повтор события "
        "→ `200` без повторного применения)."
    ),
    responses=problem_responses(401),
)
async def adapty_webhook(
    request: Request,
    session: SessionDep,
    signature: Annotated[str | None, Header(alias=_SIGNATURE_HEADER)] = None,
) -> Response:
    """Приём событий магазина подписок (server-to-server, не Bearer). Подпись валидна →
    обработка; иначе 401. Идемпотентно по идентификатору события.
    """
    settings = get_settings()
    raw_body = await request.body()

    # Верификация подписи Adapty (constant-time HMAC). Невалидно → 401 без раскрытия причины.
    secret = settings.adapty_webhook_secret.get_secret_value()
    if not verify_webhook_signature(secret, raw_body, signature):
        logger.warning("billing_webhook_unauthorized")
        raise unauthorized("Webhook signature verification failed.")

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        # Подпись валидна, но тело не JSON — 5xx (Adapty повторит); не маскируем 200.
        raise WebhookProcessingError("Webhook body is not valid JSON.") from exc

    # process_webhook сам управляет транзакцией (insert ledger + apply subscriptions).
    # WebhookProcessingError всплывает как 500 (Adapty retry) — НЕ ловим здесь.
    result = await process_webhook(session, payload)
    logger.info("billing_webhook_outcome", extra={"outcome": result.outcome.value})
    return Response(status_code=status.HTTP_200_OK)


@router.get(
    "/me",
    response_model=BillingMeResponse,
    summary="Тариф и остаток квоты",
    description=(
        "Возвращает текущий тариф (`access_level`), статус подписки и остаток квот: "
        "генерации, правки, число одновременных задач и проектов (поля `quota.*`). "
        "Пустые значения лимитов (`null`) означают безлимит. Требуется заголовок "
        "`Authorization: Bearer <api-key>`."
    ),
    responses=problem_responses(401, 429),
)
async def billing_me(user: CurrentUser, session: SessionDep) -> BillingMeResponse:
    """Возвращает текущий тариф и остаток квоты. Нет подписки → бесплатный тариф.

    Единый источник агрегатов — billing_service.build_billing_snapshot (учитывает бонус-кредиты:
    bonus_generations_remaining + generations_remaining = плановый остаток + кредиты, ADR-021
    §10.4). Тот же снимок переиспользует админ GET /admin/users/{user_id}.
    """
    snapshot = await billing_service.build_billing_snapshot(session, user)
    return BillingMeResponse(
        access_level=snapshot.access_level,
        status=snapshot.status,
        period=snapshot.period,
        quota=snapshot.quota,
    )
