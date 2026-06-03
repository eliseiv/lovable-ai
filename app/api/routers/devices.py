"""Router /devices (Sprint 5, ADR-013, docs/modules/api/02-api-contracts.md → /devices).

POST /devices (регистрация/upsert APNs device token, 201),
DELETE /devices/{apns_token} (отписка, 204). Bearer. Cross-tenant: выборка по user_id.
Отправка push — Celery notify.apns_push (модуль notify), здесь только регистрация.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from app.api.dependencies import CurrentUser, SessionDep
from app.api.errors import not_found, problem_responses, unprocessable
from app.notify import device_service
from app.schemas.api import RegisterDeviceRequest, RegisterDeviceResponse

router = APIRouter(prefix="/devices", tags=["Устройства"])


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=RegisterDeviceResponse,
    summary="Зарегистрировать устройство для push",
    description=(
        "Регистрирует устройство для получения push-уведомлений о статусе генерации. "
        "Повторная регистрация того же токена идемпотентна (повторно активирует устройство). "
        "Некорректные `platform` или `environment` → `422`. Требуется заголовок "
        "`Authorization: Bearer <api-key>`."
    ),
    responses=problem_responses(401, 422, 429),
)
async def register_device(
    body: RegisterDeviceRequest,
    user: CurrentUser,
    session: SessionDep,
) -> RegisterDeviceResponse:
    """Регистрирует устройство для push. Повтор того же токена идемпотентен. Некорректные
    значения → 422.
    """
    error = device_service.validate_registration(body.platform, body.environment)
    if error is not None:
        raise unprocessable(error)
    device_id = await device_service.register_device(
        session,
        user_id=user.id,
        apns_token=body.apns_token,
        platform=body.platform,
        environment=body.environment,
    )
    return RegisterDeviceResponse(id=device_id)


@router.delete(
    "/{apns_token}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Отписать устройство от push",
    description=(
        "Отписывает устройство от push-уведомлений (выход или смена устройства). Чужой или "
        "несуществующий токен → `404`. Операция идемпотентна. Требуется заголовок "
        "`Authorization: Bearer <api-key>`."
    ),
    responses=problem_responses(401, 404, 429),
)
async def unregister_device(
    apns_token: str,
    user: CurrentUser,
    session: SessionDep,
) -> Response:
    """Отписывает устройство от push. Чужой/несуществующий токен → 404. Идемпотентно."""
    ok = await device_service.unregister_device(session, user_id=user.id, apns_token=apns_token)
    if not ok:
        raise not_found("Device not found.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
