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
from sqlalchemy import CursorResult, select, update
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
) -> bool:
    """Транзакционный переход: обновить state + записать job_events + commit + publish.

    Возвращает True, если переход применён (1 строка CAS), False — если no-op (джоба уже
    терминальна, 0 строк CAS). Вызывающий (fail_job) использует возврат, чтобы не дублировать
    terminal-метрики на проигравшем писателе.

    Публикация в Redis — после commit, чтобы SSE не видел незакоммиченный переход.

    ADR-029 §A — CAS-барьер терминальности (единый барьер, корень). Запись state выполняется
    как conditional UPDATE: `UPDATE ... SET state=:to WHERE id=:id AND state NOT IN (LIVE,FAILED)`
    (_TERMINAL_JOB_STATES = {LIVE, FAILED}) в той же транзакции, что job_events+commit.
    `transition()` — ЕДИНСТВЕННЫЙ писатель state БЕЗ предусловия на конкретное исходное состояние
    (универсальный путь всех тасок + reconciler), поэтому барьер A покрывает все идущие через него
    переходы разом. Гонка двух писателей-ПЕРЕХОДОВ разрешается в Postgres: ровно один UPDATE
    затронет строку. 0 затронутых строк ⇒ джоба уже терминальна ⇒ переход — no-op (job_events НЕ
    пишется, publish/push/terminal-метрики НЕ выполняются), лог transition_skip_terminal, возврат
    False. Так конкурирующий писатель-ПЕРЕХОД не может перезатереть терминал: кто записал терминал
    первым, тот зафиксировал результат, второй — тихий no-op (идемпотентно).

    `transition()` — не единственная строка, физически пишущая generation_jobs.state. Существуют
    санкционированные прямые писатели вне барьера A, каждый несёт собственный предикат/обоснование
    (см. pipeline §Инвариант терминальности → список писателей, ADR-029 §A):
      - answers_service.submit_answers (AWAITING_CLARIFICATION → SPECCING): несёт собственный
        non-terminal-предикат на исходном AWAITING_CLARIFICATION (исключает оба терминала);
      - project_gc._cancel_inflight_jobs (* → FAILED при удалении проекта): отбор по
        TERMINAL_STATES = {FAILED} (НЕ {LIVE,FAILED}), поэтому НАМЕРЕННО перезаписывает
        LIVE → FAILED как санкционированное исключение по ADR-011 (удаление проекта снимает
        живой сайт), а НЕ нарушение инварианта.
    """
    from_state = job.state.value
    # job_id захватываем в локаль ДО session.execute/rollback: в no-op ветке CAS-барьера
    # session.rollback() экспайрит ORM-инстанс job, и последующее чтение job.id из синхронного
    # контекста аргументов логгера триггерит ленивую async-загрузку → MissingGreenlet.
    job_id = job.id
    now = datetime.now(UTC)
    # CAS-барьер: атомарная запись state ТОЛЬКО если джоба ещё НЕ терминальна. last_transition_at
    # (heartbeat прогресса, ADR-019) обновляется РОВНО при смене state в этой же точке, не при
    # cost-ledger/guard-state апдейтах. now() — Python-сторона; консистентно с server_default.
    result: CursorResult[Any] = await session.execute(  # type: ignore[assignment]
        update(GenerationJob)
        .where(
            GenerationJob.id == job_id,
            GenerationJob.state.not_in(_TERMINAL_JOB_STATES),
        )
        .values(state=to_state, last_transition_at=now)
    )
    if result.rowcount == 0:
        # 0 строк ⇒ джоба уже терминальна (LIVE/FAILED записан конкурентным писателем) ⇒ no-op:
        # НЕ пишем job_events, НЕ публикуем, НЕ дублируем terminal-метрики/push. Откатываем любые
        # незакоммиченные изменения этой сессии (например, выставленные fail_job-полем
        # failure_reason на ORM-объекте), чтобы не утёк partial-write мимо барьера.
        await session.rollback()
        logger.info(
            "transition_skip_terminal",
            extra={
                "job_id": job_id,
                "from_terminal": from_state,
                "attempted_to": to_state.value,
            },
        )
        return False
    # 1 строка ⇒ переход применён штатно. Синхронизируем ORM-объект с записанным CAS-значением
    # (UPDATE по таблице не трогает атрибуты загруженного объекта) — для _record_terminal_metrics
    # и вызывающего кода ниже.
    job.state = to_state
    job.last_transition_at = now
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
    return True


async def fail_job(
    session: AsyncSession,
    job: GenerationJob,
    *,
    failure_reason: str,
    failure_log_ref: str | None = None,
    last_failure_signature: str | None = None,
) -> None:
    """Перевод в FAILED с машинным failure_reason (docs/03-data-model.md).

    ADR-029 §A: job_failed_total пишется ТОЛЬКО если CAS-барьер transition() реально применил
    переход (джоба не была терминальна). Иначе reconciler/проигравший писатель, попытавшийся
    FAILED поверх уже-LIVE (или дубль FAILED), ложно инкрементировал бы failure-метрику на no-op.
    """
    job.failure_reason = failure_reason
    if failure_log_ref is not None:
        job.failure_log_ref = failure_log_ref
    if last_failure_signature is not None:
        job.last_failure_signature = last_failure_signature
    applied = await transition(
        session,
        job,
        JobState.FAILED,
        event_type="failed",
        payload={"failure_reason": failure_reason},
    )
    # Sprint 6 (ADR-015 §2.1): FAILED по failure_reason (kind). jobs_total/job_cost_usd/
    # fix_loop_depth — общий терминальный путь в transition. Не дублируем на no-op (ADR-029).
    if applied:
        metrics.job_failed_total.labels(reason=failure_reason, kind=job.kind).inc()


async def touch_heartbeat(session: AsyncSession, job: GenerationJob) -> None:
    """Обновляет last_transition_at на distinct failure-event витка БЕЗ смены state (ADR-029).

    ADR-029 §Связь с watchdog / pipeline §E2 «Heartbeat на distinct failure-event»: витки,
    прогрессирующие без смены state (отклонённый патч Agent 4 в FIXING → agent_output_invalid,
    _handle_invalid_patch — остаётся в FIXING без инкремента retry_count), двигают heartbeat
    прогресса наравне со сменой state. Так живая прогрессирующая (LLM-вызовы идут) edit/fix-джоба
    НЕ получает ложный FAILED(stuck_timeout) reconciler'ом (§E2) только из-за того, что state не
    меняется между витками. Триггеров прогресса два: смена state (transition()) И новый distinct
    failure-event витка (эта функция). cost-ledger (spend_usd) heartbeat НЕ трогает (ложный
    «живой» сигнал от Celery-ретраев исключён, как в ADR-019). Коммит — на стороне вызывающего
    (в той же транзакции, что failure_event_pending/failure_log_ref/fix_rejected).
    Защита от вечного зацикливания живой джобы остаётся за wall-clock §C(c) и no-progress §C(d).
    """
    job.last_transition_at = datetime.now(UTC)


async def load_job(session: AsyncSession, job_id: str) -> GenerationJob | None:
    result = await session.execute(select(GenerationJob).where(GenerationJob.id == job_id))
    return result.scalar_one_or_none()
