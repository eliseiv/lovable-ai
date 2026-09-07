"""Integration: RU-оплата через платёжный агрегатор (ADR-052).

Покрытие:
  - `POST /billing/cloudpayments/checkout` — ссылка создаётся с `user_id` из Bearer'а, а не из
    тела; выключенный канал → `503`, сбой провайдера → `502`; без авторизации → `401`;
  - `POST /billing/cloudpayments/webhook` — начисление токенов и подписки по подтверждённым
    платежам, идемпотентность по `payment_id`, `200 {"code": 0}` на любом разобранном событии,
    `500` при выключенном канале и недоступной сверке (провайдер повторит доставку).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.billing import cloudpayments
from app.core.config import get_settings
from app.core.security import hash_api_key
from app.db.models import CloudPaymentsPayment, Subscription, User

pytestmark = pytest.mark.asyncio

_UID = "u_cloudpay0000001"


async def _user(session, uid: str = _UID) -> User:  # noqa: ANN001
    user = User(
        id=uid,
        api_key_hash=hash_api_key(f"{uid}-legacy-key"),
        monthly_budget_usd=Decimal("50.0000"),
        status="active",
    )
    session.add(user)
    await session.flush()
    return user


def _auth(uid: str = _UID) -> dict[str, str]:
    return {"Authorization": f"Bearer {uid}-legacy-key"}


# Каталог токен-паков теста: суммы начисления берутся ТОЛЬКО отсюда, что и проверяется.
_TOKEN_PACKS = {"100_tokens_9.99": 100, "250_tokens_19.99": 250}


@pytest.fixture
def configured(monkeypatch):  # noqa: ANN201
    """Включает RU-канал на время теста (ключи агрегатора и каталог паков заданы)."""
    settings = get_settings()
    monkeypatch.setattr(settings, "cloudpayments_api_base", "https://pay.example.dev/api/v1")
    monkeypatch.setattr(settings, "cloudpayments_app_id", "app-1")
    monkeypatch.setattr(settings, "cloudpayments_api_token", "token-1")
    # Каталог паков задаётся сырой настройкой: token_pack_map читает её и кэширует разбор
    # по значению строки, поэтому подмена значения даёт нужную карту.
    monkeypatch.setattr(
        settings,
        "token_pack_products",
        ",".join(f"{code}:{amount}" for code, amount in _TOKEN_PACKS.items()),
    )
    return settings


def _payment(payment_id: str, code: str, payment_type: str) -> dict[str, object]:
    return {
        "payment_id": payment_id,
        "status": "succeeded",
        "paid_at": datetime.now(UTC).isoformat(),
        "product": {"code": code, "payment_type": payment_type},
    }


# ============================ checkout ============================


async def test_checkout_returns_link_and_sends_authenticated_user(
    client, session, monkeypatch, configured
):
    await _user(session)
    captured: dict[str, object] = {}

    async def fake_link(settings, *, user_id, product_id, customer_email):  # noqa: ANN001, ANN202
        captured.update(user_id=user_id, product_id=product_id, email=customer_email)
        return cloudpayments.CheckoutLink(
            payment_id="pay-1",
            payment_url="https://pay.example.dev/checkout/pay-1",
            status="pending",
            expires_at=None,
        )

    monkeypatch.setattr(cloudpayments, "create_payment_link", fake_link)

    resp = await client.post(
        "/v1/billing/cloudpayments/checkout",
        headers=_auth(),
        json={"product_id": "100_tokens_9.99", "customer_email": "buyer@example.com"},
    )

    assert resp.status_code == 200
    assert resp.json()["payment_url"] == "https://pay.example.dev/checkout/pay-1"
    # user_id уходит провайдеру из токена, а не из тела: иначе платёж ушёл бы на чужой аккаунт.
    assert captured["user_id"] == _UID


async def test_checkout_requires_auth(client):
    resp = await client.post(
        "/v1/billing/cloudpayments/checkout",
        json={"product_id": "100_tokens_9.99", "customer_email": "buyer@example.com"},
    )
    assert resp.status_code == 401


async def test_checkout_without_configuration_is_503(client, session):
    """Канал выключен на инстансе — честный 503, а не попытка сходить в никуда."""
    await _user(session)

    resp = await client.post(
        "/v1/billing/cloudpayments/checkout",
        headers=_auth(),
        json={"product_id": "100_tokens_9.99", "customer_email": "buyer@example.com"},
    )
    assert resp.status_code == 503


async def test_checkout_upstream_failure_is_502(client, session, monkeypatch, configured):
    await _user(session)

    async def failing(settings, **kwargs):  # noqa: ANN001, ANN202
        raise cloudpayments.CloudPaymentsUpstreamError("boom")

    monkeypatch.setattr(cloudpayments, "create_payment_link", failing)

    resp = await client.post(
        "/v1/billing/cloudpayments/checkout",
        headers=_auth(),
        json={"product_id": "100_tokens_9.99", "customer_email": "buyer@example.com"},
    )
    assert resp.status_code == 502
    # Детали апстрима наружу не уходят.
    assert "boom" not in resp.text


# ============================ webhook ============================


async def test_webhook_credits_tokens_from_server_side_map(
    client, session, monkeypatch, configured
):
    """Сумма берётся из TOKEN_PACK_PRODUCTS, а не из данных провайдера (анти-подмена)."""
    user = await _user(session)
    await session.commit()

    async def fake_list(settings, *, user_id):  # noqa: ANN001, ANN202
        # Провайдер «сообщает» вздорную сумму — она не должна ни на что влиять.
        payment = _payment("pay-tokens", "100_tokens_9.99", "one_time")
        payment["amount"] = 999999
        return [payment]

    monkeypatch.setattr(cloudpayments, "list_payments", fake_list)

    resp = await client.post(
        "/v1/billing/cloudpayments/webhook",
        content=json.dumps({"user_id": _UID}).encode(),
    )

    assert resp.status_code == 200
    assert resp.json() == {"code": 0}
    await session.refresh(user)
    assert user.bonus_generations_balance == _TOKEN_PACKS["100_tokens_9.99"]
    row = await session.get(CloudPaymentsPayment, "pay-tokens")
    assert row is not None
    assert row.kind == "tokens"


async def test_webhook_grants_subscription(client, session, monkeypatch, configured):
    await _user(session)
    await session.commit()

    async def fake_list(settings, *, user_id):  # noqa: ANN001, ANN202
        return [_payment("pay-sub", "week_6.99_nottrial", "subscription")]

    monkeypatch.setattr(cloudpayments, "list_payments", fake_list)

    resp = await client.post(
        "/v1/billing/cloudpayments/webhook",
        content=json.dumps({"user_id": _UID}).encode(),
    )

    assert resp.status_code == 200
    # PK подписки — свой id, поэтому ищем по user_id (одна актуальная строка на пользователя).
    sub = (
        await session.execute(select(Subscription).where(Subscription.user_id == _UID))
    ).scalar_one_or_none()
    assert sub is not None
    assert sub.access_level == "pro"
    assert sub.store == "cloudpayments"
    assert sub.expires_at is not None


async def test_webhook_is_idempotent_per_payment(client, session, monkeypatch, configured):
    """Повторная доставка того же платежа не начисляет второй раз."""
    user = await _user(session)
    await session.commit()

    async def fake_list(settings, *, user_id):  # noqa: ANN001, ANN202
        return [_payment("pay-once", "250_tokens_19.99", "one_time")]

    monkeypatch.setattr(cloudpayments, "list_payments", fake_list)
    body = json.dumps({"user_id": _UID}).encode()

    first = await client.post("/v1/billing/cloudpayments/webhook", content=body)
    second = await client.post("/v1/billing/cloudpayments/webhook", content=body)

    assert (first.status_code, second.status_code) == (200, 200)
    await session.refresh(user)
    assert user.bonus_generations_balance == _TOKEN_PACKS["250_tokens_19.99"]


async def test_webhook_ignores_unparsable_body(client, configured):
    """Кривой callback — 200: провайдер не должен ретраить заведомо неисправимое."""
    resp = await client.post("/v1/billing/cloudpayments/webhook", content=b"not json")

    assert resp.status_code == 200
    assert resp.json() == {"code": 0}


async def test_webhook_ignores_unknown_user(client, configured):
    resp = await client.post(
        "/v1/billing/cloudpayments/webhook",
        content=json.dumps({"user_id": "u_nosuchuser0001"}).encode(),
    )
    assert resp.status_code == 200


async def test_webhook_without_configuration_is_500(client, session):
    """Мисконфиг: провайдер обязан повторить доставку после починки, поэтому 500."""
    await _user(session)
    await session.commit()

    resp = await client.post(
        "/v1/billing/cloudpayments/webhook",
        content=json.dumps({"user_id": _UID}).encode(),
    )
    assert resp.status_code == 500


async def test_webhook_returns_500_when_verification_unavailable(
    client, session, monkeypatch, configured
):
    """Сверка недоступна → 500, платёж не теряется: провайдер повторит callback."""
    await _user(session)
    await session.commit()

    async def unavailable(settings, *, user_id):  # noqa: ANN001, ANN202
        raise cloudpayments.CloudPaymentsUnavailable("down")

    monkeypatch.setattr(cloudpayments, "list_payments", unavailable)

    resp = await client.post(
        "/v1/billing/cloudpayments/webhook",
        content=json.dumps({"user_id": _UID}).encode(),
    )
    assert resp.status_code == 500
