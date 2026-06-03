"""Router /projects (docs/modules/api/02-api-contracts.md).

POST /projects (202), GET /projects (200), GET /projects/{pid} (200).
Idempotency-Key обязателен на POST. Cross-tenant: фильтр по user_id, 404 если не свой.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, status

from app.api.dependencies import CurrentUser, SessionDep
from app.api.errors import not_found, problem_responses, unprocessable
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

router = APIRouter(prefix="/projects")


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CreateProjectResponse,
    tags=["Проекты"],
    summary="Создать проект и запустить генерацию",
    description=(
        "Создаёт новый проект и запускает асинхронную генерацию сайта по текстовому промту. "
        "Обязателен заголовок `Idempotency-Key` (защищает от повторного создания при "
        "повторной отправке запроса). В ответ возвращаются идентификаторы проекта "
        "(`project_id`) и задачи генерации (`job_id`); статус отслеживается через "
        "`GET /jobs/{jid}` или поток событий.\n\n"
        "Если активная подписка отсутствует или исчерпана квота, возвращается `402` "
        "(`application/problem+json`) с полем `reason` "
        "(`no_entitlement` / `quota_exhausted` / `project_limit` / `concurrency_limit`) и "
        "`required_entitlement`. Требуется заголовок `Authorization: Bearer <api-key>`."
    ),
    responses=problem_responses(401, 402, 422, 429),
    # Quota-gate энфорсится в сервисе ПОСЛЕ idempotency-резолва: идемпотентный replay того же
    # Idempotency-Key обходит gate (не новая генерация → не 402), реальный новый запрос → 402
    # с reason. 429 остаётся только за rate-limit (60/min), не за concurrency.
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


@router.get(
    "",
    response_model=ProjectListResponse,
    tags=["Проекты"],
    summary="Список проектов",
    description=(
        "Возвращает список проектов текущего пользователя. У опубликованных проектов "
        "заполнено поле `live_url` (адрес работающего сайта). Удалённые проекты в список "
        "не включаются. Требуется заголовок `Authorization: Bearer <api-key>`."
    ),
    responses=problem_responses(401, 429),
)
async def list_projects(user: CurrentUser, session: SessionDep) -> ProjectListResponse:
    projects = await project_service.list_projects(session, user.id)
    # Batched live_url — ОДИН запрос по всем project_id вместо N+1. Проекты без active-деплоя
    # → live_url=None.
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
    tags=["Проекты"],
    summary="Удалить проект",
    description=(
        "Удаляет проект и запускает фоновую очистку всех его ресурсов. Проект сразу "
        "исчезает из списка (`GET /projects`), полная очистка выполняется асинхронно "
        "(в ответе `status` = `deleting`). Чужой, несуществующий или уже удалённый проект "
        "→ `404`. Операция идемпотентна. Требуется заголовок "
        "`Authorization: Bearer <api-key>`."
    ),
    responses=problem_responses(401, 404, 429),
)
async def delete_project(
    project_id: str, user: CurrentUser, session: SessionDep
) -> DeleteProjectResponse:
    """Удаляет проект (сразу скрывает из списка) и запускает фоновую очистку ресурсов.

    Чужой/несуществующий/уже удалённый проект → 404. Идемпотентно.
    """
    deleted = await project_service.soft_delete_project(session, user.id, project_id)
    if not deleted:
        raise not_found("Project not found.")
    return DeleteProjectResponse(project_id=project_id, status="deleting")


@router.post(
    "/{project_id}/edits",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CreateEditResponse,
    tags=["Правки и ревизии"],
    summary="Внести правку в опубликованный сайт",
    description=(
        "Создаёт задачу правки уже опубликованного сайта по текстовой инструкции. "
        "Обязателен заголовок `Idempotency-Key`. В ответ возвращается идентификатор задачи "
        "(`job_id`), статус отслеживается через `GET /jobs/{jid}` или поток событий.\n\n"
        "Правка возможна только над работающим (опубликованным) сайтом — иначе `409`. "
        "Действует отдельный лимит правок: при отсутствии подписки или исчерпании лимита "
        "возвращается `402` с полем `reason` "
        "(`no_entitlement` / `edit_quota_exhausted` / `concurrency_limit`). Чужой или "
        "несуществующий проект → `404`. Требуется заголовок "
        "`Authorization: Bearer <api-key>`."
    ),
    responses=problem_responses(401, 402, 404, 409, 422, 429),
)
async def create_edit(
    project_id: str,
    body: CreateEditRequest,
    user: CurrentUser,
    session: SessionDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> CreateEditResponse:
    """Создаёт задачу правки опубликованного сайта по инструкции. Возвращает job_id.

    Idempotency-Key обязателен. Правка только над работающим сайтом → иначе 409. Лимит
    исчерпан/нет подписки → 402. Чужой/несуществующий проект → 404.
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


@router.get(
    "/{project_id}/revisions",
    response_model=RevisionsListResponse,
    tags=["Правки и ревизии"],
    summary="История ревизий проекта",
    description=(
        "Возвращает историю ревизий проекта. Поле `current_revision_id` указывает текущую "
        "активную ревизию (на неё можно откатиться). Чужой или несуществующий проект → "
        "`404`. Требуется заголовок `Authorization: Bearer <api-key>`."
    ),
    responses=problem_responses(401, 404, 429),
)
async def list_revisions(
    project_id: str, user: CurrentUser, session: SessionDep
) -> RevisionsListResponse:
    """Возвращает историю ревизий проекта. Чужой/несуществующий проект → 404."""
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
    tags=["Правки и ревизии"],
    summary="Откатиться на ревизию",
    description=(
        "Откатывает сайт на ранее опубликованную ревизию (повторная публикация без новой "
        "генерации или правки; лимитами правок/генераций не учитывается). В ответ "
        "возвращается идентификатор задачи (`job_id`) и номер целевой ревизии; прогресс "
        "отслеживается через `GET /jobs/{jid}`.\n\n"
        "Целевая ревизия должна быть успешно опубликованной и не текущей — иначе `409`. "
        "Нет ревизии с таким номером → `404`; чужой или несуществующий проект → `404`. "
        "Требуется заголовок `Authorization: Bearer <api-key>`."
    ),
    responses=problem_responses(401, 404, 409, 429),
)
async def rollback_revision(
    project_id: str,
    revision_no: int,
    user: CurrentUser,
    session: SessionDep,
) -> RollbackResponse:
    """Откатывает сайт на ранее опубликованную ревизию. Возвращает job_id.

    Целевая ревизия не опубликована/уже текущая → 409; нет такой ревизии → 404; чужой
    проект → 404.
    """
    result = await project_service.start_rollback(
        session, user_id=user.id, project_id=project_id, revision_no=revision_no
    )
    return RollbackResponse(job_id=result.job_id, target_revision_no=result.target_revision_no)


@router.get(
    "/{project_id}",
    response_model=ProjectOut,
    tags=["Проекты"],
    summary="Детали проекта",
    description=(
        "Возвращает детали проекта, включая адрес работающего сайта (`live_url`), если "
        "сайт опубликован. Чужой или несуществующий (в том числе удалённый) проект → "
        "`404`. Требуется заголовок `Authorization: Bearer <api-key>`."
    ),
    responses=problem_responses(401, 404, 429),
)
async def get_project(project_id: str, user: CurrentUser, session: SessionDep) -> ProjectOut:
    project = await project_service.get_project(session, user.id, project_id)
    if project is None:
        # Не раскрываем существование чужого проекта.
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
