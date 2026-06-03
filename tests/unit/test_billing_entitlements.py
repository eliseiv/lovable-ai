"""Unit: resolve_access_level / resolve_max_concurrent_jobs (docs/billing/03 §4).

Нет строки subscriptions → free/active (free всегда проходит гейт). Реальная pro-строка →
pro + лимит из plan_quotas (pro=3). gate_passes для active/grace, НЕ для billing_issue/
expired. plan_quotas сидируется миграцией 0004 (free=3/1/1, pro=100/null/3).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.billing import entitlements, subscription_state
from app.core.ids import new_subscription_id
from app.db.models import Subscription, User

pytestmark = pytest.mark.asyncio


async def _user(session, uid: str) -> User:  # noqa: ANN001
    user = User(id=uid, api_key_hash=None, monthly_budget_usd=Decimal("50.0000"), status="active")
    session.add(user)
    await session.flush()
    return user


async def _sub(session, uid: str, *, access_level: str, status: str) -> Subscription:  # noqa: ANN001
    from datetime import UTC, datetime

    sub = Subscription(
        id=new_subscription_id(),
        user_id=uid,
        access_level=access_level,
        status=status,
        will_renew=True,
        raw={},
        synced_at=datetime.now(UTC),
    )
    session.add(sub)
    await session.flush()
    return sub


async def test_no_subscription_defaults_to_free_active(session):
    user = await _user(session, "u_ent_nofree00000000001")
    ent = await entitlements.resolve_entitlement(session, user.id)
    assert ent.access_level == "free"
    assert ent.status == subscription_state.STATUS_ACTIVE
    assert ent.gate_passes is True


async def test_free_max_concurrent_is_one(session):
    user = await _user(session, "u_ent_freeconc00000001")
    assert await entitlements.resolve_max_concurrent_jobs(session, user.id) == 1


async def test_real_pro_access_level_and_concurrency(session):
    user = await _user(session, "u_ent_pro00000000000001")
    await _sub(session, user.id, access_level="pro", status="active")
    assert await entitlements.resolve_access_level(session, user.id) == "pro"
    # plan_quotas.max_concurrent_jobs для pro = 3 (сидинг 0004).
    assert await entitlements.resolve_max_concurrent_jobs(session, user.id) == 3


async def test_grace_passes_gate(session):
    user = await _user(session, "u_ent_grace0000000001")
    await _sub(session, user.id, access_level="pro", status="grace")
    ent = await entitlements.resolve_entitlement(session, user.id)
    assert ent.gate_passes is True


@pytest.mark.parametrize("status", ["billing_issue", "expired"])
async def test_billing_issue_and_expired_do_not_pass_gate(session, status):
    user = await _user(session, f"u_ent_{status[:6]}00000001")
    await _sub(session, user.id, access_level="pro", status=status)
    ent = await entitlements.resolve_entitlement(session, user.id)
    assert ent.gate_passes is False


async def test_plan_quota_seeded_values(session):
    free = await entitlements.get_plan_quota(session, "free")
    pro = await entitlements.get_plan_quota(session, "pro")
    assert free is not None and pro is not None
    assert (free.monthly_generations, free.max_concurrent_jobs, free.max_projects) == (3, 1, 1)
    assert pro.monthly_generations == 100
    assert pro.max_projects is None  # безлимит проектов (Pro)
    assert pro.max_concurrent_jobs == 3
