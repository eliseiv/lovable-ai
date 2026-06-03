"""Post-delivery правки (Sprint 5, ADR-014 §A, docs/modules/api §POST /edits).

POST /projects/{pid}/edits → новая generation_jobs (kind=edit, CREATED) → task_edit
(Agent 4 как editor: спека + current good-ревизия + instruction → новое дерево → BUILDING
→ DEPLOYING → LIVE с новой ревизией). Та же FIXING-машинерия (4 гарда §C); неудача →
авто-rollback + FAILED(edit_failed_rolled_back).

Гейтинг (отдельный лимит правок): quota_gate kind='edit' (monthly_edits vs
edit_usage_counters) → 402 reason=edit_quota_exhausted; max_concurrent; max_projects НЕ
проверяется. Правка возможна только над LIVE-сайтом (иначе 409). Идемпотентность по
(user_id, idempotency_key) — тот же партиальный UNIQUE, что generation.
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
from app.core.ids import new_job_id
from app.core.logging import get_logger
from app.db.enums import JobState
from app.db.models import GenerationJob, Project, Revision
from app.pipeline.dispatcher import dispatch_for_state
from app.pipeline.events import record_event
from app.services import project_service

logger = get_logger(__name__)

# job_events-маркер: инструкция правки (читается task_edit; instruction-колонки в
# data-model нет — храним в append-only job_events, источник истины для task_edit).
EDIT_REQUESTED_EVENT = "edit_requested"


@dataclass(frozen=True)
class CreatedEdit:
    job_id: str
    created: bool


async def _find_existing_edit(
    session: AsyncSession, user_id: str, idempotency_key: str
) -> GenerationJob | None:
    result = await session.execute(
        select(GenerationJob).where(
            GenerationJob.user_id == user_id,
            GenerationJob.idempotency_key == idempotency_key,
        )
    )
    return result.scalar_one_or_none()


async def _current_good_revision(session: AsyncSession, project: Project) -> Revision | None:
    """Текущая good-ревизия проекта (projects.current_revision_id, is_good)."""
    if project.current_revision_id is None:
        return None
    revision = await session.get(Revision, project.current_revision_id)
    if revision is None or not revision.is_good:
        return None
    return revision


async def _spec_for_revision(
    session: AsyncSession, revision: Revision
) -> tuple[str | None, str | None]:
    """Спека (spec_tz, spec_ref) джобы, породившей ревизию — вход Agent 4 editor (ADR-014)."""
    source_job = await session.get(GenerationJob, revision.created_from_job_id)
    if source_job is None:
        return None, None
    return source_job.spec_tz, source_job.spec_ref


async def create_edit_job(
    session: AsyncSession,
    *,
    user_id: str,
    project_id: str,
    instruction: str,
    idempotency_key: str,
) -> CreatedEdit:
    """Создаёт edit-джобу (kind=edit, CREATED) + ставит task_edit. ADR-014 §A.

    Порядок (idempotency-aware, как create_project_with_job):
      1. idempotency-резолв ДО quota-gate — replay того же ключа не считается новой правкой;
      2. владение проектом (cross-tenant → 404) + проект LIVE (иначе 409);
      3. quota_gate kind='edit' (402 reason=edit_quota_exhausted/concurrency_limit/...);
      4. создать edit-джобу, скопировать спеку текущей good-ревизии, записать instruction
         в job_events, поставить task_edit.
    """
    settings = get_settings()

    existing = await _find_existing_edit(session, user_id, idempotency_key)
    if existing is not None:
        return CreatedEdit(job_id=existing.id, created=False)

    # Владение проектом (cross-tenant → 404) + soft-delete-фильтр.
    project = await project_service.get_project(session, user_id, project_id)
    if project is None:
        raise not_found("Project not found.")

    # Правка возможна только над LIVE-сайтом: текущая good-ревизия должна быть активна.
    revision = await _current_good_revision(session, project)
    if revision is None:
        raise conflict("Project is not LIVE — edit requires a deployed good revision.")

    # Отдельный лимит правок (ADR-014 §A): 402 при превышении edit-квоты/concurrency.
    await enforce_quota_gate(session, user_id, check_project_limit=False, kind="edit")

    spec_tz, spec_ref = await _spec_for_revision(session, revision)

    job = GenerationJob(
        id=new_job_id(),
        project_id=project.id,
        user_id=user_id,
        state=JobState.CREATED,
        kind="edit",
        idempotency_key=idempotency_key,
        max_fix_attempts=settings.max_fix_attempts,
        budget_usd=settings.job_budget_usd,
        wall_clock_deadline=datetime.now(UTC) + timedelta(seconds=settings.job_wall_clock_budget_s),
        # Спека текущей good-ревизии — вход Agent 4 editor (неизменна в edit-цикле).
        spec_tz=spec_tz,
        spec_ref=spec_ref,
    )
    session.add(job)
    await record_event(session, job.id, "job_created", to_state=JobState.CREATED.value)
    # Инструкция правки + базовая ревизия — для task_edit (источник истины append-only).
    await record_event(
        session,
        job.id,
        EDIT_REQUESTED_EVENT,
        payload={"instruction": instruction, "base_revision_id": revision.id},
    )
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        winner = await _find_existing_edit(session, user_id, idempotency_key)
        if winner is None:
            raise
        return CreatedEdit(job_id=winner.id, created=False)

    dispatch_for_state(job.id, JobState.CREATED, kind="edit")
    logger.info("edit_job_created", extra={"job_id": job.id, "project_id": project_id})
    return CreatedEdit(job_id=job.id, created=True)
