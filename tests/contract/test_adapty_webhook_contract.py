"""Contract: фикстуры payload вебхука Adapty v2 → апдейт subscriptions/billing_events.

docs/06-testing-strategy §Contract (Adapty webhook), docs/billing/02 §1. Реалистичные
payload'ы событий Adapty v2 (стабильный минимум контракта) проходят через process_webhook
и дают корректное состояние subscriptions + ledger billing_events. Подпись проверяется
отдельно (test_billing_webhook_signature); здесь — семантика payload→state.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.billing.webhook_handler import WebhookOutcome, process_webhook
from app.db.models import BillingEvent, Subscription, User

pytestmark = pytest.mark.asyncio


async def _user(session, uid: str) -> User:  # noqa: ANN001
    user = User(
        id=uid,
        adapty_customer_user_id=uid,
        api_key_hash=None,
        monthly_budget_usd=Decimal("50.0000"),
        status="active",
    )
    session.add(user)
    await session.flush()
    return user


# Реалистичные Adapty webhook v2 payload-фикстуры (стабильный контрактный минимум).
def _fixture(event_id, event_type, uid, *, access_level="pro", is_active=True, **sub):  # noqa: ANN001, ANN003
    return {
        "event_id": event_id,
        "event_type": event_type,
        "customer_user_id": uid,
        "profile": {"access_level": access_level, "is_active": is_active},
        "subscription": {
            "product_id": sub.get("product_id", "lovable.pro.monthly"),
            "store": sub.get("store", "app_store"),
            "expires_at": sub.get("expires_at", "2026-07-02T00:00:00Z"),
            "will_renew": sub.get("will_renew", True),
            "transaction_id": sub.get("transaction_id", "1000000123456789"),
            "started_at": sub.get("started_at", "2026-06-02T00:00:00Z"),
        },
    }


async def test_subscription_started_fixture(session):
    user = await _user(session, "u_ct_started00000001")
    payload = _fixture("evt_ct_start", "subscription_started", user.id)
    result = await process_webhook(session, payload)
    assert result.outcome == WebhookOutcome.PROCESSED

    sub = (
        await session.execute(select(Subscription).where(Subscription.user_id == user.id))
    ).scalar_one()
    assert sub.status == "active"
    assert sub.access_level == "pro"
    assert sub.product_id == "lovable.pro.monthly"
    assert sub.store == "app_store"
    assert sub.adapty_transaction_id == "1000000123456789"
    assert sub.grace_until is None

    ev = (
        await session.execute(
            select(BillingEvent).where(BillingEvent.adapty_event_id == "evt_ct_start")
        )
    ).scalar_one()
    assert ev.user_id == user.id
    assert ev.event_type == "subscription_started"
    assert ev.processed_at is not None
    # Сырой payload сохранён в ledger.
    assert ev.payload["subscription"]["product_id"] == "lovable.pro.monthly"


async def test_subscription_renewed_fixture(session):
    user = await _user(session, "u_ct_renew0000000001")
    await process_webhook(session, _fixture("evt_ct_r0", "subscription_started", user.id))
    result = await process_webhook(session, _fixture("evt_ct_r1", "subscription_renewed", user.id))
    assert result.outcome == WebhookOutcome.PROCESSED
    sub = (
        await session.execute(select(Subscription).where(Subscription.user_id == user.id))
    ).scalar_one()
    assert sub.status == "active"
    assert sub.grace_until is None


async def test_subscription_expired_fixture_sets_grace(session):
    user = await _user(session, "u_ct_exp00000000001")
    await process_webhook(session, _fixture("evt_ct_e0", "subscription_started", user.id))
    payload = _fixture(
        "evt_ct_e1",
        "subscription_expired",
        user.id,
        is_active=False,
        expires_at="2026-06-10T00:00:00Z",
        will_renew=False,
    )
    await process_webhook(session, payload)
    sub = (
        await session.execute(select(Subscription).where(Subscription.user_id == user.id))
    ).scalar_one()
    assert sub.status == "grace"
    assert sub.grace_until is not None
    assert sub.will_renew is False


async def test_subscription_refunded_fixture_sets_grace(session):
    user = await _user(session, "u_ct_ref00000000001")
    await process_webhook(session, _fixture("evt_ct_rf0", "subscription_started", user.id))
    payload = _fixture("evt_ct_rf1", "subscription_refunded", user.id, is_active=False)
    await process_webhook(session, payload)
    sub = (
        await session.execute(select(Subscription).where(Subscription.user_id == user.id))
    ).scalar_one()
    assert sub.status == "grace"
    assert sub.grace_until is not None


async def test_billing_issue_fixture(session):
    user = await _user(session, "u_ct_bi000000000001")
    await process_webhook(session, _fixture("evt_ct_bi0", "subscription_started", user.id))
    payload = _fixture("evt_ct_bi1", "billing_issue_detected", user.id, is_active=False)
    await process_webhook(session, payload)
    sub = (
        await session.execute(select(Subscription).where(Subscription.user_id == user.id))
    ).scalar_one()
    assert sub.status == "billing_issue"


async def test_duplicate_fixture_is_idempotent(session):
    user = await _user(session, "u_ct_dup00000000001")
    payload = _fixture("evt_ct_dup", "subscription_started", user.id)
    r1 = await process_webhook(session, payload)
    r2 = await process_webhook(session, payload)
    assert r1.outcome == WebhookOutcome.PROCESSED
    assert r2.outcome == WebhookOutcome.DUPLICATE
