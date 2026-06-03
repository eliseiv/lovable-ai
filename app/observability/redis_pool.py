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
"""

from __future__ import annotations

import redis.asyncio as aioredis

from app.core.config import get_settings

_pool: aioredis.ConnectionPool | None = None


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
    """Redis-клиент поверх переиспользуемого пула процесса (TD-007).

    Вызывающий НЕ закрывает клиент/пул: операции возвращают соединение в пул автоматически.
    Закрывать пул целиком — только на shutdown процесса (`close_pool`).
    """
    return aioredis.Redis(connection_pool=get_pool())


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
