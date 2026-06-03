"""Integration: GET /v1/billing/me — поля edits (Sprint 5, ADR-014, docs/billing §7).

Реальный Postgres (client шарит тест-сессию). Покрывает:
  - Free: monthly_edits=5, edits_used отражает edit_usage_counters,
    edits_remaining = max(0, 5 - used);
  - Pro: monthly_edits=NULL → edits_remaining = None (безлимит);
  - edits отдельны от generations (generations_used/remaining не смешиваются с edits).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.core.ids import new_subscription_id
from app.core.security import hash_api_key
from app.db.models import EditUsageCounter, Subscription, UsageCounter, User

pytestmark = pytest.mark.asyncio


async def _user(session, uid, key) -> None:  # noqa: ANN001
    session.add(
        User(
            id=uid,
            api_key_hash=hash_api_key(key),
            monthly_budget_usd=Decimal("50.0000"),
            status="active",
        )
    )
    await session.flush()


def _hdr(key):  # noqa: ANN001
    return {"Authorization": f"Bearer {key}"}


async def test_billing_me_free_edits_used_and_remaining(client, session):
    uid = "u_me_free00000000001"
    await _user(session, uid, "me-free-key")
    period = datetime.now(UTC).strftime("%Y-%m")
    session.add(EditUsageCounter(user_id=uid, period=period, edits_used=2))
    await session.flush()

    resp = await client.get("/v1/billing/me", headers=_hdr("me-free-key"))
    assert resp.status_code == 200
    quota = resp.json()["quota"]
    assert quota["monthly_edits"] == 5
    assert quota["edits_used"] == 2
    assert quota["edits_remaining"] == 3


async def test_billing_me_free_edits_remaining_floored_at_zero(client, session):
    uid = "u_me_floor0000000001"
    await _user(session, uid, "me-floor-key")
    period = datetime.now(UTC).strftime("%Y-%m")
    session.add(EditUsageCounter(user_id=uid, period=period, edits_used=7))  # >5
    await session.flush()

    resp = await client.get("/v1/billing/me", headers=_hdr("me-floor-key"))
    quota = resp.json()["quota"]
    assert quota["edits_remaining"] == 0


async def test_billing_me_pro_edits_remaining_none(client, session):
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
    period = datetime.now(UTC).strftime("%Y-%m")
    session.add(EditUsageCounter(user_id=uid, period=period, edits_used=42))
    await session.flush()

    resp = await client.get("/v1/billing/me", headers=_hdr("me-pro-key"))
    quota = resp.json()["quota"]
    # Pro: monthly_edits=NULL → безлимит → edits_remaining None.
    assert quota["monthly_edits"] is None
    assert quota["edits_remaining"] is None
    assert quota["edits_used"] == 42


async def test_billing_me_edits_separate_from_generations(client, session):
    uid = "u_me_sep000000000001"
    await _user(session, uid, "me-sep-key")
    period = datetime.now(UTC).strftime("%Y-%m")
    session.add(UsageCounter(user_id=uid, period=period, generations_used=1))
    session.add(EditUsageCounter(user_id=uid, period=period, edits_used=3))
    await session.flush()

    resp = await client.get("/v1/billing/me", headers=_hdr("me-sep-key"))
    quota = resp.json()["quota"]
    assert quota["generations_used"] == 1
    assert quota["edits_used"] == 3
    # Free: generations monthly=3, edits monthly=5 — независимы.
    assert quota["generations_remaining"] == 2
    assert quota["edits_remaining"] == 2
