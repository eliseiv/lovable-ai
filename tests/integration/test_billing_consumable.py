"""Integration: consumable token-паки Adapty (non_subscription_purchase, ADR-038 + ADR-040/041).

Payload — ОФИЦИАЛЬНОЙ формы (tests/support/adapty_payloads.py): profile_event_id/
vendor_product_id внутри event_properties, БЕЗ event_id/profile/subscription. Реальный Postgres
(client-фикстура шарит тест-сессию). Bearer-секрет = ADAPTY_WEBHOOK_SECRET из conftest.
TOKEN_PACK_PRODUCTS — через monkeypatch на cached Settings (фикстура token_packs).

Покрытие (docs/06 §Contract g/h + сценарий 17): начисление по event_properties.vendor_product_id;
неизвестный продукт → ignored:unknown_token_product, processed_at=NULL; не трогает subscriptions;
идемпотентность; подписки=0 short-circuit; missing customer_user_id; refunded вне scope.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.core.config import get_settings
from app.db.models import BillingEvent, CreditGrant, Subscription, User
from tests.support import adapty_payloads as ap

pytestmark = pytest.mark.asyncio

CANONICAL_CSV = (
    "100_tokens_9.99:100,250_tokens_19.99:250,500_tokens_34.99:500,"
    "1000_tokens_59.99:1000,2000_tokens_99.99:2000"
)


@pytest.fixture
def token_packs(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "token_pack_products", CANONICAL_CSV, raising=False)
    return settings


def _secret() -> str:
    return get_settings().adapty_webhook_secret.get_secret_value()


def _auth(token: str | None = None) -> dict[str, str]:
    tok = token if token is not None else _secret()
    return {"Authorization": f"Bearer {tok}"}


def _consumable(customer_user_id, vendor_product_id=ap._OMIT, *, profile_event_id=ap._AUTO):  # noqa: ANN001
    return ap.subscription_event(
        "non_subscription_purchase",
        customer_user_id,
        profile_event_id=profile_event_id,
        vendor_product_id=vendor_product_id,
    )


async def _post(client, payload, *, headers=None):  # noqa: ANN001
    body = payload if isinstance(payload, bytes) else ap.to_body(payload)
    hdrs = {"Content-Type": "application/json"}
    hdrs.update(headers if headers is not None else _auth())
    return await client.post("/v1/billing/webhook/adapty", content=body, headers=hdrs)


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


async def _balance(session, uid: str) -> int:  # noqa: ANN001
    return await session.scalar(select(User.bonus_generations_balance).where(User.id == uid))


# ================= (17) Consumable applied — начисление по vendor_product_id =================


async def test_consumable_known_pack_credits_exact_amount(client, session, token_packs):
    user = await _user(session, "u_cons_ok000000001")
    peid = ap.new_profile_event_id()
    payload = _consumable(user.id, "250_tokens_19.99", profile_event_id=peid)
    resp = await _post(client, payload)
    assert resp.json() == {"status": "applied"}
    assert await _balance(session, user.id) == 250

    grant = (
        await session.execute(select(CreditGrant).where(CreditGrant.idempotency_key == peid))
    ).scalar_one()
    assert grant.reason == "adapty:non_subscription_purchase"
    assert grant.created_by == "adapty"
    assert grant.amount == 250
    assert grant.user_id == user.id

    ev = (
        await session.execute(select(BillingEvent).where(BillingEvent.adapty_event_id == peid))
    ).scalar_one()
    assert ev.event_type == "non_subscription_purchase"
    assert ev.processed_at is not None


async def test_consumable_largest_pack_credits(client, session, token_packs):
    user = await _user(session, "u_cons_big00000001")
    resp = await _post(client, _consumable(user.id, "2000_tokens_99.99"))
    assert resp.json() == {"status": "applied"}
    assert await _balance(session, user.id) == 2000


# ================= (17) Неизвестный продукт → ignored, processed_at=NULL =================


async def test_consumable_unknown_product_ignored_not_credited(client, session, token_packs):
    user = await _user(session, "u_cons_unk00000001")
    peid = ap.new_profile_event_id()
    resp = await _post(client, _consumable(user.id, "9999_not_a_pack", profile_event_id=peid))
    assert resp.json() == {"status": "ignored", "reason": "unknown_token_product"}
    assert await _balance(session, user.id) == 0
    grant_count = await session.scalar(
        select(func.count()).select_from(CreditGrant).where(CreditGrant.idempotency_key == peid)
    )
    assert grant_count == 0
    ev = (
        await session.execute(select(BillingEvent).where(BillingEvent.adapty_event_id == peid))
    ).scalar_one()
    assert ev.processed_at is None  # не теряем, ручная реобработка


async def test_consumable_missing_vendor_product_id_unknown(client, session, token_packs):
    user = await _user(session, "u_cons_novp0000001")
    resp = await _post(client, _consumable(user.id))  # vpid отсутствует
    assert resp.json() == {"status": "ignored", "reason": "unknown_token_product"}
    assert await _balance(session, user.id) == 0


async def test_consumable_unknown_when_pack_env_empty(client, session, monkeypatch):
    monkeypatch.setattr(get_settings(), "token_pack_products", "", raising=False)
    user = await _user(session, "u_cons_empty000001")
    resp = await _post(client, _consumable(user.id, "100_tokens_9.99"))
    assert resp.json() == {"status": "ignored", "reason": "unknown_token_product"}
    assert await _balance(session, user.id) == 0


# ================= Не трогает subscriptions =================


async def test_consumable_does_not_create_subscription(client, session, token_packs):
    user = await _user(session, "u_cons_nosub000001")
    resp = await _post(client, _consumable(user.id, "100_tokens_9.99"))
    assert resp.json() == {"status": "applied"}
    sub_count = await session.scalar(
        select(func.count()).select_from(Subscription).where(Subscription.user_id == user.id)
    )
    assert sub_count == 0
    assert await _balance(session, user.id) == 100


# ================= Идемпотентность =================


async def test_consumable_duplicate_no_double_credit(client, session, token_packs):
    user = await _user(session, "u_cons_dup00000001")
    peid = ap.new_profile_event_id()
    payload = _consumable(user.id, "1000_tokens_59.99", profile_event_id=peid)
    r1 = await _post(client, payload)
    assert r1.json() == {"status": "applied"}
    assert await _balance(session, user.id) == 1000

    r2 = await _post(client, payload)
    assert r2.json() == {"status": "duplicate"}
    assert await _balance(session, user.id) == 1000
    grant_count = await session.scalar(
        select(func.count()).select_from(CreditGrant).where(CreditGrant.idempotency_key == peid)
    )
    assert grant_count == 1


# ================= Подписки = 0 бонус-токенов (short-circuit) =================


@pytest.mark.parametrize("event_type", ["subscription_started", "subscription_renewed"])
async def test_subscription_zero_tokens_short_circuit(client, session, event_type):
    settings = get_settings()
    assert settings.subscription_tokens_weekly == 0
    assert settings.subscription_tokens_grant == 0
    uid = "u_sub0_st000000001" if event_type == "subscription_started" else "u_sub0_rn000000001"
    user = await _user(session, uid)
    peid = ap.new_profile_event_id()
    payload = ap.subscription_event(
        event_type,
        user.id,
        profile_event_id=peid,
        vendor_product_id=settings.subscription_product_weekly,
    )
    resp = await _post(client, payload)
    assert resp.json() == {"status": "applied"}
    sub = (
        await session.execute(select(Subscription).where(Subscription.user_id == user.id))
    ).scalar_one()
    assert sub.access_level == "pro"
    assert sub.status == "active"
    assert await _balance(session, user.id) == 0
    grant_count = await session.scalar(
        select(func.count()).select_from(CreditGrant).where(CreditGrant.idempotency_key == peid)
    )
    assert grant_count == 0


# ================= Пак amount=0 =================


async def test_consumable_zero_amount_pack_applied_no_grant(client, session, monkeypatch):
    monkeypatch.setattr(get_settings(), "token_pack_products", "free_promo_pack:0", raising=False)
    user = await _user(session, "u_cons_zero000001")
    peid = ap.new_profile_event_id()
    resp = await _post(client, _consumable(user.id, "free_promo_pack", profile_event_id=peid))
    assert resp.json() == {"status": "applied"}
    assert await _balance(session, user.id) == 0
    grant_count = await session.scalar(
        select(func.count()).select_from(CreditGrant).where(CreditGrant.idempotency_key == peid)
    )
    assert grant_count == 0
    ev = (
        await session.execute(select(BillingEvent).where(BillingEvent.adapty_event_id == peid))
    ).scalar_one()
    assert ev.processed_at is not None


# ================= missing customer_user_id =================


async def test_consumable_missing_customer_user_id(client, session, token_packs):
    peid = ap.new_profile_event_id()
    payload = ap.make_webhook_payload(
        event_type="non_subscription_purchase",
        omit_customer_user_id=True,
        event_properties=ap.make_event_properties(
            profile_event_id=peid, vendor_product_id="100_tokens_9.99"
        ),
    )
    resp = await _post(client, payload)
    assert resp.json() == {"status": "ignored", "reason": "missing_customer_user_id"}
    ev = (
        await session.execute(select(BillingEvent).where(BillingEvent.adapty_event_id == peid))
    ).scalar_one()
    assert ev.user_id is None
    assert ev.processed_at is None


# ================= refunded вне scope =================


async def test_non_subscription_purchase_refunded_out_of_scope(client, session, token_packs):
    """non_subscription_purchase_refunded ∈ CONSCIOUSLY_IGNORED → 200 ignored:type,
    billing_events(processed_at=NULL), токены не трогаются."""
    user = await _user(session, "u_cons_refund00001")
    peid = ap.new_profile_event_id()
    payload = ap.subscription_event(
        "non_subscription_purchase_refunded",
        user.id,
        profile_event_id=peid,
        vendor_product_id="100_tokens_9.99",
    )
    resp = await _post(client, payload)
    assert resp.json() == {
        "status": "ignored",
        "event_type": "non_subscription_purchase_refunded",
    }
    assert await _balance(session, user.id) == 0
    # CONSCIOUSLY_IGNORED персистится (processed_at=NULL), в отличие от unknown-типа.
    ev = (
        await session.execute(select(BillingEvent).where(BillingEvent.adapty_event_id == peid))
    ).scalar_one()
    assert ev.processed_at is None
