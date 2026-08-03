"""Integration: CRM Admin API (broad-crm контракт v1, /v1/admin)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.core.config import get_settings
from app.core.security import hash_api_key
from app.db.models import BillingEvent, User

pytestmark = pytest.mark.asyncio


async def _user(session, uid: str, *, balance: int = 0) -> User:  # noqa: ANN001
    user = User(
        id=uid,
        api_key_hash=hash_api_key(f"{uid}-legacy-key"),
        adapty_customer_user_id=f"ext_{uid}",
        monthly_budget_usd=Decimal("50.0000"),
        status="active",
        bonus_generations_balance=balance,
    )
    session.add(user)
    await session.flush()
    return user


async def test_health_alias_200(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_crm_list_users_sorted_by_registered_at_desc(client, session, admin_headers):
    older = await _user(session, "u_crm_old00000001")
    older.created_at = datetime.now(UTC) - timedelta(days=2)
    newer = await _user(session, "u_crm_new00000001")
    newer.created_at = datetime.now(UTC) - timedelta(hours=1)
    await session.flush()

    resp = await client.get("/v1/admin/users", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 2
    ids = [item["id"] for item in body["items"]]
    assert ids.index(newer.id) < ids.index(older.id)


async def test_crm_get_user_card(client, session, admin_headers):
    user = await _user(session, "u_crm_card000001", balance=12)
    resp = await client.get(f"/v1/admin/users/{user.id}", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == user.id
    assert body["external_id"] == f"ext_{user.id}"
    assert body["balance"]["tokens"] == 12.0
    assert body["subscription"]["active"] is False


async def test_crm_adjust_tokens_non_idempotent(client, session, admin_headers):
    user = await _user(session, "u_crm_tok0000001", balance=5)
    for expected in (15.0, 25.0):
        resp = await client.post(
            f"/v1/admin/users/{user.id}/tokens",
            json={"amount": 10},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["tokens"] == expected


async def test_crm_adjust_tokens_negative_balance_400(client, session, admin_headers):
    user = await _user(session, "u_crm_neg0000001", balance=2)
    resp = await client.post(
        f"/v1/admin/users/{user.id}/tokens",
        json={"amount": -5},
        headers=admin_headers,
    )
    assert resp.status_code == 400


async def test_crm_grant_subscription_idempotent(client, session, admin_headers):
    user = await _user(session, "u_crm_sub0000001")
    settings = get_settings()
    body = {
        "product_id": settings.subscription_product_weekly,
        "expires_in_days": 30,
        "grant_id": "crm-grant-001",
    }
    first = await client.post(
        f"/v1/admin/users/{user.id}/subscription",
        json=body,
        headers=admin_headers,
    )
    assert first.status_code == 200
    assert first.json()["applied"] is True
    assert first.json()["subscription_active"] is True

    second = await client.post(
        f"/v1/admin/users/{user.id}/subscription",
        json=body,
        headers=admin_headers,
    )
    assert second.status_code == 200
    assert second.json()["applied"] is False


async def test_crm_payments_from_billing_events(client, session, admin_headers):
    user = await _user(session, "u_crm_pay0000001")
    session.add(
        BillingEvent(
            adapty_event_id="crm-test-event-001",
            event_type="subscription_started",
            user_id=user.id,
            payload={
                "event_properties": {
                    "vendor_product_id": "week_6.99_not_trial",
                    "price": 6.99,
                    "currency": "USD",
                    "store": "app_store",
                }
            },
            processed_at=datetime.now(UTC),
        )
    )
    await session.flush()

    resp = await client.get(
        f"/v1/admin/users/{user.id}/payments",
        headers=admin_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["status"] == "success"
    assert body["items"][0]["amount"] == 6.99


async def test_crm_products_lists_subscriptions(client, admin_headers):
    resp = await client.get("/v1/admin/products", headers=admin_headers)
    assert resp.status_code == 200
    product_ids = {item["product_id"] for item in resp.json()["items"]}
    settings = get_settings()
    assert settings.subscription_product_weekly in product_ids
    assert settings.subscription_product_yearly in product_ids


async def test_crm_stats(client, session, admin_headers):
    await _user(session, "u_crm_stats00001")
    resp = await client.get("/v1/admin/stats", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["users_total"] >= 1


async def test_crm_requests_empty_list(client, session, admin_headers):
    user = await _user(session, "u_crm_req0000001")
    resp = await client.get(
        f"/v1/admin/users/{user.id}/requests",
        headers=admin_headers,
    )
    assert resp.status_code == 200
    assert resp.json() == {"total": 0, "items": []}
