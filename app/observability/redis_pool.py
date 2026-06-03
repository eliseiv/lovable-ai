"""Переиспользуемый Redis ConnectionPool / клиент-синглтон (Sprint 6, TD-007, ADR-016).

Закрывает TD-007: per-request `aioredis.from_url(...)` + `aclose()` в hot-path
(rate-limit / SSE / budget / events) → лишний TCP-connect/teardown на каждый запрос под
масштабом. Вместо этого — единый `ConnectionPool` на процесс (web/worker), клиенты
переиспользуют соединения пула.

Нормативные правила (docs/modules/observability/03-architecture.md §6, ADR-016 §4):
  - один пул на процесс, параметры из env `REDIS_POOL_MAX_CONNECTIONS`/`REDIS_POOL_TIMEOUT_S`;
  - клиенты, полученные `get_redis()`, НЕ закрываются вызывающими (закрывает соединение в
    пул, не TCP) — соединение возвращается в пул для переиспользования;
  - наблюдаемость занятых соединений — `lovable_redis_pool_in_use{pool}` (метрика §2.6)
    обновляется на точке использования (rate_limit/sse/budget), не здесь.

Pub/sub (SSE) требует выделенного соединения на стрим — для него отдельный клиент из того
же пула (`get_redis()` создаёт `Redis(connection_pool=...)`; `pubsub()` берёт соединение из
пула на время подписки и возвращает при `aclose()` pubsub). Пул переиспользуется.

## Разведение ASGI-пути и воркерного пути (observability §7.0–7.2, ADR-019 §Fix, ADR-016 §Уточнение)

Прод-инцидент (`corelysite.shop`, 2026-06-04): глобальный async-Redis `ConnectionPool`-
синглтон (ниже) привязан к event loop, на котором впервые создал соединение. Celery-задача
исполняет async-код через `asyncio.run` (НОВЫЙ loop на каждый вызов); соединение из глобального
пула, взятое из чужого/закрытого loop'а второй задачи, даёт `RuntimeError: Event loop is closed`
/ `Future attached to a different loop` внутри `publish_event()` → таска падала ДО вызова Claude
→ джоба зависала в `INTERVIEWING`, лочила concurrency-слот (тот же leak, что закрывает ADR-019).

Нормативное разведение (observability §7.2):
  - **ASGI-путь FastAPI** (rate-limit / SSE / budget hot-path, долгоживущий ASGI-loop) —
    глобальный `BlockingConnectionPool`-синглтон (`get_pool`/глобальный `get_redis`). Остаётся,
    НЕ меняется (ADR-016 п.4).
  - **Celery worker/beat-путь** (`asyncio.run`-loop задачи) — per-task async-Redis клиент/пул,
    созданный ВНУТРИ loop'а задачи и `aclose()`/`disconnect()`-ящийся в `finally` той же
    корутины (по аналогии с `worker_engine_scope` для DB-engine). Биндинг — через `ContextVar`
    (`worker_redis_scope`). Соединение принадлежит ТЕКУЩЕМУ loop'у и не переживает его.

Это **физически разные объекты** (observability §7.2): они не пересекаются. `get_redis()`
прозрачно отдаёт per-task клиент при активном `worker_redis_scope` (тело Celery-задачи), иначе —
клиент глобального ASGI-пула. Так `publish_event`/`budget_cache`/SSE/`rate_limit` не меняют свой
код вызова, но в Celery-контексте получают loop-локальный клиент (корневой фикс ADR-019 §Fix п.1).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar

import redis.asyncio as aioredis

from app.core.config import get_settings

_pool: aioredis.ConnectionPool | None = None

# Per-task async-Redis клиент активной синхронной Celery-задачи (worker_redis_scope). При не-None
# `get_redis()` отдаёт ЕГО (per-task клиент в asyncio.run-loop задачи), а не клиент глобального
# ASGI-пула (привязан к чужому loop → `RuntimeError: Event loop is closed`, observability §7).
# Хранит сам клиент (loop-локальный объект) — не пул процесса. Дефолт None: вне Celery-задачи
# (ASGI-путь FastAPI / скрипты / тесты) активен глобальный пул.
_task_redis_client: ContextVar[aioredis.Redis | None] = ContextVar(
    "task_redis_client", default=None
)


def get_pool() -> aioredis.ConnectionPool:
    """Единый ConnectionPool процесса (lazy-init из env REDIS_POOL_*). Синглтон.

    max_connections/timeout — из Settings (REDIS_POOL_MAX_CONNECTIONS/REDIS_POOL_TIMEOUT_S).
    Создаётся один раз; повторные вызовы возвращают тот же пул.

    Используется `BlockingConnectionPool`: его `timeout` имеет нужную семантику
    REDIS_POOL_TIMEOUT_S по docs/07 — «таймаут ожидания свободного СОЕДИНЕНИЯ из пула»
    (исчерпан `max_connections` → блокирующее ожидание свободного слота до `timeout`, далее
    `ConnectionError`). У базового `ConnectionPool` параметра ожидания слота нет, а его
    `from_url(..., timeout=...)` пробросил бы `timeout` в `Connection.__init__` как
    connection_kwarg (у `Connection` нет такого параметра) → `TypeError` при первой реальной
    операции (`make_connection`). Поэтому именно `BlockingConnectionPool`.
    """
    global _pool
    if _pool is None:
        settings = get_settings()
        _pool = aioredis.BlockingConnectionPool.from_url(
            settings.redis_url,
            max_connections=settings.redis_pool_max_connections,
            timeout=settings.redis_pool_timeout_s,
        )
    return _pool


def get_redis() -> aioredis.Redis:
    """Context-aware Redis-клиент (observability §7.0–7.2, ADR-019 §Fix, ADR-016 §Уточнение).

    - **Celery worker/beat-путь** (активен `worker_redis_scope`): отдаёт per-task клиент из
      `ContextVar` — он создан ВНУТРИ asyncio.run-loop текущей задачи и принадлежит ему. Иначе
      клиент глобального ASGI-пула (привязан к чужому/закрытому loop'у) дал бы внутри
      `publish_event`/budget `RuntimeError: Event loop is closed` (прод-инцидент ADR-019 §Fix).
    - **ASGI-путь FastAPI** (нет активного per-task scope): клиент поверх переиспользуемого
      `BlockingConnectionPool`-синглтона процесса (rate-limit/SSE/budget hot-path, TD-007).

    Вызывающий НЕ закрывает клиент/пул: на ASGI-пути соединение возвращается в пул процесса
    (закрывает пул только `close_pool` на shutdown); per-task клиент закрывает `worker_redis_scope`
    в `finally`. Код вызова (publish_event/budget_cache/sse/rate_limit) одинаков для обоих путей.
    """
    task_client = _task_redis_client.get()
    if task_client is not None:
        return task_client
    return aioredis.Redis(connection_pool=get_pool())


@asynccontextmanager
async def worker_redis_scope() -> AsyncIterator[None]:
    """Per-task async-Redis клиент синхронной Celery-задачи (observability §7.1.3, ADR-019 §Fix).

    Нормативный паттерн §7 для async-Redis в Celery (по аналогии с `worker_engine_scope` для
    DB-engine): клиент/пул, используемый из тела задачи (`publish_event` и любые async-Redis
    вызовы — SSE-publish, budget INCRBYFLOAT), создаётся ВНУТРИ текущего asyncio.run-loop задачи,
    биндится в `ContextVar` (`get_redis()` его подхватывает) и `aclose()`/`disconnect()`-ится в
    `finally` той же корутины — соединения redis не переживают loop и не «прилипают» к чужому.

    Глобальный ASGI `BlockingConnectionPool`-синглтон (`get_pool`) тут НЕ используется (он —
    путь FastAPI, observability §7.2): per-task пул свой, короткоживущий в пределах задачи.
    Параметры — те же env `REDIS_POOL_MAX_CONNECTIONS`/`REDIS_POOL_TIMEOUT_S` (07-deployment);
    per-task размер может быть меньше (соединения живут лишь в пределах таски).

    Обязателен для ВСЕХ Celery-задач, чьё тело дёргает async-Redis (`publish_event`/SSE-publish/
    budget) — оборачивается вокруг тела внутри `asyncio.run` (run_agent_task / beat-задачи). Токен
    ContextVar сбрасывается на выходе (вложенность/повторный asyncio.run безопасны).
    """
    settings = get_settings()
    pool: aioredis.ConnectionPool = aioredis.BlockingConnectionPool.from_url(
        settings.redis_url,
        max_connections=settings.redis_pool_max_connections,
        timeout=settings.redis_pool_timeout_s,
    )
    client = aioredis.Redis(connection_pool=pool)
    token = _task_redis_client.set(client)
    try:
        yield
    finally:
        _task_redis_client.reset(token)
        # aclose() возвращает соединения клиента; disconnect() закрывает все соединения пула —
        # оба в ТОМ ЖЕ loop'е задачи (соединения не переживают asyncio.run). redis 5.x async.
        await client.aclose()  # type: ignore[attr-defined]  # redis 5.x async; stub устарел
        await pool.disconnect()


async def close_pool() -> None:
    """Закрывает пул процесса (shutdown). Идемпотентно: повторный вызов — no-op."""
    global _pool
    if _pool is not None:
        await _pool.disconnect()
        _pool = None


def reset_pool_for_tests() -> None:
    """Сброс синглтона пула (тест-хук; qa переинициализирует пул на эфемерный Redis)."""
    global _pool
    _pool = None
