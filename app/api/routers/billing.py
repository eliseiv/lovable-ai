"""Router /billing (docs/modules/billing/02-api-contracts.md).

POST /billing/webhook/adapty — server-to-server, авторизация Bearer-секретом вебхука
(ADAPTY_WEBHOOK_SECRET, constant-time; НЕ пользовательский Bearer), always-200-on-bad-input
после авторизации (ADR-027 §A/§B), идемпотентность по adapty_event_id, маппинг → subscriptions
+ token-grant по тиру.
GET /billing/me — Bearer; текущий access_level + остаток квоты.
"""

from __future__ import annotations

import hmac
import json

from fastapi import APIRouter, Request, status

from app.api.dependencies import CurrentUser, SessionDep
from app.api.errors import ProblemException, problem_responses, unauthorized
from app.billing.webhook_handler import (
    WebhookOutcome,
    WebhookResult,
    process_webhook,
)
from app.core.config import get_settings
from app.core.logging import get_logger
from app.schemas.api import AdaptyWebhookResponse, BillingMeResponse
from app.services import billing_service

logger = get_logger(__name__)

router = APIRouter(prefix="/billing", tags=["Биллинг"])

# Префикс схемы авторизации вебхука (Authorization: Bearer <ADAPTY_WEBHOOK_SECRET>).
_BEARER_PREFIX = "Bearer "


def _extract_bearer_token(authorization: str | None) -> str | None:
    """Токен из заголовка `Authorization: Bearer <token>` (None при отсутствии/неверной схеме)."""
    if not authorization or not authorization.startswith(_BEARER_PREFIX):
        return None
    return authorization[len(_BEARER_PREFIX) :].strip()


def _misconfigured() -> ProblemException:
    """500 мисконфигурации сервера (ADAPTY_WEBHOOK_SECRET пуст/не задан, ADR-027 §A)."""
    return ProblemException(
        status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        title="Internal Server Error",
        detail="Adapty webhook secret is not configured on the server.",
        problem_type="internal-server-error",
    )


def _outcome_response(result: WebhookResult) -> AdaptyWebhookResponse:
    """WebhookResult → тело {status, reason?, event_type?} (docs §1 response-схема)."""
    return AdaptyWebhookResponse(
        status=result.outcome.value,
        reason=result.reason,
        event_type=result.event_type,
    )


@router.post(
    "/webhook/adapty",
    status_code=status.HTTP_200_OK,
    response_model=AdaptyWebhookResponse,
    response_model_exclude_none=True,
    summary="Приём событий магазина подписок (server-to-server)",
    description=(
        "Служебный эндпоинт server-to-server: вызывается провайдером подписок, **не** "
        "предназначен для клиента iOS. Авторизация — `Authorization: Bearer "
        "<ADAPTY_WEBHOOK_SECRET>` (constant-time, **не** пользовательский Bearer); неверный/"
        "отсутствующий токен → `401` без раскрытия причины; секрет не задан → `500`. После "
        'успешной авторизации любой кривой payload → `200 {"status":"ignored",...}` '
        "(Adapty не ретраит); `5xx` — только при реальном сбое БД. Идемпотентно по event_id."
    ),
    responses=problem_responses(401),
)
async def adapty_webhook(
    request: Request,
    session: SessionDep,
) -> AdaptyWebhookResponse:
    """Приём событий Adapty (S2S). Bearer-авторизация ВСЕГДА до парсинга тела (ADR-027 §A).

    Заголовок Authorization читается из request.headers напрямую (как и тело через
    request.body()), а НЕ объявляется Header-параметром: иначе FastAPI вывел бы Authorization
    header-параметром операции в публичной OpenAPI-схеме (он представлен security-схемой).

    Секрет не задан → 500; неверный/нет токена → 401. После авторизации: пустое тело/не-JSON →
    200 ignored; прочий кривой payload → 200 ignored (reason/event_type); применено → applied;
    повтор → duplicate. 5xx — только реальный сбой БД (WebhookProcessingError всплывает).
    """
    settings = get_settings()
    secret = settings.adapty_webhook_secret.get_secret_value()

    # 1. Мисконфигурация сервера (секрет не задан) → 500. ДО парсинга тела.
    if not secret:
        logger.error("billing_webhook_misconfigured")
        raise _misconfigured()

    # 2. Авторизация constant-time ДО чтения тела (ADR-027 §A). Секрет НЕ логируем.
    token = _extract_bearer_token(request.headers.get("Authorization"))
    if token is None or not hmac.compare_digest(token, secret):
        logger.warning("billing_webhook_unauthorized")
        raise unauthorized("Webhook authorization failed.")

    # 3. Тело читаем только после авторизации. Always-200-on-bad-input (ADR-027 §B).
    raw_body = await request.body()
    if not raw_body:
        return _outcome_response(WebhookResult(outcome=WebhookOutcome.IGNORED, reason="empty_body"))
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return _outcome_response(
            WebhookResult(outcome=WebhookOutcome.IGNORED, reason="invalid_json")
        )

    # process_webhook сам управляет транзакцией (insert ledger + apply + token-grant).
    # WebhookProcessingError всплывает как 5xx (реальный сбой БД, Adapty retry) — НЕ ловим.
    result = await process_webhook(session, payload)
    logger.info("billing_webhook_outcome", extra={"outcome": result.outcome.value})
    return _outcome_response(result)


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
