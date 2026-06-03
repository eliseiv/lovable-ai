"""Транзакционные переходы state-machine + публикация событий (ADR-001).

Каждый переход: транзакционно обновить generation_jobs.state + вставить job_events
+ опубликовать в Redis pub/sub job:{id}. Crash-resumable: state в колонке.
"""

from __future__ import annotations

import json
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.enums import JobState
from app.db.models import GenerationJob, JobEvent
from app.observability import metrics
from app.observability.redis_pool import get_redis

logger = get_logger(__name__)

# Терминальные state джобы — на них пишутся финализирующие метрики (ADR-015 §2.1/2.2).
_TERMINAL_JOB_STATES: frozenset[JobState] = frozenset({JobState.LIVE, JobState.FAILED})


def _record_terminal_metrics(job: GenerationJob, to_state: JobState) -> None:
    """Финализирующие метрики джобы на терминальном переходе (LIVE/FAILED, ADR-015 §2).

    jobs_total{kind,terminal_state} + job_cost_usd (себестоимость, §2.2) + fix_loop_depth
    (итоговый retry_count, §2.1). job_failed_total{reason} пишется отдельно в fail_job
    (нужен failure_reason). Метрики производны — Postgres остаётся источником истины.
    """
    kind = job.kind
    terminal = to_state.value
    metrics.jobs_total.labels(kind=kind, terminal_state=terminal).inc()
    with suppress(TypeError, ValueError):
        metrics.job_cost_usd.labels(kind=kind, terminal_state=terminal).observe(
            float(job.spend_usd)
        )
    metrics.fix_loop_depth.labels(terminal_state=terminal).observe(job.retry_count)
    # Edit-исход (ADR-014 §C, §2.5): успешная правка → LIVE = outcome="live". Авто-rollback
    # (edit_failed_rolled_back) пишется в _auto_rollback_edit (там известен триггер rollback).
    if kind == "edit" and to_state == JobState.LIVE:
        metrics.edit_outcome_total.labels(outcome="live").inc()


def _redis_channel(job_id: str) -> str:
    return f"job:{job_id}"


async def publish_event(
    job_id: str,
    event_type: str,
    *,
    to_state: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """Публикует событие в Redis pub/sub для SSE — best-effort (ADR-019 §Fix, pipeline §H).

    Вызывается из `transition()` ПОСЛЕ commit перехода state. publish — лишь at-most-once
    wake-сигнал для SSE; источник истины статуса — БД (`generation_jobs.state` + `job_events`),
    SSE имеет replay по Last-Event-ID (ADR-012). Поэтому сбой publish НЕ должен валить переход
    state / graceful-fail (§G): иначе таска падала бы ДО терминализации и джоба зависала бы в
    активном state, лоча concurrency-слот (прод-инцидент ADR-019).

    Sprint 6 (TD-007): соединение из переиспользуемого пула (publish — hot-path каждого
    перехода). В Celery-контексте `get_redis()` отдаёт per-task клиент (worker_redis_scope,
    observability §7) — корневой фикс loop-affinity; на ASGI-пути — клиент пула процесса.

    Расширенный catch — ВТОРОЙ слой (pipeline §H, ADR-019 §Fix п.3): ловим
    `(RedisError, OSError, RuntimeError)` — `RuntimeError` добавлен как предохранитель от
    остаточной loop-affinity-аномалии (`Event loop is closed` / `Future attached to a different
    loop`), логируем WARN и НЕ пробрасываем. Это НЕ замена корневого loop-fix (per-task Redis),
    а гарантия, что переход state и graceful-fail доходят до терминала даже при сбое нотификации.
    """
    message = {
        "event_type": event_type,
        "to_state": to_state,
        "payload": payload or {},
        "created_at": datetime.now(UTC).isoformat(),
    }
    client = get_redis()
    try:
        await client.publish(_redis_channel(job_id), json.dumps(message))
    except (aioredis.RedisError, OSError, RuntimeError) as exc:
        logger.warning("redis_publish_failed", extra={"job_id": job_id, "error": str(exc)})


async def record_event(
    session: AsyncSession,
    job_id: str,
    event_type: str,
    *,
    from_state: str | None = None,
    to_state: str | None = None,
    payload: dict[str, Any] | None = None,
) -> JobEvent:
    """Вставляет job_events (append-only). Коммит — на стороне вызывающего."""
    event = JobEvent(
        job_id=job_id,
        event_type=event_type,
        from_state=from_state,
        to_state=to_state,
        payload=payload or {},
    )
    session.add(event)
    return event


async def transition(
    session: AsyncSession,
    job: GenerationJob,
    to_state: JobState,
    *,
    event_type: str = "state_changed",
    payload: dict[str, Any] | None = None,
) -> None:
    """Транзакционный переход: обновить state + записать job_events + commit + publish.

    Публикация в Redis — после commit, чтобы SSE не видел незакоммиченный переход.
    """
    from_state = job.state.value
    job.state = to_state
    # ADR-019: heartbeat прогресса — last_transition_at обновляется РОВНО при смене state
    # (эта единственная транзакционная точка), не при cost-ledger/guard-state апдейтах. Так
    # reconciler (docs §E2) детектит зависание по простою в одном state, не ложно сброшенному
    # cost-ledger'ом. now() — серверное время БД (консистентно с server_default).
    job.last_transition_at = datetime.now(UTC)
    await record_event(
        session,
        job.id,
        event_type,
        from_state=from_state,
        to_state=to_state.value,
        payload=payload,
    )
    await session.commit()
    logger.info(
        "job_transition",
        extra={"job_id": job.id, "from_state": from_state, "to_state": to_state.value},
    )
    # Sprint 6 (ADR-015 §2): финализирующие метрики на терминальном переходе (после commit).
    if to_state in _TERMINAL_JOB_STATES:
        _record_terminal_metrics(job, to_state)
    await publish_event(job.id, event_type, to_state=to_state.value, payload=payload)
    # Sprint 5 (ADR-013): после коммита перехода — best-effort APNs push на значимых
    # состояниях (LIVE/FAILED/AWAITING_CLARIFICATION). Не в БД-транзакции (внешний
    # side-effect); фильтр перечня — notify (ADR-013 §3). Ленивый импорт — анти-цикл.
    from app.notify.trigger import enqueue_push_if_significant

    enqueue_push_if_significant(job.id, to_state.value)


async def fail_job(
    session: AsyncSession,
    job: GenerationJob,
    *,
    failure_reason: str,
    failure_log_ref: str | None = None,
    last_failure_signature: str | None = None,
) -> None:
    """Перевод в FAILED с машинным failure_reason (docs/03-data-model.md)."""
    job.failure_reason = failure_reason
    if failure_log_ref is not None:
        job.failure_log_ref = failure_log_ref
    if last_failure_signature is not None:
        job.last_failure_signature = last_failure_signature
    # Sprint 6 (ADR-015 §2.1): FAILED по failure_reason (kind). jobs_total/job_cost_usd/
    # fix_loop_depth — общий терминальный путь в transition ниже.
    metrics.job_failed_total.labels(reason=failure_reason, kind=job.kind).inc()
    await transition(
        session,
        job,
        JobState.FAILED,
        event_type="failed",
        payload={"failure_reason": failure_reason},
    )


async def load_job(session: AsyncSession, job_id: str) -> GenerationJob | None:
    result = await session.execute(select(GenerationJob).where(GenerationJob.id == job_id))
    return result.scalar_one_or_none()
