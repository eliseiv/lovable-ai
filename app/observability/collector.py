"""Периодический refresh «мгновенных» gauge-метрик (Sprint 6, ADR-015 §2.1/2.2/2.6).

Часть метрик — не событийные счётчики, а снимки состояния (gauge), которые нельзя
инкрементить на событии: их обновляет периодический beat-tick (единственный экземпляр beat,
ADR-016) пулл-моделью:
  - lovable_jobs_in_state{state,kind} — COUNT generation_jobs по (state, kind);
  - lovable_queue_depth{queue} — длина Redis-списка брокера Celery (llm/build);
  - lovable_worker_busy{queue} — занятые worker-слоты (Celery inspect active, best-effort);
  - lovable_user_spend_usd — суммарный Claude-spend всех юзеров (SUM llm_usage.cost_usd).

Beat экспонирует свой /metrics на METRICS_PORT (start_http_server). Pull-модель Prometheus
читает этот snapshot. Все операции best-effort: ошибка БД/Redis/inspect логируется, не валит
beat-tick (метрика обновится на следующем тике).
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import cast

import redis.asyncio as aioredis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.db.enums import JobState
from app.db.models import GenerationJob, LlmUsage
from app.db.session import task_engine_scope
from app.observability import metrics
from app.observability.redis_pool import get_redis

logger = get_logger(__name__)

# Имена Redis-списков брокера Celery = имена очередей (default Redis transport).
_QUEUES: tuple[str, ...] = ("llm", "build")


async def refresh_jobs_in_state(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """lovable_jobs_in_state{state,kind} = COUNT generation_jobs по (state, kind)."""
    async with sessionmaker() as session:
        result = await session.execute(
            select(GenerationJob.state, GenerationJob.kind, func.count()).group_by(
                GenerationJob.state, GenerationJob.kind
            )
        )
        rows = result.all()
    # Снимок (gauge): проставляем фактические (state, kind)-комбинации. Метрика
    # обновляется каждый beat-tick — устаревшие комбинации затухают в Grafana по времени.
    for state, kind, count in rows:
        state_value = state.value if isinstance(state, JobState) else str(state)
        metrics.jobs_in_state.labels(state=state_value, kind=kind).set(int(count))


async def _llen(client: aioredis.Redis, key: str) -> int:
    """LLEN с явным await (redis async-стаб типизирует возврат как Awaitable[int] | int)."""
    return int(await cast(Awaitable[int], client.llen(key)))


async def refresh_queue_depth() -> None:
    """lovable_queue_depth{queue} = длина Redis-списка брокера (отставание воркеров)."""
    client = get_redis()
    metrics.redis_pool_in_use.labels(pool="broker").inc()
    try:
        for queue in _QUEUES:
            depth = await _llen(client, queue)
            metrics.queue_depth.labels(queue=queue).set(depth)
    except Exception as exc:  # noqa: BLE001 — наблюдаемость best-effort
        logger.warning("queue_depth_refresh_failed", extra={"error": str(exc)})
    finally:
        metrics.redis_pool_in_use.labels(pool="broker").dec()


def refresh_worker_busy() -> None:
    """lovable_worker_busy{queue} = занятые worker-слоты (Celery inspect active, best-effort).

    Sync (Celery inspect — sync API). Недоступность воркеров/inspect → 0 (не падаем).
    Агрегируем active-таски по очереди delivery_info.routing_key.
    """
    from app.workers.celery_app import celery_app

    busy: dict[str, int] = dict.fromkeys(_QUEUES, 0)
    try:
        inspector = celery_app.control.inspect(timeout=2.0)
        active = inspector.active() or {}
        for tasks in active.values():
            for task in tasks:
                routing = (task.get("delivery_info") or {}).get("routing_key")
                if routing in busy:
                    busy[routing] += 1
    except Exception as exc:  # noqa: BLE001 — inspect недоступен → 0, не валим beat
        logger.warning("worker_busy_refresh_failed", extra={"error": str(exc)})
    for queue, count in busy.items():
        metrics.worker_busy.labels(queue=queue).set(count)


async def refresh_user_spend(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """lovable_user_spend_usd = суммарный Claude-spend всех юзеров (SUM llm_usage.cost_usd).

    Агрегат БЕЗ per-user label (кардинальность — §2.2): per-user $-панели строятся из
    Postgres-datasource в Grafana, не из Prometheus.
    """
    async with sessionmaker() as session:
        total = await session.scalar(select(func.coalesce(func.sum(LlmUsage.cost_usd), 0)))
    try:
        metrics.user_spend_usd.set(float(total or 0))
    except (TypeError, ValueError):
        metrics.user_spend_usd.set(0.0)


async def refresh_all() -> None:
    """Единый refresh всех snapshot-gauge (вызывается beat-таской metrics.refresh).

    Beat-only: gauge экспонируются на beat-/metrics (METRICS_PORT); интервал refresh ≈
    scrape-интервал (beat_schedule, celery_app). Redis/inspect-коллекторы изолируют свои
    ошибки внутри (best-effort); БД-коллекторы (jobs_in_state/user_spend) при недоступности
    Postgres пробрасывают — beat-таска поглощает фейл тика (Celery логирует), следующий тик
    повторит. Метрика наблюдаемости не должна влиять на пайплайн.

    Observability §7 (прод-фикс `RuntimeError: Future attached to a different loop`):
    async-engine/asyncpg-пул для БД-коллекторов создаётся и dispose()-ится ВНУТРИ этого
    asyncio.run-loop через task_engine_scope (НЕ глобальный engine FastAPI/session.py,
    привязанный к чужому loop). Так asyncpg-соединения не переживают loop между запусками
    задачи. Redis-коллектор (queue_depth) берёт per-task Redis-клиент текущего asyncio.run-loop
    через worker_redis_scope (его открывает beat-таска metrics.refresh) — НЕ глобальный async-Redis
    пул процесса, который LOOP-BOUND и дал бы `Future attached to a different loop` между beat-
    тиками (observability §7.0/§7.2). worker_busy — sync Celery inspect, БД/loop не трогает.
    """
    async with task_engine_scope() as sessionmaker:
        await refresh_jobs_in_state(sessionmaker)
        await refresh_user_spend(sessionmaker)
    await refresh_queue_depth()
    refresh_worker_busy()
