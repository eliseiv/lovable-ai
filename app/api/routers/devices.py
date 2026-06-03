"""Router /devices (Sprint 5, ADR-013, docs/modules/api/02-api-contracts.md → /devices).

POST /devices (регистрация/upsert APNs device token, 201),
DELETE /devices/{apns_token} (отписка, 204). Bearer. Cross-tenant: выборка по user_id.
Отправка push — Celery notify.apns_push (модуль notify), здесь только регистрация.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from app.api.dependencies import CurrentUser, SessionDep
from app.api.errors import not_found, unprocessable
from app.notify import device_service
from app.schemas.api import RegisterDeviceRequest, RegisterDeviceResponse

router = APIRouter(prefix="/devices", tags=["devices"])


@router.post("", status_code=status.HTTP_201_CREATED, response_model=RegisterDeviceResponse)
async def register_device(
    body: RegisterDeviceRequest,
    user: CurrentUser,
    session: SessionDep,
) -> RegisterDeviceResponse:
    """Регистрация APNs device token. Upsert по (user_id, apns_token) — идемпотентно
    (повтор сбрасывает invalidated_at). Невалидный platform/environment → 422.
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


@router.delete("/{apns_token}", status_code=status.HTTP_204_NO_CONTENT)
async def unregister_device(
    apns_token: str,
    user: CurrentUser,
    session: SessionDep,
) -> Response:
    """Отписка устройства (logout/смена). Чужой/несуществующий → 404 (cross-tenant).
    Идемпотентно: повтор → 204/404.
    """
    ok = await device_service.unregister_device(session, user_id=user.id, apns_token=apns_token)
    if not ok:
        raise not_found("Device not found.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
