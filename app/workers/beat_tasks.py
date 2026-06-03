"""Beat-периодика Sprint 2: sweeper уточнений + reconciler застрявших джоб (docs §E).

Оба job'а — в Celery beat (единственный экземпляр, docs/07-deployment.md). Идемпотентны:
операции по предикату state + транзакционная смена; повторный beat-tick / двойная
доставка безопасны.

E1. sweeper: AWAITING_CLARIFICATION дольше CLARIFICATION_TTL_S → FAILED(clarification_timeout).
E2. reconciler: BUILDING/DEPLOYING/FIXING дольше STUCK_THRESHOLD_S → ре-диспетчеризация
    (не меняет state, переставляет таску по текущему state, ADR-001) под
    FOR UPDATE SKIP LOCKED + Redis dispatch-lock против двойной постановки.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import redis.asyncio as aioredis
from sqlalchemy import select

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.enums import JobState
from app.db.models import GenerationJob
from app.db.session import session_scope
from app.pipeline.dispatcher import dispatch_for_state
from app.pipeline.events import fail_job
from app.workers.celery_app import celery_app

logger = get_logger(__name__)

# Активные нетерминальные состояния, которые reconciler подхватывает (docs §E2).
_STUCK_STATES = (JobState.BUILDING, JobState.DEPLOYING, JobState.FIXING)
# TTL короткоживущего Redis dispatch-lock: больше интервала reconciler'а, меньше
# stuck-порога — чтобы lock истёк к следующему реальному зависанию, но пережил
# двойную доставку одного beat-tick.
_DISPATCH_LOCK_TTL_S = 300


# --- E1. Sweeper AWAITING_CLARIFICATION (TTL) ---


async def _sweep_clarifications() -> int:
    settings = get_settings()
    cutoff = datetime.now(UTC) - timedelta(seconds=settings.clarification_ttl_s)
    swept = 0
    async with session_scope() as session:
        # SELECT ... FOR UPDATE SKIP LOCKED: не конкурируем с конкурентным резюмом
        # (POST /answers атомарно двигает AWAITING_CLARIFICATION → SPECCING).
        result = await session.execute(
            select(GenerationJob)
            .where(
                GenerationJob.state == JobState.AWAITING_CLARIFICATION,
                GenerationJob.updated_at < cutoff,
            )
            .with_for_update(skip_locked=True)
        )
        jobs = list(result.scalars().all())
        for job in jobs:
            # Идемпотентно: повтор не трогает уже-FAILED (предикат state в выборке).
            await fail_job(session, job, failure_reason="clarification_timeout")
            swept += 1
    if swept:
        logger.info("clarifications_swept", extra={"count": swept})
    return swept


@celery_app.task(name="beat.sweep_clarifications", queue="llm")
def sweep_clarifications() -> int:
    return asyncio.run(_sweep_clarifications())


# --- E2. Reconciler застрявших BUILDING/DEPLOYING/FIXING (crash-resume) ---


async def _acquire_dispatch_lock(client: aioredis.Redis, job_id: str) -> bool:
    """Короткоживущий Redis-lock dispatch:{job_id} (SET NX EX): True, если взяли.

    Защищает от двойной постановки таски reconciler'ом и acks_late-повтором (docs §E2).
    """
    acquired = await client.set(f"dispatch:{job_id}", "1", nx=True, ex=_DISPATCH_LOCK_TTL_S)
    return bool(acquired)


async def _reconcile_stuck() -> int:
    settings = get_settings()
    cutoff = datetime.now(UTC) - timedelta(seconds=settings.stuck_threshold_s)
    redispatched = 0
    client = aioredis.from_url(settings.redis_url)
    try:
        async with session_scope() as session:
            # FOR UPDATE SKIP LOCKED: не трогаем джобы, с которыми прямо сейчас работает
            # активная таска (она держит строку в своей транзакции на время перехода).
            result = await session.execute(
                select(GenerationJob)
                .where(
                    GenerationJob.state.in_(_STUCK_STATES),
                    GenerationJob.updated_at < cutoff,
                )
                .with_for_update(skip_locked=True)
            )
            jobs = list(result.scalars().all())
            for job in jobs:
                # Redis dispatch-lock: если таска для (job_id) уже переставлена недавно
                # (acks_late-повтор / прошлый tick) — не дублируем.
                if not await _acquire_dispatch_lock(client, job.id):
                    logger.info("reconcile_lock_busy", extra={"job_id": job.id})
                    continue
                # Не меняем state — ре-диспетчеризуем по текущему (ADR-001). Гарды
                # проверяются на обычном пути (истёкший wall-clock → штатный FAILED).
                # kind пробрасываем: edit-джоба в CREATED → task_edit, не task_interview.
                dispatch_for_state(job.id, job.state, kind=job.kind)
                redispatched += 1
                logger.info(
                    "reconcile_redispatch",
                    extra={"job_id": job.id, "state": job.state.value},
                )
            # Транзакция фиксируется на выходе из session_scope; SKIP LOCKED-блокировки
            # снимаются. Сам dispatch уже произошёл (idempotent через cleanup-before-run).
            await session.commit()
    finally:
        await client.aclose()  # type: ignore[attr-defined]  # redis 5.x async; stub устарел
    if redispatched:
        logger.info("stuck_jobs_redispatched", extra={"count": redispatched})
    return redispatched


@celery_app.task(name="beat.reconcile_stuck", queue="llm")
def reconcile_stuck() -> int:
    return asyncio.run(_reconcile_stuck())


# --- Sprint 6: refresh snapshot-gauge метрик (jobs_in_state/queue_depth/worker_busy/spend) ---


@celery_app.task(name="metrics.refresh", queue="llm")
def refresh_metrics() -> None:
    """Beat-tick refresh «мгновенных» gauge-метрик (ADR-015 §2.1/2.6).

    Pull-модель: beat (единственный экземпляр) экспонирует обновлённый snapshot на своём
    /metrics (METRICS_PORT). Best-effort: ошибки внутри collector логируются, не валят beat.
    """
    from app.observability.collector import refresh_all

    asyncio.run(refresh_all())
