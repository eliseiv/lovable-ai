"""Integration: обобщённый примитив grant_tokens (ADR-039 §C) — нулевая регрессия Adapty.

docs/06 §Contract (f): после параметризации created_by/reason/idempotency_key ДЕФОЛТЫ дают
БАЙТ-В-БАЙТ прежнее Adapty-поведение (created_by='adapty', reason='adapty:<event_type>',
idempotency_key=event_id); StoreKit-вызов даёт created_by='storekit',
reason='storekit:tokens_purchase', idempotency_key='storekit:'+tid. Реальный Postgres.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.billing import subscription_state
from app.db.models import CreditGrant, User

pytestmark = pytest.mark.asyncio


async def _user(session, uid: str) -> User:  # noqa: ANN001
    user = User(
        id=uid,
        api_key_hash=None,
        monthly_budget_usd=Decimal("50.0000"),
        status="active",
    )
    session.add(user)
    await session.flush()
    return user


async def test_grant_tokens_defaults_are_adapty_byte_for_byte(session):
    """Дефолтные аргументы (без created_by/reason/idempotency_key) → Adapty-семантика."""
    user = await _user(session, "u_gt_adapty000000001")
    granted = await subscription_state.grant_tokens(
        session,
        user_id=user.id,
        event_id="evt_adapty_1",
        event_type="non_subscription_purchase",
        amount=100,
    )
    assert granted == 100
    await session.flush()  # autoflush=False в тест-сессии → материализуем pending insert
    grant = (
        await session.execute(
            select(CreditGrant).where(CreditGrant.idempotency_key == "evt_adapty_1")
        )
    ).scalar_one()
    assert grant.created_by == "adapty"
    assert grant.reason == "adapty:non_subscription_purchase"
    assert grant.amount == 100
    bal = await session.scalar(select(User.bonus_generations_balance).where(User.id == user.id))
    assert bal == 100


async def test_grant_tokens_storekit_args(session):
    """StoreKit-вызов → created_by='storekit', reason='storekit:tokens_purchase', tid-key."""
    user = await _user(session, "u_gt_storekit0000001")
    granted = await subscription_state.grant_tokens(
        session,
        user_id=user.id,
        event_id="tx_gt_sk",
        event_type="tokens_purchase",
        amount=250,
        created_by="storekit",
        reason="storekit:tokens_purchase",
        idempotency_key="storekit:tx_gt_sk",
    )
    assert granted == 250
    await session.flush()
    grant = (
        await session.execute(
            select(CreditGrant).where(CreditGrant.idempotency_key == "storekit:tx_gt_sk")
        )
    ).scalar_one()
    assert grant.created_by == "storekit"
    assert grant.reason == "storekit:tokens_purchase"


async def test_grant_tokens_zero_amount_short_circuit(session):
    """amount<=0 → грант НЕ пишется (ADR-038 §C short-circuit)."""
    user = await _user(session, "u_gt_zero00000000001")
    granted = await subscription_state.grant_tokens(
        session,
        user_id=user.id,
        event_id="evt_zero",
        event_type="subscription_started",
        amount=0,
    )
    assert granted == 0
    cnt = await session.scalar(select(CreditGrant).where(CreditGrant.idempotency_key == "evt_zero"))
    assert cnt is None
