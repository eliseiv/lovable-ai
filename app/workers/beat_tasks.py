"""Beat-периодика: sweeper уточнений + reconciler застрявших джоб (docs §E, ADR-019).

Оба job'а — в Celery beat (единственный экземпляр, docs/07-deployment.md). Идемпотентны:
операции по предикату state + транзакционная смена; повторный beat-tick / двойная
доставка безопасны.

E1. sweeper: AWAITING_CLARIFICATION дольше CLARIFICATION_TTL_S → FAILED(clarification_timeout).
E2. reconciler (ADR-019): ВСЕ активные нетерминальные состояния (CREATED/INTERVIEWING/
    SPECCING/BUILDING/DEPLOYING/FIXING, кроме AWAITING_CLARIFICATION — свой TTL §E1) дольше
    STUCK_THRESHOLD_S по last_transition_at. Две ветви:
      (1) resumable BUILDING/DEPLOYING/FIXING → ре-диспетчеризация по текущему state
          (не меняет state, ADR-001; идемпотентно через cleanup-before-run);
      (2) LLM-фаза CREATED/INTERVIEWING/SPECCING без живой таски → fail-stuck в
          FAILED(stuck_timeout) — предохранитель concurrency-leak, если graceful-fail (§G)
          не сработал (смерть воркера до записи перехода).
    Под FOR UPDATE SKIP LOCKED + Redis dispatch-lock против двойной постановки/терминализации.

observability §7 (ADR-019): sync Celery-задачи с async-DB создают/dispose'ят async-engine
ВНУТРИ своего asyncio.run-loop (task_engine_scope), не переиспускают глобальный FastAPI-
engine — иначе `RuntimeError: Future attached to a different loop` (asyncpg).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.enums import JobState
from app.db.models import GenerationJob
from app.db.session import task_engine_scope
from app.pipeline.dispatcher import dispatch_for_state
from app.pipeline.events import fail_job
from app.workers.celery_app import celery_app

logger = get_logger(__name__)

# Resumable активные состояния (ветвь 1): повторная постановка таски безопасна и идемпотентна
# (cleanup-before-run в deploy). Гарды §C проверяются на обычном пути таски.
_RESUMABLE_STATES = (JobState.BUILDING, JobState.DEPLOYING, JobState.FIXING)
# LLM-фазные активные состояния (ветвь 2): при систематически недоступном LLM повторная
# постановка таски лишь бесконечно крутила бы Celery-retry без терминализации → fail-stuck.
_LLM_PHASE_STATES = (JobState.CREATED, JobState.INTERVIEWING, JobState.SPECCING)
# Полный набор активных нетерминальных состояний, удерживающих concurrency-слот (ADR-019).
# AWAITING_CLARIFICATION исключён (свой TTL §E1); терминалы LIVE/FAILED — слот уже свободен.
_STUCK_STATES = _RESUMABLE_STATES + _LLM_PHASE_STATES
# TTL короткоживущего Redis dispatch-lock: больше интервала reconciler'а, меньше stuck-порога —
# чтобы lock истёк к следующему реальному зависанию, но пережил двойную доставку одного tick.
_DISPATCH_LOCK_TTL_S = 300


# --- E1. Sweeper AWAITING_CLARIFICATION (TTL) ---


async def _sweep_clarifications(sessionmaker: async_sessionmaker[AsyncSession]) -> int:
    settings = get_settings()
    cutoff = datetime.now(UTC) - timedelta(seconds=settings.clarification_ttl_s)
    swept = 0
    async with sessionmaker() as session:
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
    async def _run() -> int:
        async with task_engine_scope() as sessionmaker:
            return await _sweep_clarifications(sessionmaker)

    return asyncio.run(_run())


# --- E2. Reconciler застрявших активных состояний (crash-resume + concurrency-leak, ADR-019) ---


async def _acquire_dispatch_lock(client: aioredis.Redis, job_id: str) -> bool:
    """Короткоживущий Redis-lock dispatch:{job_id} (SET NX EX): True, если взяли.

    Защищает от двойной постановки таски / двойной терминализации reconciler'ом и
    acks_late-повтором (docs §E2). Тот же ключ, что dispatch-lock реальной таски.
    """
    acquired = await client.set(f"dispatch:{job_id}", "1", nx=True, ex=_DISPATCH_LOCK_TTL_S)
    return bool(acquired)


def _wall_clock_expired(job: GenerationJob, now: datetime) -> bool:
    """True, если у джобы проставлен wall_clock_deadline и он истёк (гард §C(c))."""
    deadline = job.wall_clock_deadline
    if deadline is None:
        return False
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    return now >= deadline


async def _select_stuck_candidates(session: AsyncSession, cutoff: datetime) -> list[str]:
    """Снимок job_id застрявших джоб (read-only, короткая транзакция).

    НЕ держим SELECT FOR UPDATE на весь проход терминализации (Major-фикс §E2): иначе
    первый commit внутри цикла завершил бы транзакцию и снял бы FOR UPDATE-локи остальных
    кандидатов, а terminализация шла бы по строкам без блокировки — TOCTOU с acks_late-таской.
    Здесь только собираем кандидатов; блокировку берём пер-джоба в _reconcile_one.
    """
    result = await session.execute(
        select(GenerationJob.id).where(
            GenerationJob.state.in_(_STUCK_STATES),
            GenerationJob.last_transition_at < cutoff,
        )
    )
    return list(result.scalars().all())


async def _reconcile_one(
    sessionmaker: async_sessionmaker[AsyncSession],
    client: aioredis.Redis,
    job_id: str,
    now: datetime,
    cutoff: datetime,
) -> bool:
    """Терминализирует/ре-диспетчеризирует ОДНУ застрявшую джобу в своей короткой транзакции.

    Защита от двойной терминализации (Major-фикс §E2): свежий `SELECT ... FOR UPDATE` того же
    job_id + ре-проверка `state ∈ _STUCK_STATES` И `last_transition_at < cutoff`. Если между
    снимком кандидатов и этой транзакцией реальная (acks_late) таска продвинула джобу — она
    уже не stuck/не активна → no-op (continue). Redis dispatch-lock сохранён: не дублируем
    постановку/терминализацию с acks_late-повтором и прошлым tick'ом.

    Возвращает True, если джоба обработана (re-dispatch / fail-stuck / wall-clock).
    """
    async with sessionmaker() as session:
        # Свежий FOR UPDATE именно этого job_id: ре-проверяем под блокировкой строки, что
        # джоба всё ещё застряла (ветвь продвижения реальной таской между SELECT и здесь —
        # SKIP LOCKED отдаст None, либо предикат не совпадёт → пропускаем).
        result = await session.execute(
            select(GenerationJob)
            .where(GenerationJob.id == job_id)
            .with_for_update(skip_locked=True)
        )
        job = result.scalar_one_or_none()
        if job is None:
            # Строка прямо сейчас залочена активной таской (SKIP LOCKED) или исчезла → не наша.
            logger.info("reconcile_skip_locked_or_missing", extra={"job_id": job_id})
            return False
        # Ре-проверка stuck-инварианта под блокировкой: acks_late-таска могла продвинуть state
        # или обновить last_transition_at между снимком кандидатов и этой транзакцией (TOCTOU).
        # last_transition_at нормализуем к UTC-aware (как _wall_clock_expired) — колонка
        # timezone=True, но naive-фикстуры тестов не должны ломать сравнение.
        last_transition = job.last_transition_at
        if last_transition.tzinfo is None:
            last_transition = last_transition.replace(tzinfo=UTC)
        if job.state not in _STUCK_STATES or last_transition >= cutoff:
            logger.info(
                "reconcile_no_longer_stuck",
                extra={"job_id": job_id, "state": job.state.value},
            )
            return False

        # Redis dispatch-lock: если таска для (job_id) уже переставлена/терминализуется недавно
        # (acks_late-повтор / прошлый tick) — не дублируем (двойная постановка И двойная
        # терминализация, docs §E2). Берём ПОСЛЕ ре-проверки, чтобы не «сжечь» lock на no-op.
        if not await _acquire_dispatch_lock(client, job.id):
            logger.info("reconcile_lock_busy", extra={"job_id": job.id})
            return False

        # Wall-clock-предохранитель §C(c): джоба истекла по суммарному времени — в любой ветви
        # штатный FAILED(wall_clock_exceeded), освобождая слот (fail_job→transition коммитит).
        if _wall_clock_expired(job, now):
            await fail_job(session, job, failure_reason="wall_clock_exceeded")
            logger.info("reconcile_wall_clock", extra={"job_id": job.id})
            return True

        if job.state in _RESUMABLE_STATES:
            # Ветвь (1): ре-диспетчеризация по текущему state (ADR-001) — state не меняем.
            # Идемпотентно через cleanup-before-run. Гарды §C проверятся на обычном пути таски.
            # kind пробрасываем: edit-джоба в CREATED → task_edit, не task_interview.
            # commit снимает FOR UPDATE-лок этой джобы ДО постановки таски, чтобы поднятая
            # таска не конкурировала с нашей блокировкой строки.
            await session.commit()
            dispatch_for_state(job.id, job.state, kind=job.kind)
            logger.info(
                "reconcile_redispatch",
                extra={"job_id": job.id, "state": job.state.value},
            )
            return True

        # Ветвь (2): LLM-фаза (CREATED/INTERVIEWING/SPECCING) без живой таски, провисевшая
        # дольше STUCK_THRESHOLD_S → fail-stuck FAILED(stuck_timeout), освобождая concurrency-
        # слот. Предохранитель: даже если graceful-fail (§G) не сработал (смерть воркера до
        # записи перехода), reconciler терминализирует. fail_job→transition коммитит.
        await fail_job(session, job, failure_reason="stuck_timeout")
        logger.info(
            "reconcile_fail_stuck",
            extra={"job_id": job.id, "state": job.state.value},
        )
        return True


async def _reconcile_stuck(sessionmaker: async_sessionmaker[AsyncSession]) -> int:
    """Подхватывает джобы, застрявшие в активных нетерминальных состояниях (ADR-019 §E2).

    Stuck-критерий — last_transition_at (heartbeat прогресса), НЕ updated_at: cost-ledger
    дёргает updated_at и ложно сбрасывал бы heartbeat. Две ветви (resumable ре-диспетчеризация
    / LLM-фаза fail-stuck) + wall-clock-предохранитель §C(c) в любой ветви.

    Major-фикс гонки двойной терминализации (§E2): кандидаты собираются read-only снимком,
    затем КАЖДЫЙ терминализируется в ОТДЕЛЬНОЙ короткой транзакции с повторным
    `SELECT ... FOR UPDATE` того же job_id и ре-проверкой stuck-инварианта. Так первый commit
    не снимает FOR UPDATE-локи остальных выбранных джоб (TOCTOU с acks_late-таской, что могла
    продвинуть джобу между снимком и терминализацией).
    """
    settings = get_settings()
    now = datetime.now(UTC)
    cutoff = now - timedelta(seconds=settings.stuck_threshold_s)
    handled = 0
    client = aioredis.from_url(settings.redis_url)
    try:
        # 1. Read-only снимок кандидатов (без удержания FOR UPDATE на весь проход).
        async with sessionmaker() as session:
            candidates = await _select_stuck_candidates(session, cutoff)
        # 2. Терминализация/ре-диспетчеризация каждого в отдельной короткой транзакции с
        #    повторной блокировкой + ре-проверкой stuck-инварианта (защита от TOCTOU).
        for job_id in candidates:
            if await _reconcile_one(sessionmaker, client, job_id, now, cutoff):
                handled += 1
    finally:
        await client.aclose()  # type: ignore[attr-defined]  # redis 5.x async; stub устарел
    if handled:
        logger.info("stuck_jobs_reconciled", extra={"count": handled})
    return handled


@celery_app.task(name="beat.reconcile_stuck", queue="llm")
def reconcile_stuck() -> int:
    async def _run() -> int:
        async with task_engine_scope() as sessionmaker:
            return await _reconcile_stuck(sessionmaker)

    return asyncio.run(_run())


# --- Sprint 6: refresh snapshot-gauge метрик (jobs_in_state/queue_depth/worker_busy/spend) ---


@celery_app.task(name="metrics.refresh", queue="llm")
def refresh_metrics() -> None:
    """Beat-tick refresh «мгновенных» gauge-метрик (ADR-015 §2.1/2.6).

    Pull-модель: beat (единственный экземпляр) экспонирует обновлённый snapshot на своём
    /metrics (METRICS_PORT). Best-effort: ошибки внутри collector логируются, не валят beat.
    refresh_all сам управляет per-task async-engine (observability §7) внутри asyncio.run.
    """
    from app.observability.collector import refresh_all

    asyncio.run(refresh_all())
