"""project.gc — полный GC ресурсов проекта при удалении (Sprint 4, ADR-011).

Триггер: DELETE /v1/projects/{pid} → soft-delete (projects.deleted_at=now()) + Celery
project.gc (queue=GC_QUEUE, build-воркер с доступом к Docker). Ответ endpoint — 202.

Шаги (ADR-011 §B / docs/modules/deploy/03-architecture.md §6; порядок обязателен,
каждый идемпотентен/best-effort, GC crash-resumable через Celery acks_late):
  1. Отмена in-flight джоб: не-терминальные generation_jobs проекта → FAILED(project_deleted).
     reason project_deleted НЕ расширяет enum state — терминал остаётся FAILED.
  2. Teardown всех site-контейнеров проекта: docker rm -f site_{subdomain} по всем
     site_deployments (любой status) — переиспользует S1 docker_deploy.teardown_container
     (снятие Traefik-route через удаление контейнера, Docker-провайдер).
  3. Освобождение volume: удаление хостового каталога {sites_host_root}/{pid}.
  4. Batch-delete S3-артефактов всех ревизий/деплоев проекта по префиксам
     sources/dist/logs/specs всех job_id (батч GC_S3_BATCH_SIZE).
  5. БД hard-delete в FK-порядке: site_deployments → revisions → (job_events/questions/
     answers/llm_usage) → generation_jobs → projects-строка. usage_counters/subscriptions/
     billing_events (агрегаты пользователя) НЕ трогаются.

subdomain не реюзается: строки удаляются вместе с проектом, новое значение генерируется
случайно при будущих деплоях (защита от subdomain-takeover, Q-DEPLOY-3).
"""

from __future__ import annotations

import asyncio
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.db.enums import TERMINAL_STATES, JobState
from app.db.models import (
    Answer,
    Attachment,
    GenerationJob,
    JobEvent,
    LlmUsage,
    Project,
    Question,
    Revision,
    SiteDeployment,
)
from app.db.session import session_scope, worker_engine_scope
from app.deploy import docker_deploy
from app.observability import metrics
from app.storage import s3
from app.storage.s3 import S3Storage, get_storage
from app.workers.celery_app import celery_app

logger = get_logger(__name__)

# Reason-код отмены джобы удалением проекта (ADR-011 §C; не расширяет enum state).
FAILURE_REASON_PROJECT_DELETED = "project_deleted"


async def _cancel_inflight_jobs(session: AsyncSession, project_id: str) -> int:
    """Шаг 1: не-терминальные джобы проекта → FAILED(project_deleted).

    Снимает их из active_jobs(user) (concurrency-cap) и диспетчеризации. Идемпотентно:
    уже-FAILED джобы (в т.ч. с другим reason) не трогаются. failure_reason
    перезаписывается на project_deleted только для отменяемых (нетерминальных) джоб.
    """
    result = await session.execute(
        select(GenerationJob).where(
            GenerationJob.project_id == project_id,
            GenerationJob.state.notin_(TERMINAL_STATES),
        )
    )
    jobs = list(result.scalars().all())
    for job in jobs:
        job.state = JobState.FAILED
        job.failure_reason = FAILURE_REASON_PROJECT_DELETED
        logger.info("project_gc_job_cancelled", extra={"job_id": job.id, "project_id": project_id})
    return len(jobs)


async def _teardown_site_containers(session: AsyncSession, project_id: str) -> int:
    """Шаг 2: docker rm -f всех site-контейнеров проекта (любой status).

    Переиспользует S1 teardown (docker_deploy.teardown_container): docker rm -f
    site_{subdomain} + снятие Traefik-route через удаление контейнера. Идемпотентно:
    отсутствие контейнера — не ошибка. docker rm — синхронный, в thread (не блокируем loop).
    """
    result = await session.execute(
        select(SiteDeployment.subdomain).where(SiteDeployment.project_id == project_id)
    )
    subdomains = list(result.scalars().all())
    for subdomain in subdomains:
        await asyncio.to_thread(docker_deploy.teardown_container, f"site_{subdomain}")
        logger.info(
            "project_gc_container_teardown",
            extra={"project_id": project_id, "subdomain": subdomain},
        )
    return len(subdomains)


def _remove_site_volume(settings: Settings, project_id: str) -> None:
    """Шаг 3: удаление хостового каталога сайтов проекта {sites_host_root}/{pid}.

    Идемпотентно: отсутствие каталога — не ошибка (ignore_errors). Каталог именуется
    по pid (docs/modules/deploy/03-architecture.md §3 publish_dist), един для всех ревизий.
    """
    site_dir = Path(settings.sites_host_root) / project_id
    shutil.rmtree(site_dir, ignore_errors=True)
    logger.info(
        "project_gc_volume_removed", extra={"project_id": project_id, "path": str(site_dir)}
    )


async def _delete_s3_artifacts(
    session: AsyncSession, storage: S3Storage, project_id: str, batch_size: int
) -> int:
    """Шаг 4: batch-delete S3-артефактов всех job_id проекта (префиксы sources/dist/logs/specs).

    Возвращает число удалённых ключей. Идемпотентно: отсутствие объектов под префиксом →
    0 (повторный GC — no-op). job_id берутся из generation_jobs проекта (ревизии/деплои
    ссылаются на те же job_id через детерминированные ключи).
    """
    result = await session.execute(
        select(GenerationJob.id).where(GenerationJob.project_id == project_id)
    )
    job_ids = list(result.scalars().all())
    deleted = 0
    for job_id in job_ids:
        for prefix in s3.job_artifact_prefixes(job_id):
            deleted += await storage.delete_prefix(prefix, batch_size=batch_size)
    # ADR-034 §D7: project-scoped префикс приложенных изображений uploads/{project_id}/ —
    # добавляется ОТДЕЛЬНО от per-job job_artifact_prefixes (один вызов на проект, не на job).
    deleted += await storage.delete_prefix(s3.uploads_prefix(project_id), batch_size=batch_size)
    if deleted:
        logger.info(
            "project_gc_s3_deleted", extra={"project_id": project_id, "deleted_keys": deleted}
        )
    return deleted


async def _hard_delete_db_rows(session: AsyncSession, project_id: str) -> None:
    """Шаг 5: hard-delete строк проекта в FK-безопасном порядке (ADR-011 §B.5).

    Порядок: site_deployments → revisions → (job_events/questions/answers/llm_usage,
    дочерние generation_jobs) → generation_jobs → projects. usage_counters/subscriptions/
    billing_events (агрегаты пользователя) НЕ трогаются. projects.current_revision_id
    обнуляется до удаления revisions (use_alter FK projects↔revisions). Идемпотентно:
    повторный GC по отсутствующей строке — no-op (rowcount 0).
    """
    # Снять FK projects.current_revision_id → revisions перед удалением revisions.
    project = await session.get(Project, project_id)
    if project is not None:
        project.current_revision_id = None
    await session.flush()

    job_ids = list(
        (
            await session.execute(
                select(GenerationJob.id).where(GenerationJob.project_id == project_id)
            )
        )
        .scalars()
        .all()
    )

    # site_deployments (FK→projects, →revisions) — раньше revisions/jobs.
    await session.execute(delete(SiteDeployment).where(SiteDeployment.project_id == project_id))
    # revisions (FK→projects, →generation_jobs) — после снятия current_revision_id.
    await session.execute(delete(Revision).where(Revision.project_id == project_id))

    # ADR-034 §D7: attachments удаляются ДО generation_jobs (FK attachments.job_id) и ДО
    # projects (FK attachments.project_id). Скоуп — project_id (берутся все фото проекта).
    await session.execute(delete(Attachment).where(Attachment.project_id == project_id))

    if job_ids:
        # Дочерние generation_jobs (FK→generation_jobs.id) — до самих джоб.
        await session.execute(delete(JobEvent).where(JobEvent.job_id.in_(job_ids)))
        await session.execute(delete(Answer).where(Answer.job_id.in_(job_ids)))
        await session.execute(delete(Question).where(Question.job_id.in_(job_ids)))
        await session.execute(delete(LlmUsage).where(LlmUsage.job_id.in_(job_ids)))
        # generation_jobs (FK→projects) — после своих дочерних.
        await session.execute(delete(GenerationJob).where(GenerationJob.project_id == project_id))

    # projects-строка — последней.
    await session.execute(delete(Project).where(Project.id == project_id))
    logger.info("project_gc_db_deleted", extra={"project_id": project_id})


def _aware(dt: datetime) -> datetime:
    """Naive-datetime из БД → UTC-aware для вычисления gc-lag."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


async def _refresh_gc_pending_gauge() -> None:
    """Обновляет lovable_project_gc_pending: soft-deleted проекты с ещё-живыми строками (TD-010).

    Gauge = COUNT(projects WHERE deleted_at IS NOT NULL): строка проекта удаляется hard-delete
    в конце GC, поэтому остаток = незавершённые GC (eventual-окно). Best-effort: ошибка БД
    не валит GC (метрика обновится на следующем тике/прогоне).
    """
    try:
        async with session_scope() as session:
            result = await session.execute(
                select(func.count()).select_from(Project).where(Project.deleted_at.is_not(None))
            )
            metrics.project_gc_pending.set(int(result.scalar_one()))
    except Exception as exc:  # noqa: BLE001 — метрика наблюдаемости не должна валить GC
        logger.warning("project_gc_pending_gauge_failed", extra={"error": str(exc)})


async def _run_gc(project_id: str) -> None:
    """Полный GC ресурсов проекта (ADR-011 §B). Идемпотентен / crash-resumable.

    Шаги 1-2 и 5 — в БД-транзакциях; шаги 3-4 (host-fs/S3) best-effort вне БД-транзакции.
    Каждый шаг безопасно переисполняется (Celery acks_late): отсутствие
    контейнера/каталога/S3-объекта/строки — не ошибка.
    """
    settings = get_settings()
    storage = get_storage()
    gc_started = time.monotonic()

    async with session_scope() as session:
        project = await session.get(Project, project_id)
        if project is None:
            # Строки уже нет — GC завершён ранее (повторный project.gc): no-op.
            logger.info("project_gc_already_done", extra={"project_id": project_id})
            return

        # Sprint 6 (TD-010, observability §2.5): gc-lag = длительность от 202 (soft-delete
        # projects.deleted_at) до завершения GC. Источник старта окна — deleted_at, чтобы
        # учитывать очередь/ретраи (не только время исполнения таски).
        deleted_at = project.deleted_at

        # Шаг 1: отмена in-flight джоб.
        cancelled = await _cancel_inflight_jobs(session, project_id)
        await session.commit()

    # Шаг 2: teardown контейнеров (отдельная сессия — только чтение subdomain'ов).
    async with session_scope() as session:
        containers_removed = await _teardown_site_containers(session, project_id)

    # Шаг 3: освобождение host-volume (вне БД-транзакции).
    _remove_site_volume(settings, project_id)

    # Шаг 4: batch-delete S3-артефактов (вне БД-транзакции).
    async with session_scope() as session:
        s3_deleted = await _delete_s3_artifacts(
            session, storage, project_id, settings.gc_s3_batch_size
        )

    # Шаг 5: hard-delete строк в FK-порядке.
    async with session_scope() as session:
        await _hard_delete_db_rows(session, project_id)
        await session.commit()

    # gc-lag (TD-010): от soft-delete (deleted_at) до завершения GC. Если deleted_at известен —
    # «end-to-end» окно; иначе fallback на длительность исполнения таски.
    if deleted_at is not None:
        lag = (datetime.now(UTC) - _aware(deleted_at)).total_seconds()
    else:
        lag = time.monotonic() - gc_started
    metrics.project_gc_duration_seconds.labels(result="success").observe(max(lag, 0.0))
    # Обновляем gauge незавершённых GC (soft-deleted с ещё-живыми projects-строками).
    await _refresh_gc_pending_gauge()

    logger.info(
        "project_gc_done",
        extra={
            "project_id": project_id,
            "jobs_cancelled": cancelled,
            "containers_removed": containers_removed,
            "s3_keys_deleted": s3_deleted,
        },
    )


@celery_app.task(name="project.gc", queue=get_settings().gc_queue)
def project_gc(project_id: str) -> None:
    """Celery-таска полного GC проекта (queue=GC_QUEUE — build-воркер с доступом к Docker).

    Идемпотентна (acks_late): повторный запуск на уже-удалённом проекте — no-op.
    """

    async def _run() -> None:
        # observability §7 (ADR-019): per-task async-engine внутри asyncio.run-loop задачи;
        # session_scope в _run_gc/_refresh_gc_pending_gauge подхватывает его из ContextVar.
        async with worker_engine_scope():
            await _run_gc(project_id)

    asyncio.run(_run())
