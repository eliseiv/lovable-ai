"""Переход DEPLOYING → FIXING: запись failure_log в S3 + смена state (docs §B, §F).

Доменный фейл (build-fail / health-fail / invalid-agent-output) после deploy-teardown
уводит джобу в FIXING (а НЕ в FAILED, как в Sprint 1). Сюда вынесена общая логика:
  - собрать failure_log с машинной шапкой (§F) и записать в S3 per-attempt ключом по
    стадии (ADR-022, deploy §F-1): build/npm-фейл → logs/{job_id}/build.{retry_count}.log;
    deploy/health-фейл → logs/{job_id}/deploy.{retry_count}.log;
  - обновить generation_jobs.failure_log_ref;
  - транзакционно перевести state DEPLOYING → FIXING (+ job_events build_failed) и
    диспетчеризовать task_fix.

failure_signature НЕ считается здесь — она вычисляется на входе в FIXING (в task_fix,
до постановки Agent 4), это единственная точка её записи (docs §C(d), ADR-005).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.enums import JobState
from app.db.models import GenerationJob, Revision
from app.pipeline.dispatcher import dispatch_for_state
from app.pipeline.events import record_event, transition
from app.pipeline.failure_signature import build_failure_log
from app.storage import s3
from app.storage.s3 import S3Storage

logger = get_logger(__name__)


async def latest_revision_for_job(session: AsyncSession, job_id: str) -> Revision | None:
    """Последняя ревизия ТЕКУЩЕЙ джобы: created_from_job_id=job_id, max(revision_no).

    Не глобальный max(revision_no) по проекту (docs §A вход п.2): в edit-цикле S5
    верхняя ревизия проекта может быть прежней good-ревизией другой джобы.
    """
    result = await session.execute(
        select(Revision)
        .where(Revision.created_from_job_id == job_id)
        .order_by(Revision.revision_no.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def enter_fixing(
    session: AsyncSession,
    job: GenerationJob,
    storage: S3Storage,
    *,
    failure_class: str,
    failure_body: str,
    revision_no: int | None,
    exit_code: int | None = None,
) -> None:
    """Записывает failure_log в S3 и переводит джобу DEPLOYING → FIXING (+ dispatch task_fix).

    failure_class — машинный класс фейла (§F): build_error / npm_install_error /
    health_timeout / health_5xx / health_4xx.

    Per-attempt ключ по стадии failure_class (ADR-022, deploy §F-1): build/npm-фейл →
    build_log_key (build.{retry_count}.log); deploy/health-фейл → deploy_log_key
    (deploy.{retry_count}.log). Раздельные имена-стадии при одном retry_count исключают
    затирание лога успешной сборки того же витка deploy-фейлом. Дискриминатор —
    монотонный job.retry_count.
    """
    log = build_failure_log(
        failure_class=failure_class,
        body=failure_body,
        revision_no=revision_no,
        exit_code=exit_code,
        extra_header={"job_id": job.id},
    )
    if failure_class in ("build_error", "npm_install_error"):
        log_key = s3.build_log_key(job.id, job.retry_count)
    else:
        log_key = s3.deploy_log_key(job.id, job.retry_count)
    log_ref = await storage.put_text(log_key, log, "text/plain")
    job.failure_log_ref = log_ref
    # Новый failure-event: гард no-progress (§C(d)) отличит реальный повтор от
    # crash-resume того же события. Сбросит флаг сам гард при проверке.
    job.failure_event_pending = True

    await record_event(
        session,
        job.id,
        "build_failed",
        payload={"failure_class": failure_class, "failure_log_ref": log_ref},
    )
    await transition(
        session,
        job,
        JobState.FIXING,
        event_type="state_changed",
        payload={"failure_class": failure_class, "failure_log_ref": log_ref},
    )
    dispatch_for_state(job.id, JobState.FIXING)
    logger.info(
        "entered_fixing",
        extra={"job_id": job.id, "failure_class": failure_class, "failure_log_ref": log_ref},
    )
