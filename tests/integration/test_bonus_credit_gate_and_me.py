"""Integration: эффективный лимит генераций с кредитами на гейте + отражение в billing/me.

Источник истины — docs/modules/billing/03-architecture.md §4 (эффективный лимит =
monthly_generations + bonus_generations_balance) + §10.4 / 02-api-contracts §2 (billing/me:
bonus_generations_remaining = balance; generations_remaining учитывает кредиты).

free monthly_generations=3 (сидинг plan_quotas). Реальный Postgres; dispatch/publish мокаются.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.core.security import hash_api_key
from app.db.models import UsageCounter, User

pytestmark = pytest.mark.asyncio


def _period() -> str:
    return datetime.now(UTC).strftime("%Y-%m")


async def _user(session, uid: str, key: str, *, balance: int = 0) -> User:  # noqa: ANN001
    user = User(
        id=uid,
        api_key_hash=hash_api_key(key),
        monthly_budget_usd=Decimal("50.0000"),
        status="active",
        bonus_generations_balance=balance,
    )
    session.add(user)
    await session.flush()
    return user


# ============================ quota-gate: кредиты расширяют лимит ============================


async def test_gate_passes_when_plan_exhausted_but_credits_available(
    client, session, no_side_effects
):
    """План исчерпан (used=3=monthly), но balance>0 → POST /projects проходит (202)."""
    key = "bonus-gate-pass-key"
    uid = "u_bonus_gatepass001"
    await _user(session, uid, key, balance=2)
    session.add(UsageCounter(user_id=uid, period=_period(), generations_used=3))
    await session.flush()

    resp = await client.post(
        "/v1/projects",
        json={"prompt": "site"},
        headers={"Authorization": f"Bearer {key}", "Idempotency-Key": "bg-1"},
    )
    assert resp.status_code == 202


async def test_gate_402_when_plan_exhausted_and_no_credits(client, session, no_side_effects):
    """План исчерпан и balance==0 → 402 reason=quota_exhausted."""
    key = "bonus-gate-deny-key"
    uid = "u_bonus_gatedeny001"
    await _user(session, uid, key, balance=0)
    session.add(UsageCounter(user_id=uid, period=_period(), generations_used=3))
    await session.flush()

    resp = await client.post(
        "/v1/projects",
        json={"prompt": "site"},
        headers={"Authorization": f"Bearer {key}", "Idempotency-Key": "bg-2"},
    )
    assert resp.status_code == 402
    assert resp.json()["reason"] == "quota_exhausted"


# ============================ billing/me: отражение кредитов ============================


async def test_billing_me_reports_bonus_remaining(client, session):
    """bonus_generations_remaining = balance; generations_remaining учитывает кредиты."""
    key = "bonus-me-key"
    uid = "u_bonus_me00000001"
    await _user(session, uid, key, balance=25)
    # План исчерпан (used=3) → плановый остаток 0; +25 кредитов.
    session.add(UsageCounter(user_id=uid, period=_period(), generations_used=3))
    await session.flush()

    resp = await client.get("/v1/billing/me", headers={"Authorization": f"Bearer {key}"})
    assert resp.status_code == 200
    q = resp.json()["quota"]
    assert q["bonus_generations_remaining"] == 25
    assert q["generations_used"] == 3
    assert q["monthly_generations"] == 3
    # max(0, 3-3) + 25 = 25.
    assert q["generations_remaining"] == 25


async def test_billing_me_bonus_adds_to_plan_remaining(client, session):
    """Плановый остаток есть (used=1) + кредиты → generations_remaining = (3-1)+10 = 12."""
    key = "bonus-me-add-key"
    uid = "u_bonus_meadd00001"
    await _user(session, uid, key, balance=10)
    session.add(UsageCounter(user_id=uid, period=_period(), generations_used=1))
    await session.flush()

    resp = await client.get("/v1/billing/me", headers={"Authorization": f"Bearer {key}"})
    assert resp.status_code == 200
    q = resp.json()["quota"]
    assert q["bonus_generations_remaining"] == 10
    assert q["generations_remaining"] == 12


async def test_billing_me_zero_bonus_default(client, session):
    """Без кредитов bonus_generations_remaining = 0 (дефолт баланса)."""
    key = "bonus-me-zero-key"
    uid = "u_bonus_mezero0001"
    await _user(session, uid, key, balance=0)
    resp = await client.get("/v1/billing/me", headers={"Authorization": f"Bearer {key}"})
    assert resp.status_code == 200
    assert resp.json()["quota"]["bonus_generations_remaining"] == 0
