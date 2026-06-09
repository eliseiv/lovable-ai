"""Integration: вебхук Adapty POST /v1/billing/webhook/adapty (ADR-027, docs/billing/02 §1).

Ревизия ADR-027 (HMAC → Bearer + token-grant):
- (a) Bearer-авторизация constant-time ДО парсинга тела: нет/неверный → 401; верный → проходит.
- (b) пустой ADAPTY_WEBHOOK_SECRET → 500 (мисконфигурация), до парсинга тела.
- (c) always-200-on-bad-input после авторизации (empty_body/invalid_json/not_an_object/
      missing_event_id/unknown event_type/missing_customer_user_id) — НИКОГДА не 4xx/5xx.
- (d) token-grant started/renewed: subscriptions + bonus_generations_balance += tier +
      credit_grants(created_by='adapty', idempotency_key=event_id) в одной транзакции.
- (e) идемпотентность event_id → 200 duplicate, баланс/credit_grants не растут.
- cancelled → will_renew=false, токены/access не трогаем.
- 5xx ТОЛЬКО на реальный сбой БД (мок коммита) → billing_events.processed_at IS NULL.

Реальный Postgres (client-фикстура шарит тест-сессию). Bearer-секрет = ADAPTY_WEBHOOK_SECRET
из тест-env (conftest). Adapty-граница (getProfile) здесь не вызывается.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.core.config import get_settings
from app.db.models import BillingEvent, CreditGrant, Subscription, User

pytestmark = pytest.mark.asyncio


def _secret() -> str:
    return get_settings().adapty_webhook_secret.get_secret_value()


def _auth(token: str | None = None) -> dict[str, str]:
    """Заголовок Authorization: Bearer <token>. По умолчанию — валидный секрет вебхука."""
    tok = token if token is not None else _secret()
    return {"Authorization": f"Bearer {tok}"}


def _body(event_id, event_type, customer_user_id, **kw):  # noqa: ANN001, ANN003
    payload = {
        "event_id": event_id,
        "event_type": event_type,
        "customer_user_id": customer_user_id,
        "profile": kw.get("profile", {"access_level": "pro", "is_active": True}),
        "subscription": kw.get("subscription", {}),
    }
    if "event_properties" in kw:
        payload["event_properties"] = kw["event_properties"]
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


# ============================ (a) Bearer-авторизация ============================


async def test_valid_bearer_processes_200(client, session):
    user = await _user(session, "u_wh_valid000000000001")
    body = _body("evt_v1", "subscription_started", user.id)
    resp = await _post(client, body)
    assert resp.status_code == 200


async def test_invalid_bearer_returns_401(client, session):
    await _user(session, "u_wh_inv0000000000001")
    body = _body("evt_inv", "subscription_started", "u_wh_inv0000000000001")
    resp = await _post(client, body, headers=_auth("wrong-secret-value"))
    assert resp.status_code == 401
    # Без раскрытия причины (нет утечки секрета/деталей).
    detail = resp.json().get("detail", "").lower()
    assert _secret() not in resp.text
    assert "secret" not in detail


async def test_missing_authorization_returns_401(client, session):
    body = _body("evt_nosig", "subscription_started", "u_x")
    resp = await client.post(
        "/v1/billing/webhook/adapty", content=body, headers={"Content-Type": "application/json"}
    )
    assert resp.status_code == 401


async def test_wrong_scheme_returns_401(client, session):
    body = _body("evt_basic", "subscription_started", "u_x")
    resp = await _post(client, body, headers={"Authorization": f"Basic {_secret()}"})
    assert resp.status_code == 401


async def test_unauthorized_does_not_touch_body(client, session):
    """Авторизация ДО парсинга тела: при 401 кривое (не-JSON) тело не обрабатывается → не 5xx."""
    resp = await _post(client, b"this is not json at all", headers=_auth("nope"))
    assert resp.status_code == 401  # не 200 ignored, не 5xx — отбито на авторизации


async def test_empty_secret_returns_500(client, session, monkeypatch):
    """Пустой ADAPTY_WEBHOOK_SECRET → 500 (мисконфигурация), ДО парсинга тела (ADR-027 §A/§B)."""
    from pydantic import SecretStr

    settings = get_settings()
    monkeypatch.setattr(settings, "adapty_webhook_secret", SecretStr(""), raising=False)
    # Даже валидное по форме тело + любой Bearer → 500 (секрет не сконфигурирован).
    body = _body("evt_misconf", "subscription_started", "u_x")
    resp = await _post(client, body, headers={"Authorization": "Bearer anything"})
    assert resp.status_code == 500


async def test_empty_secret_500_before_body_parse(client, session, monkeypatch):
    """При пустом секрете кривое тело тоже даёт 500 (тело не парсится до авторизации)."""
    from pydantic import SecretStr

    settings = get_settings()
    monkeypatch.setattr(settings, "adapty_webhook_secret", SecretStr(""), raising=False)
    resp = await _post(client, b"\xff\xfe not json", headers={"Authorization": "Bearer x"})
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


async def test_non_utf8_body_ignored_200(client, session):
    # Не-декодируемое тело → invalid_json (UnicodeDecodeError тоже отбит, не 5xx).
    resp = await _post(client, b"\xff\xfe\xfd")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


@pytest.mark.parametrize("raw", [b"[1, 2, 3]", b"42", b'"a string"', b"true", b"null"])
async def test_not_an_object_ignored_200(client, session, raw: bytes):
    resp = await _post(client, raw)
    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored", "reason": "not_an_object"}


async def test_missing_event_id_ignored_200(client, session):
    body = json.dumps({"event_type": "subscription_started", "customer_user_id": "u_x"}).encode()
    resp = await _post(client, body)
    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored", "reason": "missing_event_id"}


async def test_unknown_event_type_ignored_200(client, session):
    body = json.dumps(
        {"event_id": "evt_unk_type", "event_type": "some_unknown_event", "customer_user_id": "u_x"}
    ).encode()
    resp = await _post(client, body)
    assert resp.status_code == 200
    # docs §1: для неизвестного event_type тело несёт event_type, не reason.
    assert resp.json() == {"status": "ignored", "event_type": "some_unknown_event"}


async def test_missing_customer_user_id_ignored_200(client, session):
    body = json.dumps({"event_id": "evt_no_cuid", "event_type": "subscription_started"}).encode()
    resp = await _post(client, body)
    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored", "reason": "missing_customer_user_id"}


async def test_unknown_customer_user_id_saved_user_null_200(client, session):
    """Неизвестный customer_user_id → billing_events(user_id=NULL, processed_at=NULL)."""
    body = _body("evt_unknown_1", "subscription_started", "u_does_not_exist_00001")
    resp = await _post(client, body)
    assert resp.status_code == 200
    assert resp.json()["reason"] == "missing_customer_user_id"
    ev = (
        await session.execute(
            select(BillingEvent).where(BillingEvent.adapty_event_id == "evt_unknown_1")
        )
    ).scalar_one()
    assert ev.user_id is None
    assert ev.processed_at is None  # не обработано (добивается ресинком/повтором)


# ====================== Дефенсивный парсинг (ADR-027 §C) ======================


async def test_event_id_from_id_field(client, session):
    """event_id = event_id || id — извлекается из 'id', если нет 'event_id'."""
    user = await _user(session, "u_wh_idfield00000001")
    body = json.dumps(
        {
            "id": "evt_from_id",
            "event_type": "subscription_started",
            "customer_user_id": user.id,
            "profile": {"access_level": "pro", "is_active": True},
        }
    ).encode()
    resp = await _post(client, body)
    assert resp.status_code == 200
    assert resp.json()["status"] == "applied"
    ev = (
        await session.execute(
            select(BillingEvent).where(BillingEvent.adapty_event_id == "evt_from_id")
        )
    ).scalar_one()
    assert ev.adapty_event_id == "evt_from_id"


async def test_event_type_case_insensitive(client, session):
    """event_type нормализуется .lower() — UPPERCASE матчится."""
    user = await _user(session, "u_wh_upcase000000001")
    body = json.dumps(
        {
            "event_id": "evt_upcase",
            "event_type": "SUBSCRIPTION_STARTED",
            "customer_user_id": user.id,
            "profile": {"access_level": "pro", "is_active": True},
        }
    ).encode()
    resp = await _post(client, body)
    assert resp.status_code == 200
    assert resp.json()["status"] == "applied"


async def test_customer_user_id_from_profile(client, session):
    """customer_user_id из profile.customer_user_id (когда top-level нет)."""
    user = await _user(session, "u_wh_cuidprof0000001")
    body = json.dumps(
        {
            "event_id": "evt_cuid_prof",
            "event_type": "subscription_started",
            "profile": {"customer_user_id": user.id, "access_level": "pro", "is_active": True},
        }
    ).encode()
    resp = await _post(client, body)
    assert resp.status_code == 200
    assert resp.json()["status"] == "applied"


async def test_customer_user_id_from_user_id_field(client, session):
    """customer_user_id из top-level user_id (третий источник)."""
    user = await _user(session, "u_wh_cuiduid00000001")
    body = json.dumps(
        {
            "event_id": "evt_cuid_uid",
            "event_type": "subscription_started",
            "user_id": user.id,
            "profile": {"access_level": "pro", "is_active": True},
        }
    ).encode()
    resp = await _post(client, body)
    assert resp.status_code == 200
    assert resp.json()["status"] == "applied"


async def test_non_dict_profile_does_not_crash(client, session):
    """profile не-dict (строка) → не 5xx; customer_user_id не извлечётся → ignored 200."""
    body = json.dumps(
        {
            "event_id": "evt_baddict",
            "event_type": "subscription_started",
            "profile": "not-a-dict",
        }
    ).encode()
    resp = await _post(client, body)
    assert resp.status_code == 200
    assert resp.json()["reason"] == "missing_customer_user_id"


async def test_non_dict_event_properties_does_not_crash(client, session):
    """event_properties не-dict → vendor_product_id не извлечётся (fallback GRANT), не 5xx."""
    user = await _user(session, "u_wh_badprops0000001")
    settings = get_settings()
    body = json.dumps(
        {
            "event_id": "evt_badprops",
            "event_type": "subscription_started",
            "customer_user_id": user.id,
            "profile": {"access_level": "pro", "is_active": True},
            "event_properties": "not-a-dict",
        }
    ).encode()
    resp = await _post(client, body)
    assert resp.status_code == 200
    assert resp.json()["status"] == "applied"
    # vendor_product_id не найден → fallback GRANT.
    assert await _balance(session, user.id) == settings.subscription_tokens_grant


# ====================== (d) Маппинг событий → subscriptions ======================


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
    renew = await _post(client, _body("evt_g2", "subscription_renewed", user.id))
    assert renew.status_code == 200
    sub = (
        await session.execute(select(Subscription).where(Subscription.user_id == user.id))
    ).scalar_one()
    assert sub.status == "active"
    assert sub.grace_until is None


# ====================== (d) Token-grant по тиру (ADR-027 §E) ======================


async def test_token_grant_weekly(client, session):
    user = await _user(session, "u_wh_grweekly0000001")
    settings = get_settings()
    body = _body(
        "evt_grant_w",
        "subscription_started",
        user.id,
        event_properties={"vendor_product_id": settings.subscription_product_weekly},
    )
    resp = await _post(client, body)
    assert resp.status_code == 200
    assert resp.json()["status"] == "applied"
    # bonus_generations_balance += SUBSCRIPTION_TOKENS_WEEKLY.
    assert await _balance(session, user.id) == settings.subscription_tokens_weekly
    # credit_grants(created_by='adapty', idempotency_key=event_id).
    grant = (
        await session.execute(
            select(CreditGrant).where(CreditGrant.idempotency_key == "evt_grant_w")
        )
    ).scalar_one()
    assert grant.created_by == "adapty"
    assert grant.amount == settings.subscription_tokens_weekly
    assert grant.user_id == user.id


async def test_token_grant_yearly(client, session):
    user = await _user(session, "u_wh_gryearly0000001")
    settings = get_settings()
    body = _body(
        "evt_grant_y",
        "subscription_started",
        user.id,
        event_properties={"vendor_product_id": settings.subscription_product_yearly},
    )
    resp = await _post(client, body)
    assert resp.json()["status"] == "applied"
    assert await _balance(session, user.id) == settings.subscription_tokens_yearly


async def test_token_grant_unknown_product_fallback(client, session):
    user = await _user(session, "u_wh_grunknown000001")
    settings = get_settings()
    body = _body(
        "evt_grant_u",
        "subscription_started",
        user.id,
        event_properties={"vendor_product_id": "com.unknown.sku.999"},
    )
    resp = await _post(client, body)
    assert resp.json()["status"] == "applied"
    # Неизвестный SKU → fallback SUBSCRIPTION_TOKENS_GRANT.
    assert await _balance(session, user.id) == settings.subscription_tokens_grant


async def test_renewed_also_grants_tokens(client, session):
    user = await _user(session, "u_wh_renewgrant00001")
    settings = get_settings()
    body = _body(
        "evt_renew_grant",
        "subscription_renewed",
        user.id,
        event_properties={"vendor_product_id": settings.subscription_product_weekly},
    )
    resp = await _post(client, body)
    assert resp.json()["status"] == "applied"
    assert await _balance(session, user.id) == settings.subscription_tokens_weekly


async def test_vendor_product_id_from_top_level_product_id(client, session):
    """vendor_product_id из top-level product_id (4-й источник дефенсив-цепочки)."""
    user = await _user(session, "u_wh_vpidtop00000001")
    settings = get_settings()
    body = json.dumps(
        {
            "event_id": "evt_vpid_top",
            "event_type": "subscription_started",
            "customer_user_id": user.id,
            "profile": {"access_level": "pro", "is_active": True},
            "product_id": settings.subscription_product_yearly,
        }
    ).encode()
    resp = await _post(client, body)
    assert resp.json()["status"] == "applied"
    assert await _balance(session, user.id) == settings.subscription_tokens_yearly


# ====================== (e) Идемпотентность (САМОЕ ВАЖНОЕ) ======================


async def test_duplicate_event_id_is_noop_200_no_double_grant(client, session):
    user = await _user(session, "u_wh_dup00000000001")
    settings = get_settings()
    body = _body(
        "evt_dup_1",
        "subscription_started",
        user.id,
        event_properties={"vendor_product_id": settings.subscription_product_weekly},
    )
    r1 = await _post(client, body)
    assert r1.status_code == 200
    assert r1.json()["status"] == "applied"
    balance_after_first = await _balance(session, user.id)
    assert balance_after_first == settings.subscription_tokens_weekly

    # Повтор того же event_id → 200 duplicate, баланс/credit_grants НЕ растут второй раз.
    r2 = await _post(client, body)
    assert r2.status_code == 200
    assert r2.json() == {"status": "duplicate"}

    assert await _balance(session, user.id) == balance_after_first  # не выросло
    ev_count = await session.scalar(
        select(func.count())
        .select_from(BillingEvent)
        .where(BillingEvent.adapty_event_id == "evt_dup_1")
    )
    assert ev_count == 1
    grant_count = await session.scalar(
        select(func.count())
        .select_from(CreditGrant)
        .where(CreditGrant.idempotency_key == "evt_dup_1")
    )
    assert grant_count == 1  # второй credit_grants НЕ создан
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


# ====================== cancelled (ADR-027 §F) ======================


async def test_cancelled_sets_will_renew_false_keeps_access(client, session):
    user = await _user(session, "u_wh_cancel000000001")
    settings = get_settings()
    # Старт: active, will_renew=True, начислены токены.
    await _post(
        client,
        _body(
            "evt_c0",
            "subscription_started",
            user.id,
            subscription={"will_renew": True},
            event_properties={"vendor_product_id": settings.subscription_product_weekly},
        ),
    )
    sub_before = (
        await session.execute(select(Subscription).where(Subscription.user_id == user.id))
    ).scalar_one()
    status_before = sub_before.status
    access_before = sub_before.access_level
    grace_before = sub_before.grace_until
    balance_before = await _balance(session, user.id)

    resp = await _post(client, _body("evt_c1", "subscription_cancelled", user.id))
    assert resp.status_code == 200

    await session.refresh(sub_before)
    sub_after = (
        await session.execute(select(Subscription).where(Subscription.user_id == user.id))
    ).scalar_one()
    assert sub_after.will_renew is False  # подписка не продлится
    assert sub_after.status == status_before  # status НЕ меняется
    assert sub_after.access_level == access_before  # access НЕ меняется
    assert sub_after.grace_until == grace_before  # grace НЕ меняется
    # Токены НЕ начисляются на cancelled (не в TOKEN_GRANT_EVENT_TYPES).
    assert await _balance(session, user.id) == balance_before
    grant_count = await session.scalar(
        select(func.count()).select_from(CreditGrant).where(CreditGrant.idempotency_key == "evt_c1")
    )
    assert grant_count == 0


# ====================== 5xx ТОЛЬКО на реальный сбой БД ======================


async def _client_5xx(session):  # noqa: ANN001, ANN202
    """ASGI-клиент, транслирующий unhandled-исключения в HTTP-5xx (raise_app_exceptions=False)."""
    import httpx

    from app.api.main import app
    from app.db.session import get_session

    async def _override():  # noqa: ANN202
        yield session

    app.dependency_overrides[get_session] = _override
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_db_commit_failure_returns_5xx_event_unprocessed(session, monkeypatch):
    """Реальный сбой БД при apply → 5xx (WebhookProcessingError), processed_at IS NULL."""
    user = await _user(session, "u_wh_err000000000001")

    import app.billing.webhook_handler as wh
    from app.api.main import app
    from app.db.session import get_session

    async def _boom(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise RuntimeError("db commit failed")

    monkeypatch.setattr(wh.subscription_state, "apply_webhook_event", _boom)

    client = await _client_5xx(session)
    try:
        body = _body("evt_err_1", "subscription_started", user.id)
        hdrs = {"Content-Type": "application/json", **_auth()}
        resp = await client.post("/v1/billing/webhook/adapty", content=body, headers=hdrs)
        assert resp.status_code >= 500  # Adapty повторит доставку
    finally:
        await client.aclose()
        app.dependency_overrides.pop(get_session, None)


async def test_bad_payload_never_5xx(client, session):
    """Кривой payload (все варианты) НИКОГДА не 5xx — даже без БД-моков."""
    for raw in [b"", b"{bad", b"[1,2]", b"42"]:
        resp = await _post(client, raw)
        assert resp.status_code == 200, f"payload {raw!r} -> {resp.status_code}"
