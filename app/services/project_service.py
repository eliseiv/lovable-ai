"""Бизнес-операции project/job (docs/modules/api/03-architecture.md).

Создание проекта+джобы с идемпотентностью, постановка стартовой задачи в очередь.
API не делает LLM/сборку инлайн — только Postgres + Celery.enqueue.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import conflict, not_found
from app.billing.quota_gate import enforce_quota_gate
from app.core.config import get_settings
from app.core.ids import new_job_id, new_project_id
from app.core.logging import get_logger
from app.db.enums import JobState
from app.db.models import GenerationJob, Project, Revision, SiteDeployment
from app.pipeline.dispatcher import dispatch_for_state
from app.pipeline.events import record_event
from app.services.attachments_service import ValidatedImage, persist_images
from app.storage.s3 import get_storage

logger = get_logger(__name__)


@dataclass(frozen=True)
class CreatedProject:
    project_id: str
    job_id: str
    created: bool  # False, если вернули существующую джобу по idempotency-key


async def create_project_with_job(
    session: AsyncSession,
    *,
    user_id: str,
    prompt: str,
    title: str | None,
    idempotency_key: str,
    images: list[ValidatedImage] | None = None,
) -> CreatedProject:
    """Создаёт project + generation_job (CREATED) и ставит task_interview.

    Идемпотентность: повтор с тем же (user_id, idempotency_key) → существующая джоба.

    Порядок (idempotency-aware quota-gate, docs/modules/billing/03 §4):
      1. idempotency-резолв (_find_existing_job) — ДО любых quota-проверок;
      2. при совпадении → существующая джоба (created=False), БЕЗ quota-gate:
         идемпотентный replay не считается новой генерацией → не против cap/quota, не 402;
      3. реальный новый запрос (нет совпадения) → enforce_quota_gate ДО постановки задачи:
         превышение entitlement/projects/concurrency/quota → 402 (RFC-7807) с reason.
    Это единая idempotency-aware точка энфорса (S3.5-gate перенесён из FastAPI-dependency
    внутрь сервиса ПОСЛЕ idempotency-чека — иначе replay free-юзера ложно ловил 402).

    `images` (ADR-034 §D9): валидированные приложенные изображения пишутся в S3 + строки
    attachments ТОЛЬКО на реально новой джобе (created=True), в той же транзакции — replay
    того же Idempotency-Key (created=False) сюда не доходит, повторных строк/объектов нет.
    """
    settings = get_settings()

    existing_job = await _find_existing_job(session, user_id, idempotency_key)
    if existing_job is not None:
        # Идемпотентный replay: возвращаем существующую джобу без quota-gate и БЕЗ повторной
        # записи attachments/S3 (ADR-034 §D9).
        return CreatedProject(
            project_id=existing_job.project_id,
            job_id=existing_job.id,
            created=False,
        )

    # Реальный новый запрос — энфорс quota-gate ДО постановки задачи (docs/billing/03 §4):
    # entitlement / max_projects / concurrency / monthly_generations. Нарушение → 402
    # (RFC-7807, reason). Каноникализация S3.5: concurrency → 402 reason=concurrency_limit,
    # не 429. Единая точка cap-энфорса; idempotency-replay сюда не доходит (вернулся выше).
    await enforce_quota_gate(session, user_id, check_project_limit=True)

    project = Project(
        id=new_project_id(),
        user_id=user_id,
        prompt=prompt,
        title=title,
    )
    job = GenerationJob(
        id=new_job_id(),
        project_id=project.id,
        user_id=user_id,
        state=JobState.CREATED,
        kind="generation",
        idempotency_key=idempotency_key,
        max_fix_attempts=settings.max_fix_attempts,
        budget_usd=settings.job_budget_usd,
        # Гард (c) wall-clock: deadline = created_at + JOB_WALL_CLOCK_BUDGET_S
        # (docs §C(c): в S2 всегда проставляется). created_at сервер проставит сам;
        # для детерминизма дедлайна берём текущий момент создания.
        wall_clock_deadline=datetime.now(UTC) + timedelta(seconds=settings.job_wall_clock_budget_s),
    )
    session.add(project)
    session.add(job)
    # ADR-034 §D4/§D9: запись изображений (S3 + строки attachments) на новой джобе, в той же
    # транзакции, что project/job — replay (created=False) сюда не доходит → не дублируется.
    if images:
        # FK-порядок parent-before-child: Attachment имеет column-level FK на projects/
        # generation_jobs БЕЗ ORM relationship() (в отличие от JobEvent/Question/Answer),
        # поэтому UoW-сортировка INSERT не гарантирует материализацию project/job до
        # attachments. Явный flush детерминированно вставляет parent-строки ДО persist_images,
        # иначе любой POST с изображениями падает ForeignKeyViolation (fk_attachments_*).
        await session.flush()
        await persist_images(
            session, get_storage(), project_id=project.id, job_id=job.id, images=images
        )
    await record_event(session, job.id, "job_created", to_state=JobState.CREATED.value)
    try:
        await session.commit()
    except IntegrityError:
        # Гонка двух конкурентных POST /projects с одинаковым (user_id, idempotency_key):
        # партиальный UNIQUE uq_generation_jobs_idempotency отклонил второй INSERT.
        # Откатываемся и идемпотентно возвращаем уже созданную джобу (created=False).
        await session.rollback()
        winner = await _find_existing_job(session, user_id, idempotency_key)
        if winner is None:
            # UNIQUE сработал по иной причине — пробрасываем (не маскируем баг).
            raise
        return CreatedProject(
            project_id=winner.project_id,
            job_id=winner.id,
            created=False,
        )

    # Постановка стартовой задачи в очередь (queue=llm).
    dispatch_for_state(job.id, JobState.CREATED)

    return CreatedProject(project_id=project.id, job_id=job.id, created=True)


async def _find_existing_job(
    session: AsyncSession, user_id: str, idempotency_key: str
) -> GenerationJob | None:
    """Существующая джоба по (user_id, idempotency_key) или None."""
    result = await session.execute(
        select(GenerationJob).where(
            GenerationJob.user_id == user_id,
            GenerationJob.idempotency_key == idempotency_key,
        )
    )
    return result.scalar_one_or_none()


async def list_projects(session: AsyncSession, user_id: str) -> list[Project]:
    # Soft-delete-фильтр (ADR-011): удаляемые/удалённые проекты исключены из листинга.
    result = await session.execute(
        select(Project)
        .where(Project.user_id == user_id, Project.deleted_at.is_(None))
        .order_by(Project.created_at.desc())
    )
    return list(result.scalars().all())


async def get_project(session: AsyncSession, user_id: str, project_id: str) -> Project | None:
    """Проект с tenant-фильтрацией по user_id (cross-tenant защита) + soft-delete-фильтр.

    deleted_at IS NULL (ADR-011): удаляемый/удалённый проект → None → 404 (как чужой/
    несуществующий — не раскрываем, что проект существовал).
    """
    result = await session.execute(
        select(Project).where(
            Project.id == project_id,
            Project.user_id == user_id,
            Project.deleted_at.is_(None),
        )
    )
    return result.scalar_one_or_none()


async def soft_delete_project(session: AsyncSession, user_id: str, project_id: str) -> bool:
    """Soft-delete проекта + постановка Celery project.gc (DELETE /projects/{pid}, ADR-011).

    Возвращает True, если проект найден (свой) — endpoint отдаёт 202 (status=deleting).
    False — проект чужой/несуществующий/физически удалён GC → endpoint отдаёт 404
    (cross-tenant: не раскрываем существование).

    Идемпотентность (ADR-011 §A): повторный DELETE уже-soft-deleted проекта (deleted_at
    уже выставлен) → True (202 no-op), GC переставляется заново (project.gc идемпотентен).
    Если строки уже физически нет (GC завершён) → False (404). deleted_at не перетирается
    на повторе — сохраняем исходный таймстамп удаления.
    """
    # Tenant-фильтр БЕЗ deleted_at-фильтра: нужно отличить «уже удаляемый» (свой,
    # deleted_at!=NULL → 202 no-op) от «чужой/нет строки» (→ 404).
    result = await session.execute(
        select(Project).where(Project.id == project_id, Project.user_id == user_id)
    )
    project = result.scalar_one_or_none()
    if project is None:
        return False

    if project.deleted_at is None:
        # Первый DELETE: транзакционно проставляем soft-delete-маркер.
        project.deleted_at = datetime.now(UTC)
        await session.commit()
        logger.info("project_soft_deleted", extra={"project_id": project_id, "user_id": user_id})
    else:
        # Повторный DELETE уже удаляемого проекта: deleted_at не трогаем (no-op путь).
        logger.info(
            "project_soft_delete_replay", extra={"project_id": project_id, "user_id": user_id}
        )

    # Постановка/переустановка Celery project.gc (queue=GC_QUEUE). Идемпотентна: project.gc
    # безопасно переисполняется (acks_late) — каждый шаг GC идемпотентен.
    _dispatch_project_gc(project_id)
    return True


def _dispatch_project_gc(project_id: str) -> None:
    """Ставит Celery project.gc в очередь GC_QUEUE (ленивый импорт — анти-цикл api↔workers)."""
    from app.deploy.project_gc import project_gc

    settings = get_settings()
    project_gc.apply_async(args=[project_id], queue=settings.gc_queue)


@dataclass(frozen=True)
class RollbackStarted:
    job_id: str
    target_revision_no: int


async def start_rollback(
    session: AsyncSession,
    *,
    user_id: str,
    project_id: str,
    revision_no: int,
) -> RollbackStarted:
    """Старт rollback на good-ревизию (ADR-014 §B, docs/api §rollback). Лимитом НЕ гейтится.

    Владение проектом (cross-tenant → 404). Целевая ревизия обязана быть is_good и
    принадлежать проекту: нет такой revision_no → 404; не good / уже current → 409.
    Создаёт re-deploy-джобу (kind=rollback, CREATED) и ставит Celery deploy.rollback_revision
    (queue=build). Прогресс — GET /jobs/{job_id}.
    """
    project = await get_project(session, user_id, project_id)
    if project is None:
        raise not_found("Project not found.")

    revision = await get_revision_by_no(session, project_id, revision_no)
    if revision is None:
        raise not_found("Revision not found for this project.")
    if not revision.is_good:
        raise conflict("Target revision is not a good revision.")
    if project.current_revision_id == revision.id:
        # Уже текущая — нечего откатывать (идемпотентный 409, docs/api §rollback).
        raise conflict("Target revision is already the current revision.")

    job = GenerationJob(
        id=new_job_id(),
        project_id=project.id,
        user_id=user_id,
        state=JobState.CREATED,
        kind="rollback",
    )
    session.add(job)
    await record_event(
        session,
        job.id,
        "rollback_requested",
        to_state=JobState.CREATED.value,
        payload={"target_revision_no": revision_no, "target_revision_id": revision.id},
    )
    await session.commit()

    _dispatch_rollback(job.id, project.id, revision.id)
    logger.info(
        "rollback_started",
        extra={"job_id": job.id, "project_id": project_id, "revision_no": revision_no},
    )
    return RollbackStarted(job_id=job.id, target_revision_no=revision_no)


def _dispatch_rollback(job_id: str, project_id: str, revision_id: str) -> None:
    """Ставит Celery deploy.rollback_revision (queue=build). Ленивый импорт — анти-цикл."""
    from app.deploy.rollback import rollback_revision

    rollback_revision.apply_async(args=[job_id, project_id, revision_id], queue="build")


async def list_revisions(session: AsyncSession, project_id: str) -> list[Revision]:
    """История ревизий проекта по revision_no (GET /projects/{pid}/revisions, ADR-014)."""
    result = await session.execute(
        select(Revision).where(Revision.project_id == project_id).order_by(Revision.revision_no)
    )
    return list(result.scalars().all())


async def get_revision_by_no(
    session: AsyncSession, project_id: str, revision_no: int
) -> Revision | None:
    """Ревизия проекта по revision_no (адрес rollback). None, если нет такой у проекта."""
    result = await session.execute(
        select(Revision).where(
            Revision.project_id == project_id, Revision.revision_no == revision_no
        )
    )
    return result.scalar_one_or_none()


async def get_project_live_url(session: AsyncSession, project_id: str) -> str | None:
    """Live URL активного деплоя проекта, если есть."""
    result = await session.execute(
        select(SiteDeployment.live_url)
        .where(
            SiteDeployment.project_id == project_id,
            SiteDeployment.status == "active",
        )
        .order_by(SiteDeployment.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_live_urls_for_projects(
    session: AsyncSession, project_ids: list[str]
) -> dict[str, str]:
    """Batched live_url активных деплоев по списку project_id — ОДИН запрос (TD-008, ADR-016).

    Закрывает N+1 в list_projects: вместо get_project_live_url в цикле — один
    `WHERE project_id IN (...) AND status='active'`. Возвращает {project_id: live_url};
    проекты без active-деплоя в словаре отсутствуют (router подставит None). При нескольких
    active-строках на проект (теоретически — берём самую свежую по created_at).
    """
    if not project_ids:
        return {}
    result = await session.execute(
        select(
            SiteDeployment.project_id,
            SiteDeployment.live_url,
            SiteDeployment.created_at,
        )
        .where(
            SiteDeployment.project_id.in_(project_ids),
            SiteDeployment.status == "active",
        )
        .order_by(SiteDeployment.created_at.asc())
    )
    # ASC + последовательная перезапись → итоговое значение = самый свежий active-деплой.
    live_by_project: dict[str, str] = {}
    for project_id, url, _created_at in result.all():
        if url is not None:
            live_by_project[project_id] = url
    return live_by_project
