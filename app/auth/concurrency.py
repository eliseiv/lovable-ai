"""Cap конкурентных генераций на user (docs/05-security.md, modules/auth §6).

Джоба «активна», пока не в терминальном/устойчивом LIVE/FAILED/AWAITING_CLARIFICATION.
Авторитет счётчика — Postgres COUNT по денормализованному generation_jobs.user_id.

⚠️ Sprint 3.5: РЕАЛЬНЫЙ энфорс конкурентности перенесён в `app.billing.quota_gate`
(FastAPI-dependency на POST /projects), который берёт access_level из subscriptions
(Adapty-кэш) и лимит из plan_quotas, и канонизирует превышение как 402 reason=
concurrency_limit (ADR-009 §D, docs/modules/billing/03 §4). Функции `count_active_jobs`/
`is_within_concurrency_cap` ниже сохранены как переиспользуемые DB-помощники; sync-стаб
`resolve_access_level`/`resolve_max_concurrent_jobs` (хардкод free) — legacy и в
production-пути запросов НЕ используется (его заменил `billing.entitlements`).
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import PAUSED_STATES, JobState
from app.db.models import GenerationJob

# Активны = НЕ в этих устойчивых/терминальных состояниях (docs §6).
_INACTIVE_STATES: frozenset[JobState] = PAUSED_STATES

# Дефолт free-tier (plan_quotas.max_concurrent_jobs для free, docs/03-data-model §plan_quotas).
# Источник числа — нормативная таблица сидинга plan_quotas (free=1 / pro=3).
_MAX_CONCURRENT_BY_ACCESS_LEVEL: dict[str, int] = {"free": 1, "pro": 3}
_DEFAULT_ACCESS_LEVEL = "free"


def resolve_access_level(_user_id: str) -> str:
    """Legacy sync-стаб тарифа (дефолт free). НЕ используется в production-пути с S3.5.

    Реальный access_level из Adapty-кэша подключён в `billing.entitlements.resolve_
    access_level` (async, по subscriptions). Этот sync-стаб сохранён только для обратной
    совместимости старых вызовов/тестов S3; новый код использует billing.entitlements.
    """
    return _DEFAULT_ACCESS_LEVEL


def resolve_max_concurrent_jobs(user_id: str) -> int:
    """Лимит конкурентных джоб по access_level (plan_quotas.max_concurrent_jobs)."""
    access_level = resolve_access_level(user_id)
    return _MAX_CONCURRENT_BY_ACCESS_LEVEL.get(access_level, 1)


async def count_active_jobs(session: AsyncSession, user_id: str) -> int:
    """Число активных джоб пользователя (NOT IN устойчивых/терминальных состояниях)."""
    result = await session.execute(
        select(func.count())
        .select_from(GenerationJob)
        .where(
            GenerationJob.user_id == user_id,
            GenerationJob.state.notin_(_INACTIVE_STATES),
        )
    )
    return int(result.scalar_one())


async def is_within_concurrency_cap(session: AsyncSession, user_id: str) -> bool:
    """True, если можно стартовать ещё одну генерацию (active < max_concurrent)."""
    active = await count_active_jobs(session, user_id)
    return active < resolve_max_concurrent_jobs(user_id)
