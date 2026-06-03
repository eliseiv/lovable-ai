"""Redis budget-счётчик как read-through кэш-гейт (Sprint 6, TD-006, observability §5.2).

> Postgres остаётся source-of-truth бюджета (pipeline §C(b) не пересматривается). Redis-
  счётчик — read-through кэш ПЕРЕД чтением Postgres, не замена.

Контракт (нормативно §5.2):
  - Ключ budget:{job_id}, TTL = JOB_WALL_CLOCK_BUDGET_S (живёт не дольше джобы).
  - Запись: после каждой записи строки llm_usage воркер делает INCRBYFLOAT budget:{job_id}
    <cost_usd> (атомарный инкремент дельты стоимости вызова). Postgres — авторитет.
  - Чтение на гейте: GET budget:{job_id}; >= budget_usd → исчерпан без Postgres. Cache-miss
    (TTL истёк / Redis рестарт / crash-resume) → fallback: прочитать spend_usd из Postgres
    (source-of-truth) + пере-засеять ключ из БД (SET). Никогда не пропустить гейт без ключа.

Все соединения — через переиспользуемый ConnectionPool (TD-007, app.observability.redis_pool).
Метрика lovable_redis_pool_in_use{pool="budget"} подтверждает использование пула.
"""

from __future__ import annotations

from decimal import Decimal

from app.core.config import get_settings
from app.core.logging import get_logger
from app.observability import metrics
from app.observability.redis_pool import get_redis

logger = get_logger(__name__)


def _budget_key(job_id: str) -> str:
    return f"budget:{job_id}"


async def increment_budget(job_id: str, cost_usd: Decimal) -> None:
    """INCRBYFLOAT budget:{job_id} <cost_usd> после записи llm_usage (§5.2).

    Best-effort кэш-апдейт: Redis недоступен → log + продолжаем (Postgres авторитетен, гейт
    при cache-miss перечитает БД). TTL ставится при первом инкременте/пере-засеве.
    """
    settings = get_settings()
    client = get_redis()
    metrics.redis_pool_in_use.labels(pool="budget").inc()
    try:
        key = _budget_key(job_id)
        await client.incrbyfloat(key, float(cost_usd))
        await client.expire(key, settings.job_wall_clock_budget_s)
    except Exception as exc:  # noqa: BLE001 — кэш best-effort, Postgres source-of-truth
        logger.warning("budget_cache_incr_failed", extra={"job_id": job_id, "error": str(exc)})
    finally:
        metrics.redis_pool_in_use.labels(pool="budget").dec()


async def get_cached_budget(job_id: str) -> Decimal | None:
    """GET budget:{job_id} → потраченное по кэшу. None при cache-miss/ошибке (→ fallback на БД)."""
    client = get_redis()
    metrics.redis_pool_in_use.labels(pool="budget").inc()
    try:
        raw = await client.get(_budget_key(job_id))
        if raw is None:
            return None
        return Decimal(str(raw.decode() if isinstance(raw, bytes) else raw))
    except Exception as exc:  # noqa: BLE001 — cache-miss → fallback на Postgres
        logger.warning("budget_cache_get_failed", extra={"job_id": job_id, "error": str(exc)})
        return None
    finally:
        metrics.redis_pool_in_use.labels(pool="budget").dec()


async def budget_exhausted(job_id: str, spend_usd: Decimal, budget_usd: Decimal) -> bool:
    """Read-through кэш-гейт бюджета (§5.2): True, если бюджет джобы исчерпан.

    Сначала GET budget:{job_id} (быстрый кэш). Cache-hit → сравнение с budget_usd без
    Postgres. Cache-miss (TTL истёк / Redis рестарт / crash-resume) → fallback на переданный
    Postgres spend_usd (source-of-truth) + пере-засев ключа из БД-значения. Никогда не
    пропускает гейт из-за отсутствия Redis-ключа.

    spend_usd передаётся вызывающим из уже-загруженной джобы (Postgres-авторитет) —
    fallback не делает лишнего обращения к БД.
    """
    cached = await get_cached_budget(job_id)
    if cached is None:
        # Cache-miss → авторитет Postgres + пере-засев ключа из БД-значения.
        await reseed_budget(job_id, spend_usd)
        return spend_usd >= budget_usd
    return cached >= budget_usd


async def reseed_budget(job_id: str, spend_usd: Decimal) -> None:
    """Пере-засев budget:{job_id} из Postgres-spend_usd при cache-miss (§5.2, source-of-truth)."""
    settings = get_settings()
    client = get_redis()
    metrics.redis_pool_in_use.labels(pool="budget").inc()
    try:
        key = _budget_key(job_id)
        await client.set(key, float(spend_usd), ex=settings.job_wall_clock_budget_s)
    except Exception as exc:  # noqa: BLE001 — best-effort reseed; БД остаётся авторитетной
        logger.warning("budget_cache_reseed_failed", extra={"job_id": job_id, "error": str(exc)})
    finally:
        metrics.redis_pool_in_use.labels(pool="budget").dec()
