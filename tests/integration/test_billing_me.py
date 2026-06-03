"""Integration: GET /v1/billing/me (docs/billing/02 §2).

access_level + status + остаток квоты; free-дефолт без подписки; max_projects=null для Pro;
generations_remaining = max(0, monthly - used). Реальный Postgres; auth — legacy-ключ.
Adapty getProfile не вызывается (synced_at свежий).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.core.ids import new_subscription_id
from app.core.security import hash_api_key
from app.db.models import Subscription, UsageCounter, User

pytestmark = pytest.mark.asyncio


async def _user(session, uid: str, key: str) -> User:  # noqa: ANN001
    user = User(
        id=uid,
        api_key_hash=hash_api_key(key),
        monthly_budget_usd=Decimal("50.0000"),
        status="active",
    )
    session.add(user)
    await session.flush()
    return user


def _hdr(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def test_free_default_no_subscription(client, session):
    await _user(session, "u_me_free00000000001", "me-free-key")
    resp = await client.get("/v1/billing/me", headers=_hdr("me-free-key"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["access_level"] == "free"
    assert body["status"] == "active"
    q = body["quota"]
    assert q["monthly_generations"] == 3
    assert q["generations_used"] == 0
    assert q["generations_remaining"] == 3
    assert q["max_projects"] == 1
    assert q["max_concurrent_jobs"] == 1


async def test_pro_subscription_max_projects_null(client, session):
    uid = "u_me_pro000000000001"
    await _user(session, uid, "me-pro-key")
    session.add(
        Subscription(
            id=new_subscription_id(),
            user_id=uid,
            access_level="pro",
            status="active",
            will_renew=True,
            raw={},
            synced_at=datetime.now(UTC),
        )
    )
    await session.flush()
    resp = await client.get("/v1/billing/me", headers=_hdr("me-pro-key"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["access_level"] == "pro"
    q = body["quota"]
    assert q["monthly_generations"] == 100
    assert q["max_projects"] is None  # безлимит Pro
    assert q["max_concurrent_jobs"] == 3


async def test_generations_remaining_reflects_usage(client, session):
    uid = "u_me_usage00000000001"
    await _user(session, uid, "me-usage-key")
    period = datetime.now(UTC).strftime("%Y-%m")
    session.add(UsageCounter(user_id=uid, period=period, generations_used=2))
    await session.flush()
    resp = await client.get("/v1/billing/me", headers=_hdr("me-usage-key"))
    body = resp.json()
    assert body["quota"]["generations_used"] == 2
    assert body["quota"]["generations_remaining"] == 1  # 3 - 2


async def test_generations_remaining_clamped_to_zero(client, session):
    uid = "u_me_over000000000001"
    await _user(session, uid, "me-over-key")
    period = datetime.now(UTC).strftime("%Y-%m")
    # used > monthly → remaining = max(0, ...) = 0 (не отрицательное).
    session.add(UsageCounter(user_id=uid, period=period, generations_used=5))
    await session.flush()
    resp = await client.get("/v1/billing/me", headers=_hdr("me-over-key"))
    assert resp.json()["quota"]["generations_remaining"] == 0


async def test_grace_status_reported(client, session):
    uid = "u_me_grace000000001"
    await _user(session, uid, "me-grace-key")
    session.add(
        Subscription(
            id=new_subscription_id(),
            user_id=uid,
            access_level="pro",
            status="grace",
            will_renew=False,
            raw={},
            synced_at=datetime.now(UTC),
        )
    )
    await session.flush()
    resp = await client.get("/v1/billing/me", headers=_hdr("me-grace-key"))
    assert resp.json()["status"] == "grace"


async def test_billing_me_requires_auth(client):
    resp = await client.get("/v1/billing/me")
    assert resp.status_code == 401
