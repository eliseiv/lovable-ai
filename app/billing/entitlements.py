"""Entitlements: реальный access_level из subscriptions (замена S3-заглушки free).

docs/modules/billing/03-architecture.md §4, ADR-009 §D. resolve_access_level /
resolve_max_concurrent_jobs читают кэш subscriptions (lazy-ресинк при протухании);
plan_quotas — источник лимитов. Заменяют хардкод free в модуле auth (concurrency-cap).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing import subscription_state
from app.billing.resync import lazy_resync_if_stale
from app.core.logging import get_logger
from app.db.enums import PAUSED_STATES, JobState
from app.db.models import GenerationJob, PlanQuota
from app.db.models import Project as ProjectModel

logger = get_logger(__name__)

DEFAULT_ACCESS_LEVEL = subscription_state.DEFAULT_ACCESS_LEVEL
# Активны = НЕ в этих устойчивых/терминальных состояниях (docs auth §6).
_INACTIVE_STATES: frozenset[JobState] = PAUSED_STATES
# Фолбэк-дефолты, если plan_quotas не сидирован (миграция 0004 сидит free/pro).
_FALLBACK_MAX_CONCURRENT: dict[str, int] = {"free": 1, "pro": 3}


@dataclass(frozen=True)
class Entitlement:
    """Текущий тариф пользователя: access_level + status (для quota-gate/billing/me)."""

    access_level: str
    status: str

    @property
    def gate_passes(self) -> bool:
        """status ∈ {active, grace} → проходит quota-gate (docs §4)."""
        return self.status in subscription_state.GATE_PASS_STATUSES


async def resolve_entitlement(session: AsyncSession, user_id: str) -> Entitlement:
    """access_level + status из subscriptions (lazy-ресинк при протухшем кэше, docs §3.2).

    Нет строки subscriptions → free/active (free-тариф всегда активен).
    """
    sub = await lazy_resync_if_stale(session, user_id)
    if sub is None:
        return Entitlement(
            access_level=DEFAULT_ACCESS_LEVEL, status=subscription_state.STATUS_ACTIVE
        )
    return Entitlement(access_level=sub.access_level, status=sub.status)


async def resolve_access_level(session: AsyncSession, user_id: str) -> str:
    """Реальный access_level пользователя (Adapty-кэш). Нет подписки → free."""
    return (await resolve_entitlement(session, user_id)).access_level


async def get_plan_quota(session: AsyncSession, access_level: str) -> PlanQuota | None:
    return await session.get(PlanQuota, access_level)


async def resolve_max_concurrent_jobs(session: AsyncSession, user_id: str) -> int:
    """Лимит конкурентных джоб по реальному access_level (plan_quotas.max_concurrent_jobs).

    Заменяет S3-заглушку free=1: значение берётся из plan_quotas по access_level из Adapty.
    """
    access_level = await resolve_access_level(session, user_id)
    quota = await get_plan_quota(session, access_level)
    if quota is not None and quota.max_concurrent_jobs is not None:
        return quota.max_concurrent_jobs
    return _FALLBACK_MAX_CONCURRENT.get(access_level, 1)


async def count_active_jobs(session: AsyncSession, user_id: str) -> int:
    """Число активных (нетерминальных) джоб пользователя (docs auth §6)."""
    result = await session.execute(
        select(func.count())
        .select_from(GenerationJob)
        .where(
            GenerationJob.user_id == user_id,
            GenerationJob.state.notin_(_INACTIVE_STATES),
        )
    )
    return int(result.scalar_one())


async def active_job_kinds(session: AsyncSession, user_id: str) -> list[str]:
    """kind активных (нетерминальных) джоб пользователя (TD-012: holder_kind concurrency-блока).

    Для метрики lovable_concurrency_block_by_kind_total{blocked_kind,holder_kind} — какой kind
    занимает слот max_concurrent_jobs (generation/edit/rollback). Порядок — по created_at.
    """
    result = await session.execute(
        select(GenerationJob.kind)
        .where(
            GenerationJob.user_id == user_id,
            GenerationJob.state.notin_(_INACTIVE_STATES),
        )
        .order_by(GenerationJob.created_at)
    )
    return list(result.scalars().all())


async def count_projects(session: AsyncSession, user_id: str) -> int:
    """Число активных проектов пользователя (для max_projects, docs §4).

    Soft-delete-фильтр (ADR-011): projects_used считает только deleted_at IS NULL —
    удаляемый/удалённый проект не занимает слот квоты max_projects.
    """
    result = await session.execute(
        select(func.count())
        .select_from(ProjectModel)
        .where(ProjectModel.user_id == user_id, ProjectModel.deleted_at.is_(None))
    )
    return int(result.scalar_one())
