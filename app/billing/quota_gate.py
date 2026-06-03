"""Quota-gate: проверка прав/квоты перед стартом генерации (docs/billing/03 §4).

FastAPI-dependency на POST /v1/projects (S3.5) и /edits (контракт, активен с S5).
Любое нарушение → 402 (RFC-7807) с reason. Проверки (docs §4):
  1. access_level активен (status ∈ {active, grace}; billing_issue/expired → no_entitlement).
  2. max_projects не превышен (только POST /projects; NULL=безлимит).
  3. max_concurrent_jobs не превышен → concurrency_limit (НЕ 429; 429 — только rate-limit).
  4. generations_used < monthly_generations → quota_exhausted.

429 канонизирован как 402 reason=concurrency_limit (единый payment-gate, docs §4).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_user
from app.api.errors import payment_required
from app.billing import entitlements, usage
from app.core.logging import get_logger
from app.db.models import User
from app.db.session import get_session
from app.observability import metrics

logger = get_logger(__name__)

# Минимальный access_level, снимающий ограничение (iOS показывает Adapty-пейвол).
_REQUIRED_ENTITLEMENT = "pro"


async def enforce_quota_gate(
    session: AsyncSession,
    user_id: str,
    *,
    check_project_limit: bool,
    kind: str = "generation",
) -> None:
    """Энфорс quota-gate. Нарушение → ProblemException(402). check_project_limit — для /projects.

    Параметр kind (ADR-014 §A / docs/billing §7) выбирает ось бизнес-квоты:
      - kind='generation': monthly_generations vs usage_counters → quota_exhausted;
      - kind='edit': monthly_edits vs edit_usage_counters → edit_quota_exhausted
        (НЕ из квоты генераций; max_projects на /edits не проверяется — проект существует).
    Общие оси (обе): access_level активен + max_concurrent_jobs (edit-джоба = активная).

    Источник access_level/status — subscriptions (lazy-ресинк при протухании). Лимиты —
    plan_quotas по реальному access_level. Все проверки до постановки задачи.
    """
    ent = await entitlements.resolve_entitlement(session, user_id)

    # 1. Активный entitlement (status ∈ {active, grace}). billing_issue/expired → отказ.
    if not ent.gate_passes:
        metrics.quota_rejected_total.labels(reason="no_entitlement").inc()
        raise payment_required(
            "No active subscription for this access level.",
            reason="no_entitlement",
            required_entitlement=_REQUIRED_ENTITLEMENT,
        )

    quota = await entitlements.get_plan_quota(session, ent.access_level)

    # 2. max_projects (только POST /projects; NULL = безлимит Pro). На /edits не проверяется.
    if check_project_limit and quota is not None and quota.max_projects is not None:
        projects_used = await entitlements.count_projects(session, user_id)
        if projects_used >= quota.max_projects:
            metrics.quota_rejected_total.labels(reason="project_limit").inc()
            raise payment_required(
                f"Project limit reached ({projects_used}/{quota.max_projects} on "
                f"{ent.access_level} plan).",
                reason="project_limit",
                required_entitlement=_REQUIRED_ENTITLEMENT,
            )

    # 3. max_concurrent_jobs (канонизировано как 402 reason=concurrency_limit, docs §4).
    #    edit-джоба считается активной джобой (ADR-014 §A).
    max_concurrent = await entitlements.resolve_max_concurrent_jobs(session, user_id)
    active_jobs = await entitlements.count_active_jobs(session, user_id)
    if active_jobs >= max_concurrent:
        metrics.quota_rejected_total.labels(reason="concurrency_limit").inc()
        # TD-012 (observability §2.7): разбивка «какой kind заблокирован каким kind».
        # holder_kind — kind активных джоб, занимающих слот (generation/edit/rollback).
        holder_kinds = await entitlements.active_job_kinds(session, user_id)
        for holder_kind in set(holder_kinds):
            metrics.concurrency_block_by_kind_total.labels(
                blocked_kind=kind, holder_kind=holder_kind
            ).inc()
        raise payment_required(
            f"Concurrent jobs limit reached ({active_jobs}/{max_concurrent} on "
            f"{ent.access_level} plan).",
            reason="concurrency_limit",
            required_entitlement=_REQUIRED_ENTITLEMENT,
        )

    # 4. Бизнес-квота: generations (kind='generation') или edits (kind='edit', ADR-014).
    if quota is None:
        return
    if kind == "edit":
        # monthly_edits NULL = безлимит (Pro) → не гейтим.
        if quota.monthly_edits is not None:
            used = await usage.get_edit_usage(session, user_id)
            if used >= quota.monthly_edits:
                metrics.quota_rejected_total.labels(reason="edit_quota_exhausted").inc()
                raise payment_required(
                    f"Monthly edit quota exhausted ({used}/{quota.monthly_edits} "
                    f"used on {ent.access_level} plan).",
                    reason="edit_quota_exhausted",
                    required_entitlement=_REQUIRED_ENTITLEMENT,
                )
    else:
        used = await usage.get_usage(session, user_id)
        if used >= quota.monthly_generations:
            metrics.quota_rejected_total.labels(reason="quota_exhausted").inc()
            raise payment_required(
                f"Monthly generation quota exhausted ({used}/{quota.monthly_generations} "
                f"used on {ent.access_level} plan).",
                reason="quota_exhausted",
                required_entitlement=_REQUIRED_ENTITLEMENT,
            )


async def quota_gate_projects(
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    """FastAPI-dependency для POST /v1/projects: энфорс gate + max_projects."""
    await enforce_quota_gate(session, user.id, check_project_limit=True)


async def quota_gate_edits(
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    """FastAPI-dependency для POST /v1/projects/{pid}/edits (контракт; роут — S5).

    Активен с S5 (ADR-014). max_projects не проверяется (правка не создаёт проект); ось
    бизнес-квоты — monthly_edits (kind='edit') → edit_quota_exhausted.
    """
    await enforce_quota_gate(session, user.id, check_project_limit=False, kind="edit")
