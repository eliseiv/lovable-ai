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
from app.api.errors import unauthorized
from app.billing import entitlements, usage
from app.billing.adapty_client import verify_webhook_signature
from app.billing.subscription_state import (
    DEFAULT_ACCESS_LEVEL,
    STATUS_ACTIVE,
    get_subscription,
)
from app.billing.webhook_handler import (
    WebhookProcessingError,
    process_webhook,
)
from app.core.config import get_settings
from app.core.logging import get_logger
from app.schemas.api import BillingMeResponse, BillingQuota

logger = get_logger(__name__)

router = APIRouter(prefix="/billing", tags=["billing"])

# Заголовок подписи вебхука Adapty (HMAC-SHA256 raw_body). Имя — по конфигурации Adapty
# webhook v2; берём канонический header. Невалидно/отсутствует → 401 без раскрытия.
_SIGNATURE_HEADER = "adapty-signature"


@router.post("/webhook/adapty", status_code=status.HTTP_200_OK)
async def adapty_webhook(
    request: Request,
    session: SessionDep,
    signature: Annotated[str | None, Header(alias=_SIGNATURE_HEADER)] = None,
) -> Response:
    """Вебхук Adapty (S2S, НЕ Bearer). Подпись валидна → обработка; иначе 401.

    Идемпотентность по adapty_event_id (200 no-op на повтор). Неизвестный customer_user_id
    → 200 (событие сохранено, не потеряно). Внутренняя ошибка после валидной подписи → 5xx
    (Adapty повторит доставку).
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


@router.get("/me", response_model=BillingMeResponse)
async def billing_me(user: CurrentUser, session: SessionDep) -> BillingMeResponse:
    """Текущий entitlement + остаток квоты (docs §2). Lazy-ресинк при протухшем кэше.

    Нет подписки → free/active с квотой free-тарифа. generations_remaining = max(0, ...).
    """
    # resolve_entitlement выполняет lazy-ресинк при протухании (fail-open на кэш).
    ent = await entitlements.resolve_entitlement(session, user.id)
    quota = await entitlements.get_plan_quota(session, ent.access_level)

    period = usage.current_period()
    generations_used = await usage.get_usage(session, user.id, period)
    edits_used = await usage.get_edit_usage(session, user.id, period)
    active_jobs = await entitlements.count_active_jobs(session, user.id)
    projects_used = await entitlements.count_projects(session, user.id)
    max_concurrent = await entitlements.resolve_max_concurrent_jobs(session, user.id)

    if quota is not None:
        monthly_generations = quota.monthly_generations
        max_projects = quota.max_projects
        monthly_edits = quota.monthly_edits
    else:
        # plan_quotas не сидирован — деградируем к нулевому потолку (явный сигнал, не падаем).
        monthly_generations = 0
        max_projects = None
        monthly_edits = None

    # edits_remaining: None при безлимите (Pro, monthly_edits=NULL), иначе max(0, лимит-исп.).
    edits_remaining = None if monthly_edits is None else max(0, monthly_edits - edits_used)

    sub = await get_subscription(session, user.id)
    access_level = sub.access_level if sub is not None else DEFAULT_ACCESS_LEVEL
    sub_status = sub.status if sub is not None else STATUS_ACTIVE

    return BillingMeResponse(
        access_level=access_level,
        status=sub_status,
        period=period,
        quota=BillingQuota(
            monthly_generations=monthly_generations,
            generations_used=generations_used,
            generations_remaining=max(0, monthly_generations - generations_used),
            monthly_edits=monthly_edits,
            edits_used=edits_used,
            edits_remaining=edits_remaining,
            max_concurrent_jobs=max_concurrent,
            active_jobs=active_jobs,
            max_projects=max_projects,
            projects_used=projects_used,
        ),
    )
