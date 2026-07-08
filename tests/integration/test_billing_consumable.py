"""Integration: consumable token-паки Adapty (non_subscription_purchase, ADR-038).

Реальный Postgres (client-фикстура шарит тест-сессию). Bearer-секрет = ADAPTY_WEBHOOK_SECRET
из тест-env (conftest). TOKEN_PACK_PRODUCTS проставляется на cached Settings через monkeypatch
(фикстура token_packs). Покрытие (docs/06 §Contract f/g/h/i, ADR-038 §A/C/D/E):

1. Consumable applied: known vendor_product_id → bonus_generations_balance += amount,
   credit_grants(reason='adapty:non_subscription_purchase', created_by='adapty',
   idempotency_key=event_id), billing_events.processed_at=now, 200 applied.
2. Unknown product → токены НЕ начислены, billing_events.processed_at=NULL,
   200 ignored:unknown_token_product.
3. Не трогает subscriptions: consumable-событие НЕ создаёт строку subscriptions.
4. Идемпотентность: повтор event_id → 200 duplicate, balance не растёт.
5. Подписки=0: subscription_started/renewed при SUBSCRIPTION_TOKENS_*=0 → subscriptions
   обновлён (access_level=pro), НО credit_grants НЕ пишется, balance не меняется, 200 applied.
7. Пак amount=0 → 200 applied без credit_grant.
8. missing customer_user_id → billing_events(user_id=NULL, processed_at=NULL),
   200 ignored:missing_customer_user_id.
9. non_subscription_purchase_refunded (вне scope) → 200 ignored:event_type (no-op).
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.core.config import get_settings
from app.db.models import BillingEvent, CreditGrant, Subscription, User

pytestmark = pytest.mark.asyncio

CANONICAL_CSV = (
    "100_tokens_9.99:100,250_tokens_19.99:250,500_tokens_34.99:500,"
    "1000_tokens_59.99:1000,2000_tokens_99.99:2000"
)


@pytest.fixture
def token_packs(monkeypatch):
    """Проставляет TOKEN_PACK_PRODUCTS на cached Settings (webhook_handler → get_settings())."""
    settings = get_settings()
    monkeypatch.setattr(settings, "token_pack_products", CANONICAL_CSV, raising=False)
    return settings


def _secret() -> str:
    return get_settings().adapty_webhook_secret.get_secret_value()


def _auth(token: str | None = None) -> dict[str, str]:
    tok = token if token is not None else _secret()
    return {"Authorization": f"Bearer {tok}"}


def _consumable_body(event_id, customer_user_id, vendor_product_id=None):  # noqa: ANN001
    payload = {
        "event_id": event_id,
        "event_type": "non_subscription_purchase",
        "customer_user_id": customer_user_id,
    }
    if vendor_product_id is not None:
        payload["event_properties"] = {"vendor_product_id": vendor_product_id}
    return json.dumps(payload).encode("utf-8")


async def _post(client, body: bytes, *, headers=None):  # noqa: ANN001
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


# ======================= (1) Consumable applied =======================


async def test_consumable_known_pack_credits_exact_amount(client, session, token_packs):
    user = await _user(session, "u_cons_ok00000000001")
    body = _consumable_body("evt_cons_ok", user.id, "250_tokens_19.99")
    resp = await _post(client, body)
    assert resp.status_code == 200
    assert resp.json() == {"status": "applied"}

    # bonus_generations_balance += 250 (ровно amount из TOKEN_PACK_PRODUCTS).
    assert await _balance(session, user.id) == 250

    # credit_grants: reason/created_by/idempotency_key/amount.
    grant = (
        await session.execute(
            select(CreditGrant).where(CreditGrant.idempotency_key == "evt_cons_ok")
        )
    ).scalar_one()
    assert grant.reason == "adapty:non_subscription_purchase"
    assert grant.created_by == "adapty"
    assert grant.amount == 250
    assert grant.user_id == user.id

    # billing_events.processed_at = now (обработано).
    ev = (
        await session.execute(
            select(BillingEvent).where(BillingEvent.adapty_event_id == "evt_cons_ok")
        )
    ).scalar_one()
    assert ev.user_id == user.id
    assert ev.event_type == "non_subscription_purchase"
    assert ev.processed_at is not None


async def test_consumable_largest_pack_credits(client, session, token_packs):
    user = await _user(session, "u_cons_big0000000001")
    resp = await _post(client, _consumable_body("evt_cons_big", user.id, "2000_tokens_99.99"))
    assert resp.json() == {"status": "applied"}
    assert await _balance(session, user.id) == 2000


# ======================= (2) Unknown product =======================


async def test_consumable_unknown_product_ignored_not_credited(client, session, token_packs):
    user = await _user(session, "u_cons_unk0000000001")
    resp = await _post(client, _consumable_body("evt_cons_unk", user.id, "9999_tokens_not_a_pack"))
    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored", "reason": "unknown_token_product"}

    # Токены НЕ начислены.
    assert await _balance(session, user.id) == 0
    grant_count = await session.scalar(
        select(func.count())
        .select_from(CreditGrant)
        .where(CreditGrant.idempotency_key == "evt_cons_unk")
    )
    assert grant_count == 0

    # billing_events сохранён с processed_at=NULL (не теряем, ручная реобработка).
    ev = (
        await session.execute(
            select(BillingEvent).where(BillingEvent.adapty_event_id == "evt_cons_unk")
        )
    ).scalar_one()
    assert ev.user_id == user.id
    assert ev.processed_at is None


async def test_consumable_missing_vendor_product_id_unknown(client, session, token_packs):
    """Отсутствующий vendor_product_id → resolve None → unknown_token_product (не 5xx)."""
    user = await _user(session, "u_cons_novp000000001")
    resp = await _post(client, _consumable_body("evt_cons_novp", user.id, vendor_product_id=None))
    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored", "reason": "unknown_token_product"}
    assert await _balance(session, user.id) == 0
    ev = (
        await session.execute(
            select(BillingEvent).where(BillingEvent.adapty_event_id == "evt_cons_novp")
        )
    ).scalar_one()
    assert ev.processed_at is None


async def test_consumable_unknown_when_pack_env_empty(client, session, monkeypatch):
    """Забытый TOKEN_PACK_PRODUCTS (пусто) → все паки unknown_token_product (оплата без токенов)."""
    settings = get_settings()
    monkeypatch.setattr(settings, "token_pack_products", "", raising=False)
    user = await _user(session, "u_cons_empty00000001")
    resp = await _post(client, _consumable_body("evt_cons_empty", user.id, "100_tokens_9.99"))
    assert resp.json() == {"status": "ignored", "reason": "unknown_token_product"}
    assert await _balance(session, user.id) == 0


# ======================= (3) Не трогает subscriptions =======================


async def test_consumable_does_not_create_subscription(client, session, token_packs):
    """Consumable НЕ создаёт/не мутирует subscriptions (apply_webhook_event не вызывается)."""
    user = await _user(session, "u_cons_nosub00000001")
    resp = await _post(client, _consumable_body("evt_cons_nosub", user.id, "100_tokens_9.99"))
    assert resp.json() == {"status": "applied"}
    # Строка подписки НЕ создана (consumable — не подписка).
    sub_count = await session.scalar(
        select(func.count()).select_from(Subscription).where(Subscription.user_id == user.id)
    )
    assert sub_count == 0
    # Но токены начислены.
    assert await _balance(session, user.id) == 100


async def test_consumable_does_not_mutate_existing_subscription(client, session, token_packs):
    """При существующей подписке consumable НЕ меняет её access_level/status."""
    user = await _user(session, "u_cons_keepsub000001")
    # Сначала подписка (started) — access_level=pro.
    started = _body_sub("evt_keepsub_start", "subscription_started", user.id)
    assert (await _post(client, started)).status_code == 200
    sub_before = (
        await session.execute(select(Subscription).where(Subscription.user_id == user.id))
    ).scalar_one()
    access_before, status_before = sub_before.access_level, sub_before.status

    # Consumable-покупка не должна трогать подписку.
    resp = await _post(client, _consumable_body("evt_keepsub_cons", user.id, "500_tokens_34.99"))
    assert resp.json() == {"status": "applied"}
    await session.refresh(sub_before)
    sub_after = (
        await session.execute(select(Subscription).where(Subscription.user_id == user.id))
    ).scalar_one()
    assert sub_after.access_level == access_before
    assert sub_after.status == status_before
    # Но токены за пак начислены.
    assert await _balance(session, user.id) == 500


def _body_sub(event_id, event_type, uid, **kw):  # noqa: ANN001, ANN003
    payload = {
        "event_id": event_id,
        "event_type": event_type,
        "customer_user_id": uid,
        "profile": {"access_level": "pro", "is_active": True},
        "subscription": kw.get("subscription", {}),
    }
    if "event_properties" in kw:
        payload["event_properties"] = kw["event_properties"]
    return json.dumps(payload).encode("utf-8")


# ======================= (4) Идемпотентность =======================


async def test_consumable_duplicate_event_id_no_double_credit(client, session, token_packs):
    user = await _user(session, "u_cons_dup0000000001")
    body = _consumable_body("evt_cons_dup", user.id, "1000_tokens_59.99")
    r1 = await _post(client, body)
    assert r1.json() == {"status": "applied"}
    assert await _balance(session, user.id) == 1000

    # Повтор того же event_id → 200 duplicate, balance/credit_grants НЕ растут.
    r2 = await _post(client, body)
    assert r2.status_code == 200
    assert r2.json() == {"status": "duplicate"}
    assert await _balance(session, user.id) == 1000

    ev_count = await session.scalar(
        select(func.count())
        .select_from(BillingEvent)
        .where(BillingEvent.adapty_event_id == "evt_cons_dup")
    )
    assert ev_count == 1
    grant_count = await session.scalar(
        select(func.count())
        .select_from(CreditGrant)
        .where(CreditGrant.idempotency_key == "evt_cons_dup")
    )
    assert grant_count == 1


# ======================= (5) Подписки = 0 бонус-токенов =======================


@pytest.mark.parametrize("event_type", ["subscription_started", "subscription_renewed"])
async def test_subscription_zero_tokens_short_circuit(client, session, event_type):
    """SUBSCRIPTION_TOKENS_*=0 → subscriptions обновлён, но credit_grants НЕ пишется (no-op)."""
    settings = get_settings()
    # Дефолты токенов подписок нормативно 0 (ADR-038 §D).
    assert settings.subscription_tokens_weekly == 0
    assert settings.subscription_tokens_yearly == 0
    assert settings.subscription_tokens_grant == 0

    uid = (
        "u_sub0_started0000001" if event_type == "subscription_started" else "u_sub0_renewed0000001"
    )
    user = await _user(session, uid)
    body = _body_sub(
        f"evt_sub0_{event_type}",
        event_type,
        user.id,
        event_properties={"vendor_product_id": settings.subscription_product_weekly},
    )
    resp = await _post(client, body)
    assert resp.status_code == 200
    assert resp.json() == {"status": "applied"}

    # subscriptions обновлён (access_level=pro, status=active).
    sub = (
        await session.execute(select(Subscription).where(Subscription.user_id == user.id))
    ).scalar_one()
    assert sub.access_level == "pro"
    assert sub.status == "active"

    # balance НЕ меняется, НЕТ мусорной нулевой credit_grants-строки.
    assert await _balance(session, user.id) == 0
    grant_count = await session.scalar(
        select(func.count())
        .select_from(CreditGrant)
        .where(CreditGrant.idempotency_key == f"evt_sub0_{event_type}")
    )
    assert grant_count == 0

    # billing_events всё равно processed (событие применено).
    ev = (
        await session.execute(
            select(BillingEvent).where(BillingEvent.adapty_event_id == f"evt_sub0_{event_type}")
        )
    ).scalar_one()
    assert ev.processed_at is not None


# ======================= (7) Пак amount=0 =======================


async def test_consumable_zero_amount_pack_applied_no_grant(client, session, monkeypatch):
    """Известный пак amount=0 → 200 applied, но credit_grants НЕ пишется (short-circuit)."""
    settings = get_settings()
    monkeypatch.setattr(settings, "token_pack_products", "free_promo_pack:0", raising=False)
    user = await _user(session, "u_cons_zero000000001")
    resp = await _post(client, _consumable_body("evt_cons_zero", user.id, "free_promo_pack"))
    assert resp.status_code == 200
    assert resp.json() == {"status": "applied"}  # known pack → applied (не ignored)

    assert await _balance(session, user.id) == 0
    grant_count = await session.scalar(
        select(func.count())
        .select_from(CreditGrant)
        .where(CreditGrant.idempotency_key == "evt_cons_zero")
    )
    assert grant_count == 0
    # processed (событие применено, пак известен).
    ev = (
        await session.execute(
            select(BillingEvent).where(BillingEvent.adapty_event_id == "evt_cons_zero")
        )
    ).scalar_one()
    assert ev.processed_at is not None


# ======================= (8) missing customer_user_id =======================


async def test_consumable_missing_customer_user_id(client, session, token_packs):
    """Consumable без customer_user_id → billing_events(user_id=NULL, processed_at=NULL)."""
    body = json.dumps(
        {
            "event_id": "evt_cons_nocuid",
            "event_type": "non_subscription_purchase",
            "event_properties": {"vendor_product_id": "100_tokens_9.99"},
        }
    ).encode("utf-8")
    resp = await _post(client, body)
    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored", "reason": "missing_customer_user_id"}
    ev = (
        await session.execute(
            select(BillingEvent).where(BillingEvent.adapty_event_id == "evt_cons_nocuid")
        )
    ).scalar_one()
    assert ev.user_id is None
    assert ev.processed_at is None


async def test_consumable_unknown_customer_user_id(client, session, token_packs):
    """Consumable с несуществующим customer_user_id → user_id=NULL, ignored:missing_customer."""
    body = _consumable_body("evt_cons_ghost", "u_ghost_does_not_exist1", "100_tokens_9.99")
    resp = await _post(client, body)
    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored", "reason": "missing_customer_user_id"}
    ev = (
        await session.execute(
            select(BillingEvent).where(BillingEvent.adapty_event_id == "evt_cons_ghost")
        )
    ).scalar_one()
    assert ev.user_id is None
    assert ev.processed_at is None


# ======================= (9) refunded вне scope =======================


async def test_non_subscription_purchase_refunded_out_of_scope(client, session, token_packs):
    """non_subscription_purchase_refunded вне KNOWN_EVENT_TYPES → 200 ignored:event_type (no-op)."""
    user = await _user(session, "u_cons_refund0000001")
    body = json.dumps(
        {
            "event_id": "evt_cons_refund",
            "event_type": "non_subscription_purchase_refunded",
            "customer_user_id": user.id,
            "event_properties": {"vendor_product_id": "100_tokens_9.99"},
        }
    ).encode("utf-8")
    resp = await _post(client, body)
    assert resp.status_code == 200
    # Неизвестный event_type → тело несёт event_type, не reason (no-op).
    assert resp.json() == {"status": "ignored", "event_type": "non_subscription_purchase_refunded"}
    # Токены НЕ начислены, событие даже не записано (отбито до insert billing_events).
    assert await _balance(session, user.id) == 0
    ev_count = await session.scalar(
        select(func.count())
        .select_from(BillingEvent)
        .where(BillingEvent.adapty_event_id == "evt_cons_refund")
    )
    assert ev_count == 0
