"""Contract: фикстуры payload вебхука Adapty v2 → апдейт subscriptions/billing_events/credit_grants.

docs/06-testing-strategy §Contract (Adapty webhook, ADR-027), docs/billing/02 §1. Реалистичные
payload'ы событий Adapty v2 (стабильный минимум контракта) проходят через process_webhook
и дают корректное состояние subscriptions + ledger billing_events + token-grant (credit_grants).
Bearer-авторизация проверяется отдельно (integration test_billing_webhook); здесь — семантика
payload→state. WebhookOutcome.APPLIED — переименован с PROCESSED в ревизии ADR-027.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.billing.webhook_handler import WebhookOutcome, process_webhook
from app.core.config import get_settings
from app.db.models import BillingEvent, CreditGrant, Subscription, User

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
        "event_properties": {
            "vendor_product_id": sub.get("vendor_product_id", "lovable.pro.weekly"),
        },
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
    assert result.outcome == WebhookOutcome.APPLIED

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


async def test_subscription_started_grants_tokens(session):
    """Валидный started → credit_grants(created_by='adapty', idempotency_key=event_id) + баланс."""
    user = await _user(session, "u_ct_grant0000000001")
    settings = get_settings()
    payload = _fixture(
        "evt_ct_grant",
        "subscription_started",
        user.id,
        vendor_product_id=settings.subscription_product_weekly,
    )
    await process_webhook(session, payload)

    balance = await session.scalar(select(User.bonus_generations_balance).where(User.id == user.id))
    assert balance == settings.subscription_tokens_weekly
    grant = (
        await session.execute(
            select(CreditGrant).where(CreditGrant.idempotency_key == "evt_ct_grant")
        )
    ).scalar_one()
    assert grant.created_by == "adapty"
    assert grant.amount == settings.subscription_tokens_weekly


async def test_subscription_renewed_fixture(session):
    user = await _user(session, "u_ct_renew0000000001")
    await process_webhook(session, _fixture("evt_ct_r0", "subscription_started", user.id))
    result = await process_webhook(session, _fixture("evt_ct_r1", "subscription_renewed", user.id))
    assert result.outcome == WebhookOutcome.APPLIED
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


async def test_cancelled_fixture_will_renew_false_no_token_grant(session):
    """cancelled (ADR-027 §F): will_renew=false; status/access не трогаем; токены не начисляем."""
    user = await _user(session, "u_ct_cancel000000001")
    await process_webhook(session, _fixture("evt_ct_cn0", "subscription_started", user.id))
    balance_before = await session.scalar(
        select(User.bonus_generations_balance).where(User.id == user.id)
    )
    result = await process_webhook(
        session, _fixture("evt_ct_cn1", "subscription_cancelled", user.id)
    )
    assert result.outcome == WebhookOutcome.APPLIED
    sub = (
        await session.execute(select(Subscription).where(Subscription.user_id == user.id))
    ).scalar_one()
    assert sub.will_renew is False
    assert sub.status == "active"  # не трогаем
    balance_after = await session.scalar(
        select(User.bonus_generations_balance).where(User.id == user.id)
    )
    assert balance_after == balance_before  # токены не начислены


async def test_duplicate_fixture_is_idempotent(session):
    user = await _user(session, "u_ct_dup00000000001")
    payload = _fixture("evt_ct_dup", "subscription_started", user.id)
    r1 = await process_webhook(session, payload)
    r2 = await process_webhook(session, payload)
    assert r1.outcome == WebhookOutcome.APPLIED
    assert r2.outcome == WebhookOutcome.DUPLICATE


async def test_response_schema_fields(session):
    """Response-схема: outcome → {status, reason?, event_type?}. ignored несёт reason/event_type."""
    # missing_event_id → reason.
    r = await process_webhook(session, {"event_type": "subscription_started"})
    assert r.outcome == WebhookOutcome.IGNORED
    assert r.reason == "missing_event_id"
    # unknown event_type → event_type (не reason).
    r2 = await process_webhook(session, {"event_id": "evt_ct_unk", "event_type": "totally_unknown"})
    assert r2.outcome == WebhookOutcome.IGNORED
    assert r2.event_type == "totally_unknown"
    # not-an-object → reason.
    r3 = await process_webhook(session, [1, 2, 3])
    assert r3.outcome == WebhookOutcome.IGNORED
    assert r3.reason == "not_an_object"
