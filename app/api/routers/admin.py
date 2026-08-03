"""Router /admin — операторская плоскость (ADR-021) + CRM Admin API (broad-crm v1).

Все эндпоинты защищены require_admin (X-Admin-Key). CRM-контракт: /v1/admin.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Header, Query, status

from app.api.dependencies import RequireAdmin, SessionDep
from app.api.errors import not_found, problem_responses
from app.db.models import User
from app.schemas.api import (
    AdminGrantCreditsRequest,
    AdminGrantCreditsResponse,
    AdminGrantSubscriptionRequest,
    AdminLoginAsRequest,
    AdminLoginAsResponse,
    AdminUserQuota,
    AdminUserResponse,
)
from app.schemas.crm_admin import (
    CrmAdjustTokensRequest,
    CrmAdjustTokensResponse,
    CrmGrantSubscriptionRequest,
    CrmGrantSubscriptionResponse,
    CrmPaymentListResponse,
    CrmProductListResponse,
    CrmRequestListResponse,
    CrmStatsResponse,
    CrmUserDetailResponse,
    CrmUserListResponse,
)
from app.services import admin_service, billing_service, crm_admin_service

router = APIRouter(prefix="/admin", tags=["Администрирование"])


async def _build_user_response(session: SessionDep, user: User) -> AdminUserResponse:
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


# ================================ legacy ADR-021 ================================


@router.post(
    "/login-as",
    response_model=AdminLoginAsResponse,
    status_code=status.HTTP_200_OK,
    summary="Выпустить пользовательский ключ за указанного пользователя",
    responses=problem_responses(401, 403, 422),
)
async def login_as(
    body: AdminLoginAsRequest,
    session: SessionDep,
    _admin: RequireAdmin,
) -> AdminLoginAsResponse:
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
    summary="Начислить или скорректировать бонус-генерации (идемпотентно)",
    responses=problem_responses(401, 403, 404, 409, 422),
)
async def grant_credits(
    user_id: str,
    body: AdminGrantCreditsRequest,
    session: SessionDep,
    _admin: RequireAdmin,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> AdminGrantCreditsResponse:
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
    "/users/{user_id}/quota",
    response_model=AdminUserResponse,
    summary="Баланс бонус-генераций и квота пользователя (внутренний снимок)",
    responses=problem_responses(401, 403, 404),
    include_in_schema=True,
)
async def get_user_quota(
    user_id: str,
    session: SessionDep,
    _admin: RequireAdmin,
) -> AdminUserResponse:
    user = await admin_service.get_user(session, user_id)
    if user is None:
        raise not_found("User not found.")
    return await _build_user_response(session, user)


@router.post(
    "/users/{user_id}/subscription/legacy",
    response_model=AdminUserResponse,
    status_code=status.HTTP_200_OK,
    summary="Выдать pro-подписку (legacy ADR-037, duration_days/expires_at)",
    responses=problem_responses(401, 403, 404, 422),
)
async def grant_subscription_legacy(
    user_id: str,
    body: AdminGrantSubscriptionRequest,
    session: SessionDep,
    _admin: RequireAdmin,
) -> AdminUserResponse:
    user = await admin_service.grant_subscription(
        session,
        user_id=user_id,
        duration_days=body.duration_days,
        expires_at=body.expires_at,
    )
    return await _build_user_response(session, user)


# ================================ CRM Admin API v1 ================================


@router.get(
    "/users",
    response_model=CrmUserListResponse,
    summary="CRM: список пользователей",
    responses=problem_responses(401, 403),
)
async def crm_list_users(
    session: SessionDep,
    _admin: RequireAdmin,
    limit: Annotated[int, Query(le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    search: Annotated[str | None, Query()] = None,
    date_from: Annotated[datetime | None, Query()] = None,
    date_to: Annotated[datetime | None, Query()] = None,
    is_paid: Annotated[bool | None, Query()] = None,
) -> CrmUserListResponse:
    return await crm_admin_service.list_users(
        session,
        limit=limit,
        offset=offset,
        search=search,
        date_from=date_from,
        date_to=date_to,
        is_paid=is_paid,
    )


@router.get(
    "/users/{user_id}",
    response_model=CrmUserDetailResponse,
    summary="CRM: карточка пользователя",
    responses=problem_responses(401, 403, 404),
)
async def crm_get_user(
    user_id: str,
    session: SessionDep,
    _admin: RequireAdmin,
) -> CrmUserDetailResponse:
    return await crm_admin_service.get_user_detail(session, user_id)


@router.get(
    "/users/{user_id}/payments",
    response_model=CrmPaymentListResponse,
    summary="CRM: история оплат пользователя",
    responses=problem_responses(401, 403, 404),
)
async def crm_list_payments(
    user_id: str,
    session: SessionDep,
    _admin: RequireAdmin,
    limit: Annotated[int, Query(le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> CrmPaymentListResponse:
    return await crm_admin_service.list_payments(session, user_id, limit=limit, offset=offset)


@router.get(
    "/users/{user_id}/requests",
    response_model=CrmRequestListResponse,
    summary="CRM: история запросов пользователя",
    responses=problem_responses(401, 403, 404),
)
async def crm_list_requests(
    user_id: str,
    session: SessionDep,
    _admin: RequireAdmin,
    limit: Annotated[int, Query(le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> CrmRequestListResponse:
    return await crm_admin_service.list_requests(session, user_id, limit=limit, offset=offset)


@router.get(
    "/stats",
    response_model=CrmStatsResponse,
    summary="CRM: сводная статистика",
    responses=problem_responses(401, 403),
)
async def crm_stats(
    session: SessionDep,
    _admin: RequireAdmin,
    date_from: Annotated[datetime | None, Query()] = None,
    date_to: Annotated[datetime | None, Query()] = None,
) -> CrmStatsResponse:
    return await crm_admin_service.get_stats(session, date_from=date_from, date_to=date_to)


@router.get(
    "/products",
    response_model=CrmProductListResponse,
    summary="CRM: доступные тарифы",
    responses=problem_responses(401, 403),
)
async def crm_products(_admin: RequireAdmin) -> CrmProductListResponse:
    return crm_admin_service.list_products()


@router.post(
    "/users/{user_id}/tokens",
    response_model=CrmAdjustTokensResponse,
    status_code=status.HTTP_200_OK,
    summary="CRM: начислить/списать токены (не идемпотентно)",
    responses=problem_responses(400, 401, 403, 404),
)
async def crm_adjust_tokens(
    user_id: str,
    body: CrmAdjustTokensRequest,
    session: SessionDep,
    _admin: RequireAdmin,
) -> CrmAdjustTokensResponse:
    uid, tokens = await crm_admin_service.adjust_tokens(
        session, user_id=user_id, amount=body.amount
    )
    return CrmAdjustTokensResponse(id=uid, tokens=tokens)


@router.post(
    "/users/{user_id}/subscription",
    response_model=CrmGrantSubscriptionResponse,
    status_code=status.HTTP_200_OK,
    summary="CRM: выдать/продлить подписку (идемпотентно по grant_id)",
    responses=problem_responses(400, 401, 403, 404, 422),
)
async def crm_grant_subscription(
    user_id: str,
    body: CrmGrantSubscriptionRequest,
    session: SessionDep,
    _admin: RequireAdmin,
) -> CrmGrantSubscriptionResponse:
    return await crm_admin_service.grant_subscription_crm(
        session,
        user_id=user_id,
        product_id=body.product_id,
        expires_in_days=body.expires_in_days,
        grant_id=body.grant_id,
    )
