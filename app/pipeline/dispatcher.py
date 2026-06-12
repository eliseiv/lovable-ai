"""Диспетчер task-на-состояние (ADR-001, docs/modules/pipeline/03-architecture.md).

По текущему generation_jobs.state ставит следующий Celery-task в нужную очередь.
Пауза на AWAITING_CLARIFICATION — ноль задач в очереди. Резюм из POST /answers.

Импорт celery-тасок ленивый (внутри функции), чтобы избежать циклического импорта
api ↔ workers и держать API независимым от тяжёлых зависимостей при импорте.
"""

from __future__ import annotations

import redis

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.enums import JobState

logger = get_logger(__name__)

# ADR-029 §C — точка хранения task_id живой Celery-таски джобы для best-effort revoke
# reconciler'ом при терминализации (Redis-ключ job_task:{job_id}, без миграции). TTL должен
# быть больше STUCK_THRESHOLD_S, чтобы ключ переживал максимальный простой до fail-stuck и
# reconciler нашёл task_id. Запись здесь (единый чокпойнт постановки таски), чтение+revoke — в
# beat_tasks._maybe_revoke_live_task.
_JOB_TASK_KEY_PREFIX = "job_task:"


def job_task_key(job_id: str) -> str:
    """Redis-ключ хранения task_id живой таски джобы (ADR-029 §C)."""
    return f"{_JOB_TASK_KEY_PREFIX}{job_id}"


def _record_job_task(job_id: str, task_id: str) -> None:
    """Best-effort запись task_id поставленной таски в Redis job_task:{job_id} (ADR-029 §C).

    Синхронный redis-клиент (dispatch_for_state — sync, вызывается из ASGI-loop сервисов и из
    тел Celery-задач; async-Redis тут вызвал бы loop-affinity-проблему, observability §7). TTL =
    STUCK_THRESHOLD_S + RECONCILE_INTERVAL_S — переживает максимальный простой до fail-stuck плюс
    период reconciler. Промах (Redis недоступен) НЕ валит постановку таски: revoke — оптимизация,
    корректность держит CAS-барьер transition() (ADR-029 §A).
    """
    settings = get_settings()
    ttl = settings.stuck_threshold_s + settings.reconcile_interval_s
    try:
        client = redis.Redis.from_url(settings.redis_url)
        try:
            client.set(job_task_key(job_id), task_id, ex=ttl)
        finally:
            client.close()
    except (redis.RedisError, OSError) as exc:
        logger.warning("job_task_record_failed", extra={"job_id": job_id, "error": str(exc)})


def dispatch_for_state(job_id: str, state: JobState, *, kind: str = "generation") -> None:
    """Ставит следующий task по состоянию. Безопасно для устойчивых состояний (no-op).

    kind различает стартовую таску в CREATED (Sprint 5, ADR-014): generation → task_interview
    (Agent 1 интервью), edit → task_edit (Agent 4 editor: спека + good-ревизия + instruction).
    Прочие состояния (SPECCING/BUILDING/DEPLOYING/FIXING) одинаковы для обоих kind — edit-цикл
    переиспускает ту же build/deploy/FIXING-машинерию (ADR-014 §A).

    ADR-029 §C: task_id поставленной таски пишется в Redis job_task:{job_id} (best-effort) —
    reconciler читает его для revoke живой таски при терминализации застрявшей джобы.
    """
    from app.workers import tasks

    if state == JobState.CREATED:
        if kind == "edit":
            result = tasks.task_edit.apply_async(args=[job_id], queue="llm")
        else:
            result = tasks.task_interview.apply_async(args=[job_id], queue="llm")
    elif state == JobState.SPECCING:
        result = tasks.task_spec.apply_async(args=[job_id], queue="llm")
    elif state == JobState.BUILDING:
        result = tasks.task_build_request.apply_async(args=[job_id], queue="build")
    elif state == JobState.DEPLOYING:
        result = tasks.task_deploy.apply_async(args=[job_id], queue="build")
    elif state == JobState.FIXING:
        # Sprint 2: восстановительный цикл — Agent 4 (queue=llm), гарды §C внутри таски.
        result = tasks.task_fix.apply_async(args=[job_id], queue="llm")
    else:
        # AWAITING_CLARIFICATION / LIVE / FAILED / INTERVIEWING — задач нет.
        logger.info("dispatch_noop", extra={"job_id": job_id, "state": state.value})
        return
    _record_job_task(job_id, result.id)
