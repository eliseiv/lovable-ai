"""Graceful-fail шага агента при недоступности LLM (ADR-019 §G, docs/pipeline §G).

Инвариант ADR-019: ни одна агент-таска не имеет «пути в никуда» — любой исход ведёт либо к
продвижению state, либо к терминальному FAILED. Этот модуль реализует терминализацию джобы
при недоступности Claude как страховку поверх Celery-ретраев (ADR-006 / docs §D):

- **Транзиентные** сбои Claude (429/5xx/timeout/connection) ретраятся Celery (autoretry,
  exponential backoff, max_retries=5, §D). При **исчерпании** ретраев таска ОБЯЗАНА сделать
  graceful-переход в FAILED(agent_unavailable), а не оставить джобу в активном state.
- **Не-транзиентные** сбои Claude (401/403/400 — ключ отсутствует/невалиден, нет прав,
  bad request) НЕ ретраятся: таска немедленно делает graceful-переход в
  FAILED(agent_unavailable) без сжигания max_retries.
- Исчерпание ретраев на **не-LLM** инфра-сбое (Docker/S3/БД/Redis) → FAILED(infra_error)
  (вина окружения, не кода сайта) — отдельный reason-код от LLM-недоступности.

`run_agent_task` оборачивает sync-тело bound Celery-таски: запускает async-логику в
asyncio.run и решает, ретраить (поднять исключение для autoretry) или терминализировать.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

from celery import Task

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.enums import JobState
from app.db.session import session_scope, worker_engine_scope
from app.observability.redis_pool import worker_redis_scope
from app.pipeline.events import fail_job, load_job
from app.workers.retry_policy import (
    MAX_RETRIES,
    is_llm_failure,
    is_non_retryable_llm_failure,
    is_transient,
    llm_credential_present,
)

logger = get_logger(__name__)


async def _graceful_fail_job(job_id: str, failure_reason: str) -> None:
    """Терминализирует джобу в FAILED(failure_reason), освобождая concurrency-слот (§G).

    Идемпотентно: если джоба уже терминальна (LIVE/FAILED) или исчезла — no-op (повторная
    доставка / гонка с reconciler-страховкой §E2 безопасны).

    observability §7: вызывается из отдельного `asyncio.run` (в run_agent_task), поэтому
    обёрнута в `worker_engine_scope()` — per-task engine внутри ЭТОГО loop'а, а не глобальный
    (asyncpg `Future attached to a different loop`). `session_scope()` подхватывает per-task
    sessionmaker из ContextVar. Аналогично `worker_redis_scope()` (ADR-019 §Fix): per-task
    async-Redis клиент внутри ЭТОГО loop'а — `fail_job→transition→publish_event` дёргает Redis,
    глобальный ASGI-пул дал бы `RuntimeError: Event loop is closed`.
    """
    async with worker_engine_scope(), worker_redis_scope(), session_scope() as session:
        job = await load_job(session, job_id)
        if job is None:
            logger.info("graceful_fail_job_missing", extra={"job_id": job_id})
            return
        if job.state in (JobState.LIVE, JobState.FAILED):
            logger.info(
                "graceful_fail_noop_terminal",
                extra={"job_id": job_id, "state": job.state.value},
            )
            return
        await fail_job(session, job, failure_reason=failure_reason)
        logger.warning(
            "agent_graceful_fail",
            extra={"job_id": job_id, "failure_reason": failure_reason},
        )


def run_agent_task(
    task: Task,
    coro_factory: Callable[[], Coroutine[Any, Any, Any]],
    job_id: str,
    *,
    requires_llm: bool = False,
) -> None:
    """Запускает async-тело агент-таски с graceful-fail при недоступности LLM (ADR-019 §G).

    `task` — bound Celery-таска (bind=True) для доступа к task.request.retries. `coro_factory`
    создаёт корутину тела таски (новая корутина на каждый вызов — для повторного asyncio.run).
    `requires_llm` — таска обращается к Anthropic SDK (interview/spec/fix/edit): для неё
    включается per-job fail-fast preflight LLM-credential (§Fix round 3, п.1). Build/deploy-таски
    Claude не зовут → `requires_llm=False`, preflight не применяется.

    **Per-job fail-fast preflight LLM-credential (основной путь, §Fix round 3 п.1).** Для
    LLM-тасок ПЕРЕД запуском тела (до первого обращения к Anthropic SDK) проверяется непустой
    `Settings.anthropic_api_key`. Если пусто/whitespace-only → немедленный graceful-переход
    FAILED(agent_unavailable) ТЕМ ЖЕ транзакционным путём, что graceful-fail ниже, БЕЗ вызова
    тела/SDK и БЕЗ ретраев. Это единая точка (общая обёртка) — классификация не дублируется в
    коде агентов. Version-agnostic: не зависит от типа/текста встроенного `TypeError` SDK,
    детерминированно ловит самый частый прод-кейс (сервис намеренно запущен без ключа). Уровень —
    per-job (НЕ отказ старта сервиса: app/worker обязаны подниматься без ключа, §Fix round 3).

    Логика классификации исключения — единственная точка решения retry_policy (docs §D/§G):
    - не-транзиентный LLM-сбой (401/403/400) → немедленный FAILED(agent_unavailable), без
      ретрая (исключение НЕ пробрасывается → Celery не ретраит);
    - транзиентный сбой и ретраи НЕ исчерпаны → пробрасываем исключение, чтобы сработал
      Celery autoretry (TRANSIENT_EXCEPTIONS в _RETRY_KWARGS, exponential backoff);
    - транзиентный сбой и ретраи исчерпаны → graceful-fail: LLM-сбой → agent_unavailable,
      не-LLM инфра → infra_error;
    - не-транзиентное НЕ-LLM исключение (баг/доменное) → пробрасываем как есть (не маскируем).

    observability §7 (ADR-019): тело задачи исполняется через `asyncio.run` под
    `worker_engine_scope()` + `worker_redis_scope()` — per-task async-engine И per-task async-Redis
    клиент внутри этого loop'а (НЕ глобальные FastAPI-engine/ASGI-Redis-пул, привязанные к чужому
    loop). `session_scope()`/`get_redis()` в теле подхватывают per-task ресурсы из ContextVar.
    Без per-task Redis `publish_event()` из `transition()` падал бы `RuntimeError: Event loop is
    closed` на втором таске того же воркера → джоба зависала в активном state, лочила слот
    (прод-инцидент ADR-019 §Fix п.1). graceful-fail ниже идёт отдельным `asyncio.run` и сам
    оборачивается в worker_engine_scope + worker_redis_scope (см. _graceful_fail_job).
    """

    # Per-job fail-fast preflight LLM-credential (§Fix round 3 п.1, основной путь): для
    # LLM-тасок отсекаем пустой/whitespace-only ANTHROPIC_API_KEY ДО вызова тела/SDK —
    # немедленный graceful FAILED(agent_unavailable) без ретраев, освобождая слот за секунды
    # (а не через reconciler-TTL). Read-only Settings (без секретов в логе) — get_settings()
    # вне worker_engine_scope: _graceful_fail_job сам открывает per-task engine/redis-scope.
    if requires_llm and not llm_credential_present(
        get_settings().anthropic_api_key.get_secret_value()
    ):
        logger.warning("agent_preflight_no_credential", extra={"job_id": job_id})
        asyncio.run(_graceful_fail_job(job_id, "agent_unavailable"))
        return

    async def _run_in_engine_scope() -> None:
        async with worker_engine_scope(), worker_redis_scope():
            await coro_factory()

    try:
        asyncio.run(_run_in_engine_scope())
    except BaseException as exc:  # noqa: BLE001 — классифицируем и решаем терминализацию/ретрай
        # Не-транзиентный сбой Claude (401/403/400): немедленный graceful-fail без ретраев.
        if is_non_retryable_llm_failure(exc):
            asyncio.run(_graceful_fail_job(job_id, "agent_unavailable"))
            return
        if is_transient(exc):
            retries = getattr(task.request, "retries", 0) or 0
            if retries < MAX_RETRIES:
                # Ретраи не исчерпаны — пробрасываем для Celery autoretry (ADR-006).
                raise
            # Ретраи исчерпаны: LLM-недоступность → agent_unavailable, иначе infra_error (§G/§D).
            reason = "agent_unavailable" if is_llm_failure(exc) else "infra_error"
            asyncio.run(_graceful_fail_job(job_id, reason))
            return
        # Не-транзиентное не-LLM исключение (программная ошибка/неклассифицированное) —
        # пробрасываем без маскирования: оно не должно тихо терминализировать джобу.
        raise
