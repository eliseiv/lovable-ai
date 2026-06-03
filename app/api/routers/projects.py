"""Router /projects (docs/modules/api/02-api-contracts.md).

POST /projects (202), GET /projects (200), GET /projects/{pid} (200).
Idempotency-Key обязателен на POST. Cross-tenant: фильтр по user_id, 404 если не свой.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, status

from app.api.dependencies import CurrentUser, SessionDep
from app.api.errors import not_found, unprocessable
from app.schemas.api import (
    CreateEditRequest,
    CreateEditResponse,
    CreateProjectRequest,
    CreateProjectResponse,
    DeleteProjectResponse,
    ProjectListResponse,
    ProjectOut,
    RevisionOut,
    RevisionsListResponse,
    RollbackResponse,
)
from app.services import edit_service, project_service

router = APIRouter(prefix="/projects", tags=["projects"])


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CreateProjectResponse,
    # Sprint 3.5: quota-gate billing энфорсится в project_service.create_project_with_job
    # ПОСЛЕ idempotency-резолва (docs/billing/03 §4): идемпотентный replay того же
    # Idempotency-Key обходит gate (не новая генерация → не 402), реальный новый запрос
    # → 402 (RFC-7807) с reason. Gate перенесён из FastAPI-dependency внутрь сервиса,
    # чтобы стать idempotency-aware (фикс регрессии S3.5: replay free-юзера ловил 402).
    # 429 остаётся только за rate-limit (60/min), не за concurrency (каноникализация S3.5).
)
async def create_project(
    body: CreateProjectRequest,
    user: CurrentUser,
    session: SessionDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> CreateProjectResponse:
    if not idempotency_key:
        raise unprocessable("Idempotency-Key header is required.")
    result = await project_service.create_project_with_job(
        session,
        user_id=user.id,
        prompt=body.prompt,
        title=body.title,
        idempotency_key=idempotency_key,
    )
    return CreateProjectResponse(project_id=result.project_id, job_id=result.job_id)


@router.get("", response_model=ProjectListResponse)
async def list_projects(user: CurrentUser, session: SessionDep) -> ProjectListResponse:
    projects = await project_service.list_projects(session, user.id)
    # TD-008 (ADR-016): batched live_url — ОДИН запрос по всем project_id вместо N+1
    # (прежде get_project_live_url в цикле). Проекты без active-деплоя → live_url=None.
    live_urls = await project_service.get_live_urls_for_projects(
        session, [project.id for project in projects]
    )
    out: list[ProjectOut] = [
        ProjectOut(
            id=project.id,
            title=project.title,
            prompt=project.prompt,
            current_revision_id=project.current_revision_id,
            live_url=live_urls.get(project.id),
            created_at=project.created_at,
        )
        for project in projects
    ]
    return ProjectListResponse(projects=out)


@router.delete(
    "/{project_id}",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=DeleteProjectResponse,
)
async def delete_project(
    project_id: str, user: CurrentUser, session: SessionDep
) -> DeleteProjectResponse:
    """DELETE /projects/{pid} (Sprint 4, ADR-011): soft-delete + async GC (project.gc).

    202 Accepted (status=deleting): проект сразу soft-delete (deleted_at=now()) и исчезает
    из GET /projects; GC ресурсов (контейнеры/route/volume/S3/БД-каскад) — асинхронно.
    Cross-tenant: чужой/несуществующий/уже физически удалённый pid → 404 (не раскрываем
    существование). Идемпотентно: повторный DELETE уже удаляемого проекта → 202 (no-op путь).
    """
    deleted = await project_service.soft_delete_project(session, user.id, project_id)
    if not deleted:
        raise not_found("Project not found.")
    return DeleteProjectResponse(project_id=project_id, status="deleting")


@router.post(
    "/{project_id}/edits",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CreateEditResponse,
)
async def create_edit(
    project_id: str,
    body: CreateEditRequest,
    user: CurrentUser,
    session: SessionDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> CreateEditResponse:
    """POST /projects/{pid}/edits (Sprint 5, ADR-014): post-delivery правка → Agent 4 editor.

    Idempotency-Key обязателен. Гейтинг — отдельный лимит правок (quota_gate kind='edit'):
    402 reason ∈ {no_entitlement, edit_quota_exhausted, concurrency_limit}. Правка только
    над LIVE-сайтом → 409 иначе. Чужой/несуществующий pid → 404 (cross-tenant).
    """
    if not idempotency_key:
        raise unprocessable("Idempotency-Key header is required.")
    result = await edit_service.create_edit_job(
        session,
        user_id=user.id,
        project_id=project_id,
        instruction=body.instruction,
        idempotency_key=idempotency_key,
    )
    return CreateEditResponse(job_id=result.job_id)


@router.get("/{project_id}/revisions", response_model=RevisionsListResponse)
async def list_revisions(
    project_id: str, user: CurrentUser, session: SessionDep
) -> RevisionsListResponse:
    """История ревизий проекта (ADR-014). current_revision_id — активная good-ревизия.
    Чужой/несуществующий pid → 404 (cross-tenant).
    """
    project = await project_service.get_project(session, user.id, project_id)
    if project is None:
        raise not_found("Project not found.")
    revisions = await project_service.list_revisions(session, project_id)
    return RevisionsListResponse(
        current_revision_id=project.current_revision_id,
        revisions=[RevisionOut.model_validate(r) for r in revisions],
    )


@router.post(
    "/{project_id}/revisions/{revision_no}/rollback",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=RollbackResponse,
)
async def rollback_revision(
    project_id: str,
    revision_no: int,
    user: CurrentUser,
    session: SessionDep,
) -> RollbackResponse:
    """POST /projects/{pid}/revisions/{revision_no}/rollback (Sprint 5, ADR-014 §B).

    Откат на good-ревизию — re-deploy без новой генерации/правки (лимитом НЕ гейтится).
    202 → re-deploy асинхронный (Celery queue=build), прогресс через GET /jobs/{job_id}.
    Целевая ревизия не good/уже текущая → 409; нет такой revision_no → 404; чужой pid → 404.
    """
    result = await project_service.start_rollback(
        session, user_id=user.id, project_id=project_id, revision_no=revision_no
    )
    return RollbackResponse(job_id=result.job_id, target_revision_no=result.target_revision_no)


@router.get("/{project_id}", response_model=ProjectOut)
async def get_project(project_id: str, user: CurrentUser, session: SessionDep) -> ProjectOut:
    project = await project_service.get_project(session, user.id, project_id)
    if project is None:
        # Cross-tenant: не раскрываем существование чужого проекта.
        raise not_found("Project not found.")
    live_url = await project_service.get_project_live_url(session, project.id)
    return ProjectOut(
        id=project.id,
        title=project.title,
        prompt=project.prompt,
        current_revision_id=project.current_revision_id,
        live_url=live_url,
        created_at=project.created_at,
    )
