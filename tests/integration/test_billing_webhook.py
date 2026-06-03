"""Integration: вебхук Adapty POST /v1/billing/webhook/adapty (docs/billing/02 §1, 03 §2).

HMAC-SHA256 верификация (валид→200; невалид/отсутствует→401 без раскрытия); идемпотентность
по adapty_event_id UNIQUE (повтор→200 no-op, без дубля billing_events/subscriptions);
маппинг событий §2.3; неизвестный customer_user_id→billing_events(user_id=NULL,
processed_at=NULL)+200; внутренняя ошибка после валидной подписи→5xx (Adapty retry).

Реальный Postgres (client-фикстура шарит тест-сессию). Подпись считается локально по
ADAPTY_WEBHOOK_SECRET из тест-env. Adapty-граница (getProfile) здесь не вызывается.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.core.config import get_settings
from app.db.models import BillingEvent, Subscription, User

pytestmark = pytest.mark.asyncio

_SIG_HEADER = "adapty-signature"


def _sign(body: bytes) -> str:
    secret = get_settings().adapty_webhook_secret.get_secret_value()
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _body(event_id, event_type, customer_user_id, **kw):  # noqa: ANN001, ANN003
    payload = {
        "event_id": event_id,
        "event_type": event_type,
        "customer_user_id": customer_user_id,
        "profile": kw.get("profile", {"access_level": "pro", "is_active": True}),
        "subscription": kw.get("subscription", {}),
    }
    return json.dumps(payload).encode("utf-8")


async def _post(client, body: bytes, *, sign: bool = True, signature=None):  # noqa: ANN001
    sig = signature if signature is not None else (_sign(body) if sign else None)
    headers = {"Content-Type": "application/json"}
    if sig is not None:
        headers[_SIG_HEADER] = sig
    return await client.post("/v1/billing/webhook/adapty", content=body, headers=headers)


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


# --- Подпись ---


async def test_valid_signature_processes_200(client, session):
    user = await _user(session, "u_wh_valid000000000001")
    body = _body("evt_v1", "subscription_started", user.id)
    resp = await _post(client, body)
    assert resp.status_code == 200


async def test_invalid_signature_returns_401(client, session):
    await _user(session, "u_wh_inv0000000000001")
    body = _body("evt_inv", "subscription_started", "u_wh_inv0000000000001")
    resp = await _post(client, body, signature="deadbeef" * 8)
    assert resp.status_code == 401
    # Без раскрытия причины — generic detail.
    assert "signature" in resp.json()["detail"].lower()


async def test_missing_signature_returns_401(client, session):
    body = _body("evt_nosig", "subscription_started", "u_x")
    resp = await _post(client, body, sign=False)
    assert resp.status_code == 401


# --- Маппинг событий → subscriptions (§2.3) ---


async def test_started_creates_active_subscription(client, session):
    user = await _user(session, "u_wh_started000000001")
    body = _body(
        "evt_s1",
        "subscription_started",
        user.id,
        subscription={"expires_at": "2026-07-02T00:00:00Z", "will_renew": True},
    )
    assert (await _post(client, body)).status_code == 200
    sub = (
        await session.execute(select(Subscription).where(Subscription.user_id == user.id))
    ).scalar_one()
    assert sub.status == "active"
    assert sub.access_level == "pro"
    assert sub.grace_until is None


async def test_expired_sets_grace(client, session):
    user = await _user(session, "u_wh_expired00000001")
    await _post(client, _body("evt_e0", "subscription_started", user.id))
    body = _body(
        "evt_e1",
        "subscription_expired",
        user.id,
        profile={"access_level": "pro", "is_active": False},
        subscription={"expires_at": "2026-06-10T00:00:00Z"},
    )
    assert (await _post(client, body)).status_code == 200
    sub = (
        await session.execute(select(Subscription).where(Subscription.user_id == user.id))
    ).scalar_one()
    assert sub.status == "grace"
    assert sub.grace_until is not None
    assert sub.access_level == "pro"  # сохраняется в grace


async def test_billing_issue_sets_billing_issue(client, session):
    user = await _user(session, "u_wh_bissue000000001")
    await _post(client, _body("evt_b0", "subscription_started", user.id))
    body = _body(
        "evt_b1",
        "billing_issue_detected",
        user.id,
        profile={"access_level": "pro", "is_active": False},
    )
    assert (await _post(client, body)).status_code == 200
    sub = (
        await session.execute(select(Subscription).where(Subscription.user_id == user.id))
    ).scalar_one()
    assert sub.status == "billing_issue"


async def test_renew_in_grace_returns_active_grace_null(client, session):
    user = await _user(session, "u_wh_renewgr0000001")
    await _post(client, _body("evt_g0", "subscription_started", user.id))
    await _post(
        client,
        _body(
            "evt_g1",
            "subscription_expired",
            user.id,
            profile={"access_level": "pro", "is_active": False},
            subscription={"expires_at": "2026-06-10T00:00:00Z"},
        ),
    )
    # Renew в grace → active + grace_until=NULL (отмена pending-teardown).
    renew = await _post(client, _body("evt_g2", "subscription_renewed", user.id))
    assert renew.status_code == 200
    sub = (
        await session.execute(select(Subscription).where(Subscription.user_id == user.id))
    ).scalar_one()
    assert sub.status == "active"
    assert sub.grace_until is None


# --- Идемпотентность по adapty_event_id ---


async def test_duplicate_event_id_is_noop_200(client, session):
    user = await _user(session, "u_wh_dup00000000001")
    body = _body("evt_dup_1", "subscription_started", user.id)
    assert (await _post(client, body)).status_code == 200
    # Повтор того же event_id → 200 no-op, без дубля.
    assert (await _post(client, body)).status_code == 200

    ev_count = await session.scalar(
        select(func.count())
        .select_from(BillingEvent)
        .where(BillingEvent.adapty_event_id == "evt_dup_1")
    )
    assert ev_count == 1
    sub_count = await session.scalar(
        select(func.count()).select_from(Subscription).where(Subscription.user_id == user.id)
    )
    assert sub_count == 1


async def test_processed_event_has_user_and_processed_at(client, session):
    user = await _user(session, "u_wh_proc0000000001")
    body = _body("evt_proc_1", "subscription_started", user.id)
    await _post(client, body)
    ev = (
        await session.execute(
            select(BillingEvent).where(BillingEvent.adapty_event_id == "evt_proc_1")
        )
    ).scalar_one()
    assert ev.user_id == user.id
    assert ev.processed_at is not None


# --- Неизвестный customer_user_id (рассинхрон) ---


async def test_unknown_customer_user_id_saved_user_null_200(client, session):
    body = _body("evt_unknown_1", "subscription_started", "u_does_not_exist_00001")
    resp = await _post(client, body)
    assert resp.status_code == 200  # событие не теряется
    ev = (
        await session.execute(
            select(BillingEvent).where(BillingEvent.adapty_event_id == "evt_unknown_1")
        )
    ).scalar_one()
    assert ev.user_id is None
    assert ev.processed_at is None  # не обработано (добивается ресинком/повтором)


# --- Внутренняя ошибка после валидной подписи → 5xx (Adapty retry) ---


async def _client_5xx(session):  # noqa: ANN001, ANN202
    """ASGI-клиент, транслирующий unhandled-исключения в HTTP-5xx (raise_app_exceptions=False).

    Базовая client-фикстура поднимает исключение в тесте; для проверки контракта «5xx →
    Adapty retry» нужен ответ со статусом, а не проброс. Шарит ту же тест-сессию.
    """
    import httpx

    from app.api.main import app
    from app.db.session import get_session

    async def _override():  # noqa: ANN202
        yield session

    app.dependency_overrides[get_session] = _override
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_internal_error_after_valid_signature_returns_5xx(session, monkeypatch):
    user = await _user(session, "u_wh_err000000000001")

    import app.billing.webhook_handler as wh
    from app.api.main import app
    from app.db.session import get_session

    async def _boom(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise RuntimeError("apply failed")

    monkeypatch.setattr(wh.subscription_state, "apply_webhook_event", _boom)

    client = await _client_5xx(session)
    try:
        body = _body("evt_err_1", "subscription_started", user.id)
        resp = await _post(client, body)
        assert resp.status_code >= 500  # Adapty повторит доставку
    finally:
        await client.aclose()
        app.dependency_overrides.pop(get_session, None)


async def test_missing_event_id_after_valid_signature_returns_5xx(session):
    from app.api.main import app
    from app.db.session import get_session

    client = await _client_5xx(session)
    try:
        # Контрактный минимум отсутствует, подпись валидна → 5xx (не маскируем 200).
        body = json.dumps(
            {"event_type": "subscription_started", "customer_user_id": "u_x"}
        ).encode()
        resp = await _post(client, body)
        assert resp.status_code >= 500
    finally:
        await client.aclose()
        app.dependency_overrides.pop(get_session, None)
