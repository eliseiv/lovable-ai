"""Router /admin — операторская плоскость (ADR-021, docs/modules/admin/02-api-contracts.md).

Все эндпоинты:
  - защищены require_admin (заголовок X-Admin-Key, НЕ Bearer);
  - скрыты из публичной схемы (include_in_schema=False на роутере) — как /metrics/healthz;
  - возвращают RFC-7807 при ошибках.

POST /admin/login-as                — выпустить пользовательский Bearer за user_id (без Apple).
POST /admin/users/{user_id}/credits — начислить/скорректировать бонус-генерации (идемпотентно).
GET  /admin/users/{user_id}         — баланс бонус-генераций + квота пользователя.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, status

from app.api.dependencies import RequireAdmin, SessionDep
from app.api.errors import not_found, problem_responses
from app.schemas.api import (
    AdminGrantCreditsRequest,
    AdminGrantCreditsResponse,
    AdminLoginAsRequest,
    AdminLoginAsResponse,
    AdminUserQuota,
    AdminUserResponse,
)
from app.services import admin_service, billing_service

# include_in_schema=False на уровне роутера → ни один /v1/admin/* не попадает в /openapi.json
# и /docs (ADR-021 §C, docs/admin §4). Кастомный openapi() в main.py итерирует app.routes —
# роуты с include_in_schema=False исключаются из схемы автоматически, B.7-чистота сохраняется.
router = APIRouter(prefix="/admin", include_in_schema=False)


@router.post(
    "/login-as",
    response_model=AdminLoginAsResponse,
    status_code=status.HTTP_200_OK,
    summary="Выпустить пользовательский ключ за указанного пользователя",
    responses=problem_responses(401, 422),
)
async def login_as(
    body: AdminLoginAsRequest,
    session: SessionDep,
    _admin: RequireAdmin,
) -> AdminLoginAsResponse:
    """Выпускает свежий Bearer за user_id (создаёт пользователя без Apple, если его нет).

    user_id задан и есть → токен за него; задан и нет → создать с этим идентификатором;
    опущен → сгенерировать новый и создать. Ключ возвращается один раз.
    """
    result = await admin_service.login_as(
        session,
        user_id=body.user_id,
        device_label=body.device_label,
    )
    return AdminLoginAsResponse(
        api_key=result.api_key,
        token_id=result.token_id,
        user_id=result.user_id,
    )


@router.post(
    "/users/{user_id}/credits",
    response_model=AdminGrantCreditsResponse,
    status_code=status.HTTP_200_OK,
    summary="Начислить или скорректировать бонус-генерации пользователю",
    responses=problem_responses(401, 404, 409, 422),
)
async def grant_credits(
    user_id: str,
    body: AdminGrantCreditsRequest,
    session: SessionDep,
    _admin: RequireAdmin,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> AdminGrantCreditsResponse:
    """Начисляет (amount>0) или корректирует (amount<0) бонус-генерации.

    Атомарно: запись в журнал начислений + обновление баланса. amount==0 → 422; коррекция,
    уводящая баланс ниже нуля → 409; нет пользователя → 404. Повтор с тем же Idempotency-Key
    → no-op (возврат текущего баланса).
    """
    result = await admin_service.grant_credits(
        session,
        user_id=user_id,
        amount=body.amount,
        reason=body.reason,
        idempotency_key=idempotency_key,
    )
    return AdminGrantCreditsResponse(
        user_id=result.user_id,
        amount_applied=result.amount_applied,
        bonus_generations_balance=result.bonus_generations_balance,
    )


@router.get(
    "/users/{user_id}",
    response_model=AdminUserResponse,
    summary="Баланс бонус-генераций и квота пользователя",
    responses=problem_responses(401, 404),
)
async def get_user(
    user_id: str,
    session: SessionDep,
    _admin: RequireAdmin,
) -> AdminUserResponse:
    """Возвращает баланс бонус-генераций + квоту указанного пользователя (нет → 404)."""
    user = await admin_service.get_user(session, user_id)
    if user is None:
        raise not_found("User not found.")

    snapshot = await billing_service.build_billing_snapshot(session, user)
    q = snapshot.quota
    return AdminUserResponse(
        user_id=user.id,
        access_level=snapshot.access_level,
        status=snapshot.status,
        period=snapshot.period,
        bonus_generations_balance=user.bonus_generations_balance,
        quota=AdminUserQuota(
            monthly_generations=q.monthly_generations,
            generations_used=q.generations_used,
            generations_remaining=q.generations_remaining,
            monthly_edits=q.monthly_edits,
            edits_used=q.edits_used,
            edits_remaining=q.edits_remaining,
            max_concurrent_jobs=q.max_concurrent_jobs,
            active_jobs=q.active_jobs,
            max_projects=q.max_projects,
            projects_used=q.projects_used,
        ),
    )
