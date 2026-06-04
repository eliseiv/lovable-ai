"""Билинг-агрегаты для GET /billing/me и админ GET /admin/users/{user_id}.

Единый источник вычисления квоты/остатков пользователя (docs/modules/billing/02 §2 +
§10.4, ADR-021). Учитывает бонус-кредиты (users.bonus_generations_balance):
generations_remaining = max(0, monthly_generations - generations_used) + bonus_balance;
bonus_generations_remaining = bonus_balance. Переиспользуется админ-плоскостью за указанного
user_id (а не за текущего Bearer).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.billing import entitlements, usage
from app.billing.subscription_state import (
    DEFAULT_ACCESS_LEVEL,
    STATUS_ACTIVE,
    get_subscription,
)
from app.db.models import User
from app.schemas.api import BillingQuota


@dataclass(frozen=True)
class BillingSnapshot:
    """Снимок тарифа+квоты пользователя (общий для billing/me и admin GET)."""

    access_level: str
    status: str
    period: str
    quota: BillingQuota


async def build_billing_snapshot(session: AsyncSession, user: User) -> BillingSnapshot:
    """Тариф + остаток квоты пользователя с учётом бонус-кредитов (docs §2/§10.4).

    resolve_entitlement выполняет lazy-ресинк при протухании (fail-open на кэш). Бонус-баланс
    читается из user.bonus_generations_balance (денормализованный O(1)-баланс, ADR-021).
    """
    ent = await entitlements.resolve_entitlement(session, user.id)
    quota = await entitlements.get_plan_quota(session, ent.access_level)

    period = usage.current_period()
    generations_used = await usage.get_usage(session, user.id, period)
    edits_used = await usage.get_edit_usage(session, user.id, period)
    active_jobs = await entitlements.count_active_jobs(session, user.id)
    projects_used = await entitlements.count_projects(session, user.id)
    max_concurrent = await entitlements.resolve_max_concurrent_jobs(session, user.id)

    if quota is not None:
        monthly_generations = quota.monthly_generations
        max_projects = quota.max_projects
        monthly_edits = quota.monthly_edits
    else:
        # plan_quotas не сидирован — деградируем к нулевому потолку (явный сигнал, не падаем).
        monthly_generations = 0
        max_projects = None
        monthly_edits = None

    # edits_remaining: None при безлимите (Pro, monthly_edits=NULL), иначе max(0, лимит-исп.).
    edits_remaining = None if monthly_edits is None else max(0, monthly_edits - edits_used)

    # ADR-021 §D/§10.4: бонус-кредиты учитываются ТОЛЬКО для генераций.
    bonus_balance = user.bonus_generations_balance
    plan_remaining = max(0, monthly_generations - generations_used)
    generations_remaining = plan_remaining + bonus_balance

    sub = await get_subscription(session, user.id)
    access_level = sub.access_level if sub is not None else DEFAULT_ACCESS_LEVEL
    sub_status = sub.status if sub is not None else STATUS_ACTIVE

    return BillingSnapshot(
        access_level=access_level,
        status=sub_status,
        period=period,
        quota=BillingQuota(
            monthly_generations=monthly_generations,
            generations_used=generations_used,
            generations_remaining=generations_remaining,
            bonus_generations_remaining=bonus_balance,
            monthly_edits=monthly_edits,
            edits_used=edits_used,
            edits_remaining=edits_remaining,
            max_concurrent_jobs=max_concurrent,
            active_jobs=active_jobs,
            max_projects=max_projects,
            projects_used=projects_used,
        ),
    )


__all__ = ["BillingSnapshot", "build_billing_snapshot"]
