"""Integration (Sprint 6, TD-007, ADR-016): переиспользуемый Redis ConnectionPool.

РЕАЛЬНЫЙ Redis обязателен (эфемерный контейнер из conftest, TEST_REDIS_URL) — пул нельзя
проверить моком: критично, что BlockingConnectionPool.make_connection не падает TypeError
на ПЕРВОЙ реальной операции (docs/observability §6, redis_pool.py docstring — базовый
ConnectionPool пробросил бы timeout в Connection.__init__ → TypeError).

Покрывает:
  - реальный проход через пул: reset_pool_for_tests → get_redis → incrbyfloat/get НЕ падает;
  - N операций rate_limit/sse → стабильное число соединений из одного пула (синглтон);
  - исчерпание max_connections: ожидание слота до timeout → ConnectionError (BlockingPool).

Внешних границ нет (Redis — реальный, единственная зависимость теста).
"""

from __future__ import annotations

import asyncio

import pytest
import redis.asyncio as aioredis

from app.observability import redis_pool

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("flush_redis")]


def _created_connections(pool: aioredis.BlockingConnectionPool) -> int:
    """Число реально созданных соединений BlockingConnectionPool.

    _available_connections — список, предзаполненный None-слотами до max_connections;
    реальные соединения — не-None записи. _in_use_connections — set занятых. Сумма = всего
    созданных пулом TCP-соединений (метрика переиспользования: не растёт линейно с операциями).
    """
    available = sum(1 for c in pool._available_connections if c is not None)
    return available + len(pool._in_use_connections)


async def test_pool_first_operation_does_not_raise_typeerror():
    """КРИТИЧНО (TD-007): первая реальная операция через пул НЕ падает TypeError на make_connection.

    reset_pool_for_tests() заставляет пул создаться на loop текущего теста; первый
    incrbyfloat/get реально дёргает BlockingConnectionPool.make_connection. Базовый
    ConnectionPool.from_url(..., timeout=...) пробросил бы timeout в Connection и упал бы
    TypeError здесь — тест подтверждает, что выбран BlockingConnectionPool (docstring §6).
    """
    redis_pool.reset_pool_for_tests()
    client = redis_pool.get_redis()
    # Первая операция — incrbyfloat (как budget-кэш), затем get.
    await client.incrbyfloat("pool:first-op", 1.5)
    raw = await client.get("pool:first-op")
    assert raw is not None
    assert float(raw) == pytest.approx(1.5)


async def test_pool_is_blocking_connection_pool():
    """Пул — BlockingConnectionPool (семантика timeout = ожидание свободного слота, не connect)."""
    redis_pool.reset_pool_for_tests()
    pool = redis_pool.get_pool()
    assert isinstance(pool, aioredis.BlockingConnectionPool)


async def test_get_pool_is_singleton_across_calls():
    """get_pool()/get_redis() переиспользуют ОДИН пул процесса (нет per-request from_url)."""
    redis_pool.reset_pool_for_tests()
    pool_a = redis_pool.get_pool()
    pool_b = redis_pool.get_pool()
    assert pool_a is pool_b
    # Клиенты разные, но поверх ОДНОГО connection_pool.
    c1 = redis_pool.get_redis()
    c2 = redis_pool.get_redis()
    assert c1.connection_pool is c2.connection_pool is pool_a


async def test_many_operations_reuse_stable_connection_count():
    """N операций rate_limit/sse-стиля → стабильное число соединений (переиспользование пула).

    Прогоняем серию операций через get_redis(); пул не должен создавать соединение на каждую
    (per-request connect — закрытый TD-007). Проверяем, что число созданных соединений мало и
    не растёт линейно с числом операций (стабильно, не пилообразно).
    """
    redis_pool.reset_pool_for_tests()
    client = redis_pool.get_redis()
    # Прогрев: одна операция создаёт соединение.
    await client.incr("pool:reuse")
    pool = redis_pool.get_pool()
    created_after_warmup = _created_connections(pool)  # все созданные пулом соединения
    # Множество последовательных операций — соединение возвращается в пул и переиспользуется.
    for _ in range(50):
        await client.incr("pool:reuse")
    created_after_load = _created_connections(pool)
    # Последовательные операции не плодят новые соединения (одно переиспользуется).
    assert created_after_load == created_after_warmup
    assert created_after_load <= 2  # стабильно мало, не ~50


async def test_concurrent_operations_bounded_by_pool():
    """Параллельные операции берут несколько соединений, но не больше max_connections."""
    redis_pool.reset_pool_for_tests()
    client = redis_pool.get_redis()
    pool = redis_pool.get_pool()

    async def _op(i: int) -> int:
        return int(await client.incr(f"pool:conc:{i % 5}"))

    await asyncio.gather(*[_op(i) for i in range(20)])
    # Число реально созданных соединений не превышает лимит пула.
    assert _created_connections(pool) <= pool.max_connections


async def test_pool_exhaustion_waits_then_connection_error(monkeypatch):  # noqa: ANN001
    """Исчерпание max_connections → блокирующее ожидание слота до timeout → ConnectionError.

    Урезаем пул до max_connections=1 и timeout малый; занимаем единственное соединение
    «вручную» (не возвращаем в пул), затем требуем второе — BlockingConnectionPool ждёт
    слот до timeout и бросает ConnectionError (семантика REDIS_POOL_TIMEOUT_S, docstring §6).
    """
    redis_pool.reset_pool_for_tests()
    pool = aioredis.BlockingConnectionPool.from_url(
        _redis_url(),
        max_connections=1,
        timeout=0.3,
    )
    # Берём единственное соединение и НЕ возвращаем (имитация занятого слота).
    conn = await pool.get_connection("PING")
    try:
        with pytest.raises((aioredis.ConnectionError, ConnectionError)):
            # Второй запрос соединения — пул пуст, ждём timeout → ConnectionError.
            await pool.get_connection("PING")
    finally:
        await pool.release(conn)
        await pool.disconnect()


def _redis_url() -> str:
    from app.core.config import get_settings

    return get_settings().redis_url
