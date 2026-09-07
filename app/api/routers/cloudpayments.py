"""Router /billing/cloudpayments — RU-оплата через платёжный агрегатор (ADR-052).

Два входа с принципиально разной моделью доверия:

- **`POST /checkout`** — обычный клиентский вызов с пользовательским Bearer. `user_id` для
  провайдера берётся ТОЛЬКО из аутентифицированного пользователя, никогда из тела запроса:
  иначе платёж уходит на чужой аккаунт и не находится при начислении.
- **`POST /webhook`** — server-to-server callback провайдера. Он приходит БЕЗ подписи и без
  авторизации, поэтому телу не верят: оно лишь триггер сверки. Тело читается сырым (без
  Pydantic-модели) — кривой callback обязан получить `200`, а не `422`, иначе провайдер будет
  бесконечно ретраить заведомо неисправимое. `500` отдаётся только там, где повтор осмыслен:
  канал не сконфигурирован или сверка недоступна.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from app.api.dependencies import CurrentUser, SessionDep
from app.api.errors import ProblemException, problem_responses
from app.billing import cloudpayments
from app.core.config import get_settings
from app.core.logging import get_logger
from app.schemas.api import (
    CloudPaymentsCheckoutRequest,
    CloudPaymentsCheckoutResponse,
    CloudPaymentsWebhookResponse,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/billing/cloudpayments", tags=["Биллинг"])


@router.post(
    "/checkout",
    response_model=CloudPaymentsCheckoutResponse,
    summary="Создать ссылку на оплату (Россия)",
    description=(
        "Создаёт платёжную ссылку для оплаты российской картой и возвращает `payment_url` — "
        "откройте его в браузере. Оплата привязывается к текущему пользователю автоматически. "
        "Продукт (`product_id`) — подписка или пакет токенов; после успешной оплаты начисление "
        "выполняется автоматически, отдельного подтверждения от приложения не требуется.\n\n"
        "`503` — способ оплаты недоступен на этой инсталляции; `502` — платёжный провайдер "
        "временно недоступен. Требуется заголовок `Authorization: Bearer <api-key>`."
    ),
    responses=problem_responses(401, 429, 502, 503),
)
async def cloudpayments_checkout(
    body: CloudPaymentsCheckoutRequest,
    user: CurrentUser,
) -> CloudPaymentsCheckoutResponse:
    """Ссылка на оплату для текущего пользователя. Не сконфигурировано → 503."""
    settings = get_settings()
    try:
        link = await cloudpayments.create_payment_link(
            settings,
            user_id=user.id,
            product_id=body.product_id,
            customer_email=body.customer_email,
        )
    except cloudpayments.CloudPaymentsNotConfigured as exc:
        raise ProblemException(
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
            title="Service Unavailable",
            detail="Payment method is not available on this installation.",
            problem_type="payment-method-unavailable",
        ) from exc
    except cloudpayments.CloudPaymentsUpstreamError as exc:
        raise ProblemException(
            status=status.HTTP_502_BAD_GATEWAY,
            title="Bad Gateway",
            detail="Payment provider is temporarily unavailable.",
            problem_type="payment-provider-unavailable",
        ) from exc

    return CloudPaymentsCheckoutResponse(
        payment_id=link.payment_id,
        payment_url=link.payment_url,
        status=link.status,
        expires_at=link.expires_at,
    )


@router.post(
    "/webhook",
    response_model=CloudPaymentsWebhookResponse,
    summary="Приём платежа (server-to-server)",
    description=(
        "Служебный эндпоинт: вызывается платёжным провайдером, **не** клиентом. Событие — "
        "только сигнал: начисление выполняется после сверки платежей через API провайдера, "
        "поэтому содержимое запроса на сумму начисления не влияет. Любое разобранное событие "
        'получает `200` с телом `{"code": 0}` (провайдер не ретраит). `500` — канал не '
        "сконфигурирован или сверка недоступна: тогда повторная доставка уместна и приводит к "
        "штатной обработке (идемпотентно по идентификатору платежа)."
    ),
    include_in_schema=False,
)
async def cloudpayments_webhook(request: Request, session: SessionDep) -> JSONResponse:
    """Callback провайдера. Всегда 200 `{"code": 0}`, кроме случаев, где нужен ретрай."""
    raw = await request.body()
    settings = get_settings()
    try:
        outcome = await cloudpayments.handle_webhook(session, settings, raw)
    except cloudpayments.CloudPaymentsNotConfigured:
        # 500 намеренно: канал выключен мисконфигом, повтор после починки начислит платёж.
        logger.warning("cloudpayments_webhook_not_configured")
        return JSONResponse({"code": 13}, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
    except cloudpayments.CloudPaymentsUnavailable:
        logger.warning("cloudpayments_verification_unavailable")
        return JSONResponse({"code": 13}, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

    logger.info(
        "cloudpayments_webhook_outcome",
        extra={"result": outcome.result, "reason": outcome.reason, "credited": outcome.credited},
    )
    return JSONResponse({"code": 0}, status_code=status.HTTP_200_OK)
