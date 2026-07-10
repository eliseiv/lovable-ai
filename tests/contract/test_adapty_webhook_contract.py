"""Contract: вебхук Adapty на ОФИЦИАЛЬНОЙ форме payload → subscriptions/billing_events.

docs/06-testing-strategy §Contract (Adapty webhook, ADR-027 + ADR-040 + ADR-041), billing/02 §1.
Root cause прод-инцидента nexoraweb.shop (2026-07-10): self-consistent payload по нашей схеме
(верхнеуровневый `event_id`, объекты `profile`/`subscription`) скрыл ДВА дефекта денежного пути
(ключ дедупа + field-extraction). Нормативно (ADR-040/041 §F): contract-фикстуры строятся ОТ
официального образца Adapty (profile_event_id/subscription_expires_at/will_renew/is_active внутри
event_properties, БЕЗ profile/subscription) — см. tests/support/adapty_payloads.py.

Здесь — семантика payload→state на уровне process_webhook (Bearer/HTTP — integration-файл).
Покрывает: §A field-extraction (§C3), §B access_level_updated-семантика, §C preserve-on-missing +
новая строка, §G synced_at carve-out, §C KNOWN_EVENT_TYPES=18 / consciously-ignored / unknown.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.billing import subscription_state
from app.billing.webhook_handler import WebhookOutcome, process_webhook
from app.core.config import get_settings
from app.db.models import BillingEvent, Subscription, User
from tests.support import adapty_payloads as ap

# asyncio_mode=auto (pyproject) — async-тесты запускаются автоматически; файл смешивает
# sync-проверки (форма фикстуры/множества типов) и async, поэтому без module-level asyncio-mark.

_GRACE_DAYS = get_settings().grace_period_days


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


async def _process(session, payload: dict):  # noqa: ANN001
    """process_webhook с raw_body из payload (как читает роутер)."""
    return await process_webhook(session, payload, ap.to_body(payload))


async def _sub(session, uid: str) -> Subscription:  # noqa: ANN001
    return (
        await session.execute(select(Subscription).where(Subscription.user_id == uid))
    ).scalar_one()


# ============ Фикстура официальной формы: гард против рецидива self-consistent ============


def test_official_fixture_has_no_forbidden_top_level_keys():
    """Билдер даёт официальную форму: НЕТ event_id/id/profile/subscription (root cause guard)."""
    p = ap.subscription_event("subscription_started", "u_x", subscription_expires_at="2026-08-01Z")
    assert "event_id" not in p and "id" not in p
    assert "profile" not in p and "subscription" not in p
    # profile_event_id — внутри event_properties (не на верхнем уровне).
    assert "profile_event_id" in p["event_properties"]
    assert "profile_event_id" not in p
    ap.assert_official_shape(p)


def test_assert_official_shape_rejects_legacy_schema():
    """Гард ловит легаси-схему (event_id/profile) — будущие тесты не соскользнут на неё."""
    for bad in ({"event_id": "x"}, {"id": "x"}, {"profile": {}}, {"subscription": {}}):
        with pytest.raises(AssertionError):
            ap.assert_official_shape(bad)


# ==================== (1) Регресс инцидента: payload не отбрасывается ====================


async def test_official_payload_processed_dedup_key_is_profile_event_id(session):
    """Payload официальной формы (profile_event_id в event_properties, БЕЗ event_id) обрабатывается;
    billing_events.adapty_event_id = этот UUID как есть (не missing_event_id, не syn-fallback)."""
    user = await _user(session, "u_ct_peid00000000001")
    peid = ap.new_profile_event_id()
    payload = ap.subscription_event(
        "subscription_started",
        user.id,
        profile_event_id=peid,
        subscription_expires_at="2026-08-01T00:00:00Z",
    )
    result = await _process(session, payload)
    assert result.outcome == WebhookOutcome.APPLIED

    ev = (
        await session.execute(select(BillingEvent).where(BillingEvent.adapty_event_id == peid))
    ).scalar_one()
    assert ev.user_id == user.id
    assert ev.event_type == "subscription_started"
    assert ev.processed_at is not None
    # Ключ = profile_event_id как есть, без синтетического префикса.
    assert ev.adapty_event_id == peid
    assert not ev.adapty_event_id.startswith("adapty-syn:")


# ============ (2) subscription_started оплатившего pro-юзера → pro (не free) ============


async def test_started_sets_pro_and_expires_from_event_properties(session):
    """Регресс раздельного выката ADR-040 без ADR-041: НЕ пишет access_level='free'.
    access_level=pro (константа §B), expires_at из event_properties.subscription_expires_at."""
    user = await _user(session, "u_ct_pro000000000001")
    payload = ap.subscription_event(
        "subscription_started",
        user.id,
        subscription_expires_at="2026-08-01T00:00:00Z",
        will_renew=True,
    )
    await _process(session, payload)
    sub = await _sub(session, user.id)
    assert sub.access_level == "pro"
    assert sub.status == "active"
    assert sub.will_renew is True
    assert sub.expires_at == datetime(2026, 8, 1, tzinfo=UTC)
    assert sub.grace_until is None


# ==================== (8) preserve-on-missing на существующей строке ====================


async def test_partial_payload_preserves_existing_pro_row(session):
    """Частичный payload (без subscription_expires_at/will_renew) на СУЩЕСТВУЮЩЕЙ pro-строке →
    прежние access_level/expires_at/will_renew СОХРАНЕНЫ (§C), не обнулены."""
    user = await _user(session, "u_ct_preserve0000001")
    # Первичное авторитетное событие: pro, expires_at, will_renew=True.
    await _process(
        session,
        ap.subscription_event(
            "subscription_started",
            user.id,
            subscription_expires_at="2026-09-01T00:00:00Z",
            will_renew=True,
        ),
    )
    sub = await _sub(session, user.id)
    assert sub.expires_at == datetime(2026, 9, 1, tzinfo=UTC)
    assert sub.will_renew is True

    # access_level_updated{is_active=true} БЕЗ expires_at/will_renew → preserve.
    await _process(session, ap.access_level_updated_event(user.id, is_active=True))
    sub2 = await _sub(session, user.id)
    assert sub2.access_level == "pro"  # не понижен
    assert sub2.expires_at == datetime(2026, 9, 1, tzinfo=UTC)  # сохранён
    assert sub2.will_renew is True  # сохранён


async def test_missing_field_never_downgrades_paying_user(session):
    """(10) Ни одна ветка не понижает права платящего из-за отсутствующего поля."""
    user = await _user(session, "u_ct_nodowngrade0001")
    await _process(
        session,
        ap.subscription_event(
            "subscription_started",
            user.id,
            subscription_expires_at="2026-09-01T00:00:00Z",
            will_renew=True,
        ),
    )
    # billing_issue без will_renew/expires_at → access_level/expires_at preserve, только status.
    await _process(session, ap.subscription_event("billing_issue_detected", user.id))
    sub = await _sub(session, user.id)
    assert sub.access_level == "pro"  # НЕ понижен
    assert sub.expires_at == datetime(2026, 9, 1, tzinfo=UTC)  # НЕ обнулён
    assert sub.will_renew is True  # НЕ форсирован в false
    assert sub.status == subscription_state.STATUS_BILLING_ISSUE


# ==================== (9) c3: новая строка без subscription_expires_at ====================


async def test_started_new_row_without_expires_over_grants_pro_null_expires(session):
    """c3: subscription_started без subscription_expires_at на ВПЕРВЫЕ создаваемой строке →
    pro/active/will_renew=true (событийный дефолт)/expires_at=NULL (осознанный over-grant, §C)."""
    user = await _user(session, "u_ct_newrow000000001")
    payload = ap.subscription_event("subscription_started", user.id)  # без subscription_expires_at
    await _process(session, payload)
    sub = await _sub(session, user.id)
    assert sub.access_level == "pro"
    assert sub.status == "active"
    assert sub.will_renew is True
    assert sub.expires_at is None  # over-grant, не синтетический срок


# ==================== (13) §B access_level_updated семантика ====================


async def test_access_level_updated_is_active_true_sets_pro_active(session):
    user = await _user(session, "u_ct_alu_active00001")
    await _process(session, ap.access_level_updated_event(user.id, is_active=True))
    sub = await _sub(session, user.id)
    assert sub.access_level == "pro"
    assert sub.status == subscription_state.STATUS_ACTIVE


async def test_access_level_updated_is_active_true_grace_when_in_grace(session):
    user = await _user(session, "u_ct_alu_grace000001")
    await _process(
        session, ap.access_level_updated_event(user.id, is_active=True, is_in_grace_period=True)
    )
    sub = await _sub(session, user.id)
    assert sub.access_level == "pro"
    assert sub.status == subscription_state.STATUS_GRACE


async def test_access_level_updated_refund_grace_keeps_access(session):
    """{is_refund=true} → grace + grace_until; access_level НЕ понижен (§B/§C)."""
    user = await _user(session, "u_ct_alu_refund00001")
    # Существующая pro-строка.
    await _process(session, ap.subscription_event("subscription_started", user.id, will_renew=True))
    before = datetime.now(UTC)
    await _process(session, ap.access_level_updated_event(user.id, is_active=False, is_refund=True))
    after = datetime.now(UTC)
    sub = await _sub(session, user.id)
    assert sub.status == subscription_state.STATUS_GRACE
    assert sub.access_level == "pro"  # НЕ понижен
    assert (
        before + timedelta(days=_GRACE_DAYS)
        <= sub.grace_until
        <= after + timedelta(days=_GRACE_DAYS)
    )


async def test_access_level_updated_is_active_false_not_forced_expired(session):
    """{is_active=false, не refund} → status НЕ форсируется в expired, access_level НЕ понижен."""
    user = await _user(session, "u_ct_alu_inactive001")
    await _process(
        session,
        ap.subscription_event(
            "subscription_started", user.id, subscription_expires_at="2026-09-01T00:00:00Z"
        ),
    )
    await _process(session, ap.access_level_updated_event(user.id, is_active=False))
    sub = await _sub(session, user.id)
    assert sub.status != subscription_state.STATUS_EXPIRED
    assert (
        sub.status == subscription_state.STATUS_ACTIVE
    )  # прежний status под управлением lifecycle
    assert sub.access_level == "pro"  # НЕ затёрт в free


# ==================== (14) subscription_expired grace_until расчёт ====================


async def test_expired_without_expires_uses_now_plus_grace(session):
    user = await _user(session, "u_ct_exp_nofield0001")
    await _process(session, ap.subscription_event("subscription_started", user.id))
    before = datetime.now(UTC)
    await _process(session, ap.subscription_event("subscription_expired", user.id))
    after = datetime.now(UTC)
    sub = await _sub(session, user.id)
    assert sub.status == subscription_state.STATUS_GRACE
    assert (
        before + timedelta(days=_GRACE_DAYS)
        <= sub.grace_until
        <= after + timedelta(days=_GRACE_DAYS)
    )
    assert sub.will_renew is False


async def test_expired_with_expires_uses_expires_plus_grace(session):
    user = await _user(session, "u_ct_exp_field000001")
    await _process(session, ap.subscription_event("subscription_started", user.id))
    await _process(
        session,
        ap.subscription_event(
            "subscription_expired", user.id, subscription_expires_at="2026-06-10T00:00:00Z"
        ),
    )
    sub = await _sub(session, user.id)
    assert sub.status == subscription_state.STATUS_GRACE
    assert sub.grace_until == datetime(2026, 6, 10, tzinfo=UTC) + timedelta(days=_GRACE_DAYS)
    assert sub.access_level == "pro"  # сохраняется в grace (проходит гейт)


# ==================== (15) subscription_renewal_cancelled ====================


async def test_renewal_cancelled_only_sets_will_renew_false(session):
    """subscription_renewal_cancelled (имя исправлено ADR-040 §C) → только will_renew=false;
    teardown прав/токенов НЕ происходит; status/access/grace сохранены."""
    user = await _user(session, "u_ct_rcancel0000001")
    await _process(session, ap.subscription_event("subscription_started", user.id, will_renew=True))
    sub_before = await _sub(session, user.id)
    status_b, access_b, grace_b = sub_before.status, sub_before.access_level, sub_before.grace_until
    balance_before = await session.scalar(
        select(User.bonus_generations_balance).where(User.id == user.id)
    )

    result = await _process(
        session, ap.subscription_event("subscription_renewal_cancelled", user.id)
    )
    assert result.outcome == WebhookOutcome.APPLIED
    await session.refresh(sub_before)
    sub_after = await _sub(session, user.id)
    assert sub_after.will_renew is False
    assert sub_after.status == status_b
    assert sub_after.access_level == access_b
    assert sub_after.grace_until == grace_b
    assert (
        await session.scalar(select(User.bonus_generations_balance).where(User.id == user.id))
        == balance_before
    )


async def test_legacy_subscription_cancelled_is_unknown_event(session):
    """Легаси-имя subscription_cancelled БОЛЬШЕ не известно (ADR-040 §C факт №6) → ignored:type."""
    user = await _user(session, "u_ct_legacycancel001")
    # Собираем вручную официальной формы (билдер не ограничивает event_type).
    payload = ap.subscription_event("subscription_cancelled", user.id)
    result = await _process(session, payload)
    assert result.outcome == WebhookOutcome.IGNORED
    assert result.event_type == "subscription_cancelled"


# ==================== (16) Все 18 типов классифицированы; вне 18 → ignored ====================


def test_known_event_types_are_exactly_18():
    """KNOWN_EVENT_TYPES = все 18 фактических типов Adapty (ADR-040 §C, факт №5)."""
    assert frozenset(ap.KNOWN_EVENT_TYPES_18) == subscription_state.KNOWN_EVENT_TYPES
    assert len(subscription_state.KNOWN_EVENT_TYPES) == 18
    # Три непересекающихся множества, объединение = 18.
    h = subscription_state.HANDLED_SUBSCRIPTION_EVENT_TYPES
    c = subscription_state.CONSUMABLE_EVENT_TYPES
    ig = subscription_state.CONSCIOUSLY_IGNORED_EVENT_TYPES
    assert len(h) == 7 and len(c) == 1 and len(ig) == 10
    assert h.isdisjoint(c) and h.isdisjoint(ig) and c.isdisjoint(ig)
    assert (h | c | ig) == subscription_state.KNOWN_EVENT_TYPES


@pytest.mark.parametrize("event_type", sorted(subscription_state.HANDLED_SUBSCRIPTION_EVENT_TYPES))
async def test_handled_types_not_unknown_event(session, event_type):
    """Ни один из 7 handled-типов не попадает в ветку unknown_event."""
    user = await _user(session, f"u_ct_h_{abs(hash(event_type)) % 10**8:08d}")
    result = await _process(session, ap.subscription_event(event_type, user.id))
    # handled → APPLIED (не IGNORED:unknown).
    assert result.outcome == WebhookOutcome.APPLIED


@pytest.mark.parametrize("event_type", sorted(subscription_state.CONSCIOUSLY_IGNORED_EVENT_TYPES))
async def test_consciously_ignored_persist_no_state(session, event_type):
    """CONSCIOUSLY_IGNORED (10): 200 ignored:type, billing_events(processed_at=NULL),
    subscriptions/токены НЕ трогаются."""
    user = await _user(session, f"u_ct_ci_{abs(hash(event_type)) % 10**8:08d}")
    peid = ap.new_profile_event_id()
    payload = ap.subscription_event(event_type, user.id, profile_event_id=peid)
    r1 = await _process(session, payload)
    assert r1.outcome == WebhookOutcome.IGNORED
    assert r1.event_type == event_type

    ev = (
        await session.execute(select(BillingEvent).where(BillingEvent.adapty_event_id == peid))
    ).scalar_one()
    assert ev.processed_at is None  # персистится, но не обработано
    assert ev.user_id is None
    # subscriptions не создана.
    sub_count = await session.scalar(
        select(func.count()).select_from(Subscription).where(Subscription.user_id == user.id)
    )
    assert sub_count == 0
    # Права/токены не трогаются.
    assert (
        await session.scalar(select(User.bonus_generations_balance).where(User.id == user.id)) == 0
    )


async def test_consciously_ignored_redelivery_idempotent(session):
    """Повторная доставка consciously-ignored события идемпотентна: IntegrityError по UNIQUE
    adapty_event_id обрабатывается gracefully (200 ignored, НЕ 5xx/исключение).

    В shared-savepoint изоляции второй commit→rollback откатывает и первую строку — поэтому
    проверяем только gracefully-исход второй доставки (в проде это отдельные транзакции; UNIQUE
    гарантирует единственность строки)."""
    user = await _user(session, "u_ct_ci_redeliver01")
    peid = ap.new_profile_event_id()
    payload = ap.subscription_event("trial_started", user.id, profile_event_id=peid)
    r1 = await _process(session, payload)
    assert r1.outcome == WebhookOutcome.IGNORED
    # Вторая доставка того же события: IntegrityError ловится → IGNORED без исключения.
    r2 = await _process(session, payload)
    assert r2.outcome == WebhookOutcome.IGNORED


async def test_unknown_type_outside_18_ignored_not_persisted(session):
    """Тип вне 18 → 200 ignored:type + WARN, НЕ персистится (проверяется ДО резолюции ключа)."""
    user = await _user(session, "u_ct_unk000000000001")
    peid = ap.new_profile_event_id()
    payload = ap.subscription_event("some_brand_new_adapty_type", user.id, profile_event_id=peid)
    result = await _process(session, payload)
    assert result.outcome == WebhookOutcome.IGNORED
    assert result.event_type == "some_brand_new_adapty_type"
    # НЕ персистится.
    ev_count = await session.scalar(
        select(func.count()).select_from(BillingEvent).where(BillingEvent.adapty_event_id == peid)
    )
    assert ev_count == 0


# ==================== (20) customer_user_id — верхний уровень, без profile.* ====================


async def test_customer_user_id_from_top_level(session):
    user = await _user(session, "u_ct_cuidtop00000001")
    payload = ap.subscription_event("subscription_started", user.id)
    assert "customer_user_id" in payload  # верхний уровень
    result = await _process(session, payload)
    assert result.outcome == WebhookOutcome.APPLIED


async def test_customer_user_id_legacy_user_id_fallback(session):
    """Legacy-fallback на верхнеуровневый user_id сохранён (ADR-041 §A) — без profile.*."""
    user = await _user(session, "u_ct_cuiduid00000001")
    payload = ap.make_webhook_payload(
        event_type="subscription_started",
        customer_user_id=None,
        omit_customer_user_id=True,
        event_properties=ap.make_event_properties(),
        top_level_extra={"user_id": user.id},
    )
    result = await _process(session, payload)
    assert result.outcome == WebhookOutcome.APPLIED


async def test_missing_customer_user_id_persisted_user_null(session):
    """Нет customer_user_id → billing_events(user_id=NULL, processed_at=NULL), ignored."""
    peid = ap.new_profile_event_id()
    payload = ap.make_webhook_payload(
        event_type="subscription_started",
        customer_user_id=None,
        omit_customer_user_id=True,
        event_properties=ap.make_event_properties(profile_event_id=peid),
    )
    result = await _process(session, payload)
    assert result.outcome == WebhookOutcome.IGNORED
    assert result.reason == "missing_customer_user_id"
    ev = (
        await session.execute(select(BillingEvent).where(BillingEvent.adapty_event_id == peid))
    ).scalar_one()
    assert ev.user_id is None
    assert ev.processed_at is None


# ==================== Response-схема (docs §1) ====================


async def test_response_schema_ignored_carries_reason(session):
    r = await _process(session, [1, 2, 3])  # type: ignore[arg-type]
    assert r.outcome == WebhookOutcome.IGNORED
    assert r.reason == "not_an_object"


async def test_duplicate_outcome_on_replay(session):
    user = await _user(session, "u_ct_dupout000000001")
    payload = ap.subscription_event("subscription_started", user.id)
    r1 = await _process(session, payload)
    r2 = await _process(session, payload)
    assert r1.outcome == WebhookOutcome.APPLIED
    assert r2.outcome == WebhookOutcome.DUPLICATE
