"""Integration (Sprint 6, TD-006, ADR-015 §5.2): Redis budget-кэш как read-through гейт.

РЕАЛЬНЫЙ Redis (conftest эфемерный): INCRBYFLOAT/EXPIRE/GET/SET реально применяются (нельзя
проверить моком — контракт TTL/ключа budget:{job_id}). Postgres остаётся source-of-truth.

Покрывает:
  - increment_budget: INCRBYFLOAT + EXPIRE реально применены (ключ budget:{job_id} существует,
    TTL выставлен = JOB_WALL_CLOCK_BUDGET_S, накапливается по нескольким вызовам);
  - cache-hit: budget>=budget_usd → budget_exhausted True БЕЗ обращения к Postgres-fallback;
  - cache-miss: ключа нет → fallback на переданный Postgres spend_usd + пере-засев ключа (SET),
    гейт никогда не пропускается из-за отсутствия ключа;
  - reseed_budget восстанавливает ключ из БД-значения с TTL;
  - метрика lovable_redis_pool_in_use{pool="budget"} балансирует inc/dec (не течёт).

Внешних границ нет (Redis реальный; Postgres не нужен — spend_usd передаётся вызывающим).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.config import get_settings
from app.observability import budget_cache, redis_pool
from app.observability.redis_pool import get_redis

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("flush_redis")]


def _budget_pool_in_use() -> float:
    from prometheus_client import REGISTRY

    val = REGISTRY.get_sample_value("lovable_redis_pool_in_use", {"pool": "budget"})
    return val or 0.0


async def test_increment_budget_sets_key_and_ttl():
    """increment_budget: INCRBYFLOAT + EXPIRE реально применены (ключ есть, TTL ≈ wall-clock)."""
    redis_pool.reset_pool_for_tests()
    job_id = "j_budget_incr"
    await budget_cache.increment_budget(job_id, Decimal("1.25"))

    client = get_redis()
    raw = await client.get(f"budget:{job_id}")
    assert raw is not None
    assert float(raw) == pytest.approx(1.25)

    ttl = await client.ttl(f"budget:{job_id}")
    wall_clock = get_settings().job_wall_clock_budget_s
    # TTL выставлен и не превышает wall-clock budget (живёт не дольше джобы, §5.2).
    assert 0 < ttl <= wall_clock


async def test_increment_budget_accumulates():
    """Несколько increment_budget атомарно накапливают стоимость (INCRBYFLOAT-дельты)."""
    redis_pool.reset_pool_for_tests()
    job_id = "j_budget_accum"
    await budget_cache.increment_budget(job_id, Decimal("0.50"))
    await budget_cache.increment_budget(job_id, Decimal("0.75"))
    await budget_cache.increment_budget(job_id, Decimal("1.00"))

    cached = await budget_cache.get_cached_budget(job_id)
    assert cached is not None
    assert cached == pytest.approx(Decimal("2.25"))


async def test_budget_exhausted_cache_hit_true_without_db():
    """Cache-hit budget>=budget_usd → budget_exhausted True; spend_usd-fallback НЕ используется.

    Засеваем кэш ВЫШЕ лимита; передаём Postgres spend_usd=0 (намеренно «несогласованный»),
    чтобы доказать: при cache-hit решение принимается по кэшу, БД-значение не читается.
    """
    redis_pool.reset_pool_for_tests()
    job_id = "j_budget_hit"
    await budget_cache.increment_budget(job_id, Decimal("6.00"))  # > budget 5

    exhausted = await budget_cache.budget_exhausted(
        job_id, spend_usd=Decimal("0.00"), budget_usd=Decimal("5.00")
    )
    assert exhausted is True


async def test_budget_exhausted_cache_hit_false_under_limit():
    """Cache-hit budget<budget_usd → не исчерпан (гейт пропускает)."""
    redis_pool.reset_pool_for_tests()
    job_id = "j_budget_under"
    await budget_cache.increment_budget(job_id, Decimal("2.00"))
    exhausted = await budget_cache.budget_exhausted(
        job_id, spend_usd=Decimal("99.0"), budget_usd=Decimal("5.00")
    )
    # Cache-hit (2.0 < 5.0) — fallback на Postgres spend_usd=99 НЕ применяется.
    assert exhausted is False


async def test_budget_exhausted_cache_miss_fallbacks_to_postgres_and_reseeds():
    """Cache-miss → fallback на Postgres spend_usd + пере-засев ключа (гейт не пропущен)."""
    redis_pool.reset_pool_for_tests()
    job_id = "j_budget_miss"
    client = get_redis()
    # Гарантируем отсутствие ключа (TTL истёк / Redis рестарт / crash-resume).
    assert await client.get(f"budget:{job_id}") is None

    # Postgres-авторитет spend_usd=7 >= budget 5 → исчерпан, несмотря на пустой кэш.
    exhausted = await budget_cache.budget_exhausted(
        job_id, spend_usd=Decimal("7.00"), budget_usd=Decimal("5.00")
    )
    assert exhausted is True
    # Пере-засев: ключ восстановлен из БД-значения (последующий гейт — cache-hit).
    raw = await client.get(f"budget:{job_id}")
    assert raw is not None
    assert float(raw) == pytest.approx(7.00)


async def test_budget_exhausted_cache_miss_under_limit_not_exhausted():
    """Cache-miss + Postgres spend_usd<budget → не исчерпан, ключ пере-засеян из БД."""
    redis_pool.reset_pool_for_tests()
    job_id = "j_budget_miss_under"
    exhausted = await budget_cache.budget_exhausted(
        job_id, spend_usd=Decimal("1.50"), budget_usd=Decimal("5.00")
    )
    assert exhausted is False
    cached = await budget_cache.get_cached_budget(job_id)
    assert cached == pytest.approx(Decimal("1.50"))


async def test_reseed_budget_sets_key_with_ttl():
    """reseed_budget восстанавливает budget:{job_id} из Postgres-значения с TTL."""
    redis_pool.reset_pool_for_tests()
    job_id = "j_budget_reseed"
    await budget_cache.reseed_budget(job_id, Decimal("3.33"))
    client = get_redis()
    raw = await client.get(f"budget:{job_id}")
    assert float(raw) == pytest.approx(3.33)
    ttl = await client.ttl(f"budget:{job_id}")
    assert 0 < ttl <= get_settings().job_wall_clock_budget_s


async def test_redis_pool_in_use_balanced_after_budget_ops():
    """lovable_redis_pool_in_use{pool=budget} возвращается к 0 после операций (inc/dec парны)."""
    redis_pool.reset_pool_for_tests()
    before = _budget_pool_in_use()
    await budget_cache.increment_budget("j_balance", Decimal("0.10"))
    await budget_cache.get_cached_budget("j_balance")
    await budget_cache.reseed_budget("j_balance", Decimal("0.20"))
    after = _budget_pool_in_use()
    # Каждая операция inc().. .dec() в finally — gauge возвращается к исходному уровню.
    assert after == pytest.approx(before)
