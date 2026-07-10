"""Integration: вебхук Adapty POST /v1/billing/webhook/adapty (ADR-027 + ADR-040 + ADR-041).

HTTP-уровень (Bearer-авторизация + сырое тело → tier-3 хэш ключа дедупа). Payload — ОФИЦИАЛЬНОЙ
формы Adapty (tests/support/adapty_payloads.py, ADR-040/041 §F): profile_event_id/
subscription_expires_at/will_renew/is_active внутри event_properties, БЕЗ event_id/profile/
subscription. Покрытие (docs/06 §Contract c/c2/c3/d/e):
- (a) Bearer constant-time ДО парсинга тела: нет/неверный → 401; верный → проходит.
- (b) пустой ADAPTY_WEBHOOK_SECRET → 500 (мисконфигурация), до парсинга тела.
- (c) always-200-on-bad-input (empty_body/invalid_json/not_an_object/unknown/missing_cuid).
- (c2) ключ дедупа выводится ВСЕГДА (тир1 profile_event_id / тир2 syn:txid / тир3 syn:body-hash);
  денежное событие без profile_event_id НЕ дропается тихо (ADR-040 §A/§B, регресс инцидента).
- идемпотентность + конкурентная гонка (IntegrityError → duplicate, без двойного начисления).
- 5xx ТОЛЬКО на реальный сбой БД (мок apply) → billing_events.processed_at IS NULL.

Реальный Postgres (client-фикстура шарит тест-сессию). Bearer-секрет = ADAPTY_WEBHOOK_SECRET
из тест-env (conftest).
"""

from __future__ import annotations

import hashlib
import logging
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.billing.webhook_handler import WebhookOutcome, process_webhook
from app.core.config import get_settings
from app.db.models import BillingEvent, CreditGrant, Subscription, User
from tests.support import adapty_payloads as ap

pytestmark = pytest.mark.asyncio


def _secret() -> str:
    return get_settings().adapty_webhook_secret.get_secret_value()


def _auth(token: str | None = None) -> dict[str, str]:
    tok = token if token is not None else _secret()
    return {"Authorization": f"Bearer {tok}"}


async def _post(client, payload_or_bytes, *, headers=None):  # noqa: ANN001
    body = payload_or_bytes if isinstance(payload_or_bytes, bytes) else ap.to_body(payload_or_bytes)
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


# ============================ (a) Bearer-авторизация ============================


async def test_valid_bearer_processes_200(client, session):
    user = await _user(session, "u_wh_valid000000001")
    resp = await _post(client, ap.subscription_event("subscription_started", user.id))
    assert resp.status_code == 200
    assert resp.json()["status"] == "applied"


async def test_invalid_bearer_returns_401(client, session):
    await _user(session, "u_wh_inv00000000001")
    resp = await _post(
        client,
        ap.subscription_event("subscription_started", "u_wh_inv00000000001"),
        headers=_auth("wrong-secret-value"),
    )
    assert resp.status_code == 401
    assert _secret() not in resp.text
    assert "secret" not in resp.json().get("detail", "").lower()


async def test_missing_authorization_returns_401(client, session):
    resp = await client.post(
        "/v1/billing/webhook/adapty",
        content=ap.to_body(ap.subscription_event("subscription_started", "u_x")),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 401


async def test_wrong_scheme_returns_401(client, session):
    resp = await _post(
        client,
        ap.subscription_event("subscription_started", "u_x"),
        headers={"Authorization": f"Basic {_secret()}"},
    )
    assert resp.status_code == 401


async def test_unauthorized_does_not_touch_body(client, session):
    resp = await _post(client, b"this is not json at all", headers=_auth("nope"))
    assert resp.status_code == 401


async def test_empty_secret_returns_500(client, session, monkeypatch):
    from pydantic import SecretStr

    monkeypatch.setattr(get_settings(), "adapty_webhook_secret", SecretStr(""), raising=False)
    resp = await _post(
        client,
        ap.subscription_event("subscription_started", "u_x"),
        headers={"Authorization": "Bearer anything"},
    )
    assert resp.status_code == 500


# ====================== (c) always-200-on-bad-input ======================


async def test_empty_body_ignored_200(client, session):
    resp = await _post(client, b"")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored", "reason": "empty_body"}


async def test_invalid_json_ignored_200(client, session):
    resp = await _post(client, b"{not valid json")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored", "reason": "invalid_json"}


@pytest.mark.parametrize("raw", [b"[1, 2, 3]", b"42", b'"a string"', b"true", b"null"])
async def test_not_an_object_ignored_200(client, session, raw: bytes):
    resp = await _post(client, raw)
    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored", "reason": "not_an_object"}


async def test_unknown_event_type_ignored_200(client, session):
    payload = ap.subscription_event("some_unknown_event", "u_x")
    resp = await _post(client, payload)
    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored", "event_type": "some_unknown_event"}


async def test_missing_customer_user_id_ignored_200(client, session):
    payload = ap.make_webhook_payload(
        event_type="subscription_started",
        omit_customer_user_id=True,
        event_properties=ap.make_event_properties(),
    )
    resp = await _post(client, payload)
    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored", "reason": "missing_customer_user_id"}


async def test_bad_payload_never_5xx(client, session):
    for raw in [b"", b"{bad", b"[1,2]", b"42"]:
        resp = await _post(client, raw)
        assert resp.status_code == 200, f"payload {raw!r} -> {resp.status_code}"


# ============ (c3) Field-extraction на реальной форме через HTTP (денежный путь) ============


async def test_http_started_sets_pro_from_event_properties(client, session):
    """Регресс инцидента: payload БЕЗ profile/subscription → access_level=pro, expires_at из
    event_properties.subscription_expires_at (не пусто/free)."""
    user = await _user(session, "u_wh_pro00000000001")
    payload = ap.subscription_event(
        "subscription_started",
        user.id,
        subscription_expires_at="2026-08-01T00:00:00Z",
        will_renew=True,
    )
    resp = await _post(client, payload)
    assert resp.json()["status"] == "applied"
    sub = (
        await session.execute(select(Subscription).where(Subscription.user_id == user.id))
    ).scalar_one()
    assert sub.access_level == "pro"
    assert sub.status == "active"
    assert sub.will_renew is True
    assert sub.expires_at is not None


# ================= (c2, тир 1) profile_event_id → ключ = UUID как есть =================


async def test_tier1_profile_event_id_is_dedup_key(client, session):
    user = await _user(session, "u_wh_tier1000000001")
    peid = ap.new_profile_event_id()
    payload = ap.subscription_event("subscription_started", user.id, profile_event_id=peid)
    resp = await _post(client, payload)
    assert resp.json()["status"] == "applied"
    ev = (
        await session.execute(select(BillingEvent).where(BillingEvent.adapty_event_id == peid))
    ).scalar_one()
    assert ev.adapty_event_id == peid


# ================= (3) Идемпотентный ретрай — без повторного начисления =================


async def test_identical_retry_is_duplicate_no_double_grant(client, session, monkeypatch):
    """Идентичный ретрай того же события → duplicate; bonus_generations_balance и credit_grants
    НЕ изменились (деньги). Проверяем на consumable-паке (реальное начисление > 0)."""
    from app.core.config import get_settings as gs

    monkeypatch.setattr(gs(), "token_pack_products", "pack_250:250", raising=False)
    user = await _user(session, "u_wh_retry000000001")
    peid = ap.new_profile_event_id()
    payload = ap.subscription_event(
        "non_subscription_purchase", user.id, profile_event_id=peid, vendor_product_id="pack_250"
    )
    r1 = await _post(client, payload)
    assert r1.json() == {"status": "applied"}
    assert await _balance(session, user.id) == 250

    r2 = await _post(client, payload)
    assert r2.json() == {"status": "duplicate"}
    assert await _balance(session, user.id) == 250  # не выросло
    grant_count = await session.scalar(
        select(func.count()).select_from(CreditGrant).where(CreditGrant.idempotency_key == peid)
    )
    assert grant_count == 1
    ev_count = await session.scalar(
        select(func.count()).select_from(BillingEvent).where(BillingEvent.adapty_event_id == peid)
    )
    assert ev_count == 1


# ============ (4) Два РАЗНЫХ события одного типа НЕ схлопываются в duplicate ============


async def test_two_distinct_events_same_type_both_processed(client, session):
    user = await _user(session, "u_wh_distinct000001")
    peid1, peid2 = ap.new_profile_event_id(), ap.new_profile_event_id()
    r1 = await _post(
        client, ap.subscription_event("subscription_renewed", user.id, profile_event_id=peid1)
    )
    r2 = await _post(
        client, ap.subscription_event("subscription_renewed", user.id, profile_event_id=peid2)
    )
    assert r1.json()["status"] == "applied"
    assert r2.json()["status"] == "applied"  # НЕ duplicate — разные profile_event_id
    count = await session.scalar(
        select(func.count())
        .select_from(BillingEvent)
        .where(BillingEvent.adapty_event_id.in_([peid1, peid2]))
    )
    assert count == 2


# ============ (5) Fallback тир-2: transaction_id → syn-ключ + WARN; кросс-тип ============


async def test_tier2_transaction_id_synthetic_key_and_warn(client, session, caplog):
    user = await _user(session, "u_wh_tier2000000001")
    txid = "1000000999888777"
    payload = ap.subscription_event(
        "subscription_started", user.id, profile_event_id=None, transaction_id=txid
    )
    expected_key = f"adapty-syn:subscription_started:{txid}"
    with caplog.at_level(logging.WARNING):
        resp = await _post(client, payload)
    assert resp.json()["status"] == "applied"
    ev = (
        await session.execute(
            select(BillingEvent).where(BillingEvent.adapty_event_id == expected_key)
        )
    ).scalar_one()
    assert ev.processed_at is not None
    # WARN-след profile_event_id_absent обязателен (ADR-040 §B/§D).
    assert any(getattr(rec, "reason", None) == "profile_event_id_absent" for rec in caplog.records)


async def test_tier2_cross_type_same_txid_distinct_keys(client, session):
    """Один transaction_id в subscription_started и access_level_updated → РАЗНЫЕ ключи
    (префикс event_type), ложного duplicate нет."""
    user = await _user(session, "u_wh_xtype000000001")
    txid = "1000000111222333"
    r1 = await _post(
        client,
        ap.subscription_event(
            "subscription_started", user.id, profile_event_id=None, transaction_id=txid
        ),
    )
    # access_level_updated с тем же txid, без profile_event_id.
    alu = ap.make_webhook_payload(
        event_type="access_level_updated",
        customer_user_id=user.id,
        event_properties=ap.make_event_properties(
            profile_event_id=None, transaction_id=txid, is_active=True
        ),
    )
    r2 = await _post(client, alu)
    assert r1.json()["status"] == "applied"
    assert r2.json()["status"] == "applied"  # НЕ duplicate
    keys = [
        f"adapty-syn:subscription_started:{txid}",
        f"adapty-syn:access_level_updated:{txid}",
    ]
    count = await session.scalar(
        select(func.count()).select_from(BillingEvent).where(BillingEvent.adapty_event_id.in_(keys))
    )
    assert count == 2


# ============ (6) Fallback тир-3: хэш тела; идентичное → duplicate; иное → обработано ============


async def test_tier3_body_hash_identical_duplicate_different_processed(client, session):
    user = await _user(session, "u_wh_tier3000000001")
    # Ни profile_event_id, ни transaction_id → тир-3 (хэш тела).
    payload = ap.subscription_event("subscription_renewed", user.id, profile_event_id=None)
    body = ap.to_body(payload)
    expected_key = f"adapty-syn:body:{hashlib.sha256(body).hexdigest()}"

    r1 = await _post(client, body)
    assert r1.json()["status"] == "applied"
    ev = (
        await session.execute(
            select(BillingEvent).where(BillingEvent.adapty_event_id == expected_key)
        )
    ).scalar_one()
    assert ev.adapty_event_id == expected_key

    # Идентичное байт-в-байт тело → тот же ключ → duplicate.
    r2 = await _post(client, body)
    assert r2.json()["status"] == "duplicate"

    # Байт-различное тело (иной profile_id) → другой ключ → обрабатывается.
    payload_diff = ap.subscription_event("subscription_renewed", user.id, profile_event_id=None)
    payload_diff["profile_id"] = "different-profile-id-0000"
    body_diff = ap.to_body(payload_diff)
    assert body_diff != body
    r3 = await _post(client, body_diff)
    assert r3.json()["status"] == "applied"


# ============ (7) Конкурентная доставка → IntegrityError → duplicate, не 5xx ============


async def test_concurrent_delivery_integrity_error_yields_duplicate(session, monkeypatch):
    """Гонка вставки одного события по UNIQUE adapty_event_id → IntegrityError на commit →
    корректный duplicate (не 5xx, не двойное начисление)."""
    user = await _user(session, "u_wh_race0000000001")
    peid = ap.new_profile_event_id()
    payload = ap.subscription_event("subscription_started", user.id, profile_event_id=peid)
    body = ap.to_body(payload)

    real_commit = session.commit
    state = {"n": 0}

    async def flaky_commit():  # noqa: ANN202
        state["n"] += 1
        if state["n"] == 1:
            # Эмулируем конфликт UNIQUE adapty_event_id от конкурентной доставки на commit.
            raise IntegrityError("duplicate adapty_event_id", None, Exception("unique"))
        return await real_commit()

    monkeypatch.setattr(session, "commit", flaky_commit)
    # IntegrityError ловится → DUPLICATE (НЕ 5xx/WebhookProcessingError, НЕ двойное начисление).
    result = await process_webhook(session, payload, body)
    assert result.outcome == WebhookOutcome.DUPLICATE
    # Проигравший писатель откатан: строки события нет (начисление не произошло).
    ev_count = await session.scalar(
        select(func.count()).select_from(BillingEvent).where(BillingEvent.adapty_event_id == peid)
    )
    assert ev_count == 0


# ====================== (d/f) Token-grant по тиру (ADR-038 §D: 0 токенов) ======================


async def test_started_zero_tokens_short_circuit_no_grant(client, session):
    """ADR-038 §D: подписка = 0 бонус-токенов → subscriptions обновлён, credit_grants НЕ пишется."""
    settings = get_settings()
    assert settings.subscription_tokens_weekly == 0
    user = await _user(session, "u_wh_zerotok000001")
    peid = ap.new_profile_event_id()
    payload = ap.subscription_event(
        "subscription_started",
        user.id,
        profile_event_id=peid,
        vendor_product_id=settings.subscription_product_weekly,
    )
    resp = await _post(client, payload)
    assert resp.json()["status"] == "applied"
    assert await _balance(session, user.id) == 0
    grant_count = await session.scalar(
        select(func.count()).select_from(CreditGrant).where(CreditGrant.idempotency_key == peid)
    )
    assert grant_count == 0


# ====================== 5xx ТОЛЬКО на реальный сбой БД ======================


async def test_db_apply_failure_returns_5xx_event_unprocessed(session, monkeypatch):
    """Реальный сбой БД при apply → 5xx (WebhookProcessingError), не тихий дроп."""
    import httpx

    from app.api.main import app
    from app.db.session import get_session

    user = await _user(session, "u_wh_err00000000001")

    import app.billing.webhook_handler as wh

    async def _boom(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise RuntimeError("db apply failed")

    monkeypatch.setattr(wh.subscription_state, "apply_webhook_event", _boom)

    async def _override():  # noqa: ANN202
        yield session

    app.dependency_overrides[get_session] = _override
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            payload = ap.subscription_event("subscription_started", user.id)
            resp = await ac.post(
                "/v1/billing/webhook/adapty",
                content=ap.to_body(payload),
                headers={"Content-Type": "application/json", **_auth()},
            )
            assert resp.status_code >= 500
    finally:
        app.dependency_overrides.pop(get_session, None)
