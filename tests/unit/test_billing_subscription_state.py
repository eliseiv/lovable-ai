"""Unit: apply_webhook_event на ФАКТИЧЕСКОЙ форме payload (ADR-041 §A/§B/§C/§G, docs §2.3).

Field-extraction из event_properties (НЕТ profile/subscription — ADR-041 §A): expires_at из
subscription_expires_at; will_renew булев; access_level=pro константой для started/renewed (§B);
access_level_updated — по is_active/is_in_grace_period/is_refund. Инвариант preserve-on-missing
(§C): отсутствующее поле сохраняет прежнее значение; на новой строке — событийный дефолт §2.3.
synced_at carve-out (§G): access_level_updated{is_active=false, не refund} НЕ продвигает synced_at;
подтверждающие события и admin/storekit-мутаторы — продвигают.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.billing import subscription_state
from app.billing.adapty_client import AdaptyProfile
from app.core.config import get_settings
from app.core.ids import new_subscription_id
from app.db.models import Subscription, User
from tests.support import adapty_payloads as ap

# asyncio_mode=auto (pyproject) — async-тесты запускаются автоматически; файл смешивает
# sync apply_profile_resync-проверки и async apply_webhook_event, без module-level asyncio-mark.

_GRACE_DAYS = get_settings().grace_period_days
_OLD = datetime(2020, 1, 1, tzinfo=UTC)


async def _user(session, uid: str = "u_substate0000000001") -> User:  # noqa: ANN001
    user = User(id=uid, api_key_hash=None, monthly_budget_usd=Decimal("50.0000"), status="active")
    session.add(user)
    await session.flush()
    return user


async def _apply(session, user_id, event_type, **props):  # noqa: ANN001, ANN003
    """apply_webhook_event с event_properties из билдера официальной формы (ADR-041 §A)."""
    event_properties = ap.make_event_properties(**props)
    raw_payload = ap.make_webhook_payload(
        event_type=event_type, customer_user_id=user_id, event_properties=event_properties
    )
    sub = await subscription_state.apply_webhook_event(
        session,
        user_id=user_id,
        event_type=event_type,
        event_properties=event_properties,
        raw_payload=raw_payload,
    )
    # flush делает строку видимой для get_subscription следующего _apply (autoflush=False).
    await session.flush()
    return sub


async def _seed_sub(session, user_id, **kw) -> Subscription:  # noqa: ANN001, ANN003
    """Существующая строка подписки с известными полями (для preserve/carve-out тестов)."""
    sub = Subscription(
        id=new_subscription_id(),
        user_id=user_id,
        access_level=kw.get("access_level", "pro"),
        status=kw.get("status", "active"),
        will_renew=kw.get("will_renew", True),
        expires_at=kw.get("expires_at"),
        grace_until=kw.get("grace_until"),
        synced_at=kw.get("synced_at", _OLD),
        raw={},
    )
    session.add(sub)
    await session.flush()
    return sub


# ==================== started/renewed → pro + expires из event_properties ====================


@pytest.mark.parametrize("event_type", ["subscription_started", "subscription_renewed"])
async def test_started_renewed_set_pro_active(session, event_type):
    user = await _user(session)
    sub = await _apply(
        session,
        user.id,
        event_type,
        subscription_expires_at="2026-07-02T00:00:00Z",
        will_renew=True,
    )
    assert sub.status == subscription_state.STATUS_ACTIVE
    assert sub.access_level == "pro"  # константа §B — из payload не читается
    assert sub.grace_until is None
    assert sub.will_renew is True
    assert sub.expires_at == datetime(2026, 7, 2, tzinfo=UTC)


# ==================== (9) c3: новая строка без subscription_expires_at ====================


async def test_started_new_row_no_expires_over_grant_null(session):
    user = await _user(session)
    sub = await _apply(session, user.id, "subscription_started")  # без subscription_expires_at
    assert sub.access_level == "pro"
    assert sub.status == subscription_state.STATUS_ACTIVE
    assert sub.will_renew is True  # событийный дефолт §2.3
    assert sub.expires_at is None  # over-grant, не синтетический срок (§C)


# ==================== (8) preserve-on-missing на существующей строке ====================


async def test_partial_renewed_preserves_expires(session):
    user = await _user(session)
    await _seed_sub(session, user.id, expires_at=datetime(2026, 9, 1, tzinfo=UTC), will_renew=True)
    # renewed БЕЗ subscription_expires_at → expires_at preserve (не обнулён).
    sub = await _apply(session, user.id, "subscription_renewed")
    assert sub.access_level == "pro"
    assert sub.expires_at == datetime(2026, 9, 1, tzinfo=UTC)


async def test_access_level_updated_active_preserves_expires_and_will_renew(session):
    user = await _user(session)
    await _seed_sub(session, user.id, expires_at=datetime(2026, 9, 1, tzinfo=UTC), will_renew=True)
    # is_active=true БЕЗ expires_at/will_renew → preserve обоих.
    sub = await _apply(session, user.id, "access_level_updated", is_active=True)
    assert sub.access_level == "pro"
    assert sub.expires_at == datetime(2026, 9, 1, tzinfo=UTC)
    assert sub.will_renew is True


async def test_billing_issue_preserves_access_and_expires(session):
    user = await _user(session)
    await _seed_sub(session, user.id, expires_at=datetime(2026, 9, 1, tzinfo=UTC), will_renew=True)
    sub = await _apply(session, user.id, "billing_issue_detected")
    assert sub.status == subscription_state.STATUS_BILLING_ISSUE
    assert sub.access_level == "pro"  # НЕ понижен
    assert sub.expires_at == datetime(2026, 9, 1, tzinfo=UTC)  # НЕ обнулён
    assert sub.will_renew is True  # НЕ форсирован в false


# ==================== (13) §B access_level_updated семантика ====================


async def test_alu_active_true_pro_active(session):
    user = await _user(session)
    sub = await _apply(session, user.id, "access_level_updated", is_active=True)
    assert sub.access_level == "pro"
    assert sub.status == subscription_state.STATUS_ACTIVE


async def test_alu_active_true_grace_flag(session):
    user = await _user(session)
    sub = await _apply(
        session, user.id, "access_level_updated", is_active=True, is_in_grace_period=True
    )
    assert sub.access_level == "pro"
    assert sub.status == subscription_state.STATUS_GRACE


async def test_alu_refund_grace_keeps_access(session):
    user = await _user(session)
    await _seed_sub(session, user.id, access_level="pro", status="active")
    before = datetime.now(UTC)
    sub = await _apply(session, user.id, "access_level_updated", is_active=False, is_refund=True)
    after = datetime.now(UTC)
    assert sub.status == subscription_state.STATUS_GRACE
    assert sub.access_level == "pro"  # НЕ понижен
    assert (
        before + timedelta(days=_GRACE_DAYS)
        <= sub.grace_until
        <= after + timedelta(days=_GRACE_DAYS)
    )


async def test_alu_inactive_not_forced_expired_access_kept(session):
    user = await _user(session)
    await _seed_sub(session, user.id, access_level="pro", status="active")
    sub = await _apply(session, user.id, "access_level_updated", is_active=False)
    assert sub.status != subscription_state.STATUS_EXPIRED
    assert sub.status == subscription_state.STATUS_ACTIVE  # прежний
    assert sub.access_level == "pro"  # НЕ затёрт в free


# ==================== (14) expired grace_until ====================


async def test_expired_without_field_now_plus_grace(session):
    user = await _user(session)
    await _seed_sub(session, user.id)
    before = datetime.now(UTC)
    sub = await _apply(session, user.id, "subscription_expired")
    after = datetime.now(UTC)
    assert sub.status == subscription_state.STATUS_GRACE
    assert (
        before + timedelta(days=_GRACE_DAYS)
        <= sub.grace_until
        <= after + timedelta(days=_GRACE_DAYS)
    )
    assert sub.will_renew is False


async def test_expired_with_field_expires_plus_grace(session):
    user = await _user(session)
    await _seed_sub(session, user.id)
    sub = await _apply(
        session, user.id, "subscription_expired", subscription_expires_at="2026-06-10T00:00:00Z"
    )
    assert sub.grace_until == datetime(2026, 6, 10, tzinfo=UTC) + timedelta(days=_GRACE_DAYS)
    assert sub.access_level == "pro"  # preserve в grace


# ==================== (15) renewal_cancelled → только will_renew=false ====================


async def test_renewal_cancelled_only_will_renew_false(session):
    user = await _user(session)
    await _seed_sub(
        session,
        user.id,
        status="active",
        access_level="pro",
        will_renew=True,
        grace_until=None,
    )
    sub = await _apply(session, user.id, "subscription_renewal_cancelled")
    assert sub.will_renew is False
    assert sub.status == subscription_state.STATUS_ACTIVE  # teardown НЕ происходит
    assert sub.access_level == "pro"


# ==================== (11) §G synced_at carve-out ====================


CONFIRMING_EVENTS = [
    ("subscription_started", {}),
    ("subscription_renewed", {}),
    ("subscription_expired", {}),
    ("subscription_refunded", {}),
    ("billing_issue_detected", {}),
    ("subscription_renewal_cancelled", {}),
    ("access_level_updated", {"is_active": True}),
    ("access_level_updated", {"is_refund": True, "is_active": False}),
]


@pytest.mark.parametrize("event_type,props", CONFIRMING_EVENTS)
async def test_confirming_events_advance_synced_at(session, event_type, props):
    """Подтверждающие события продвигают synced_at=now() (приоритет вебхука над resync)."""
    user = await _user(session, f"u_syncadv_{abs(hash((event_type, tuple(props)))) % 10**7:07d}")
    await _seed_sub(session, user.id, synced_at=_OLD)
    before = datetime.now(UTC)
    sub = await _apply(session, user.id, event_type, **props)
    assert sub.synced_at >= before
    assert sub.synced_at != _OLD


async def test_carveout_inactive_does_not_advance_synced_at(session):
    """access_level_updated{is_active=false, не refund} → synced_at НЕ продвинут (§G)."""
    user = await _user(session, "u_carveout00000001")
    await _seed_sub(session, user.id, synced_at=_OLD)
    sub = await _apply(session, user.id, "access_level_updated", is_active=False)
    assert sub.synced_at == _OLD  # прежнее значение — carve-out §G


# ==================== (12) admin/storekit-мутаторы бампают synced_at ====================


async def test_admin_grant_bumps_synced_at(session):
    user = await _user(session, "u_admin_bump000001")
    await _seed_sub(session, user.id, synced_at=_OLD)
    before = datetime.now(UTC)
    sub = await subscription_state.apply_admin_grant(session, user_id=user.id, expires_at=None)
    assert sub.access_level == "pro"
    assert sub.synced_at >= before  # асимметрия §G осознанна: admin-grant защищается бампом


async def test_storekit_subscription_bumps_synced_at(session):
    user = await _user(session, "u_storekit_bump0001")
    await _seed_sub(session, user.id, synced_at=_OLD)
    before = datetime.now(UTC)
    sub = await subscription_state.apply_storekit_subscription(
        session,
        user_id=user.id,
        expires_at=datetime(2026, 12, 1, tzinfo=UTC),
        environment="Production",
        transaction_id="txn-1",
        original_transaction_id="otxn-1",
        product_id="lovable.pro.weekly",
    )
    assert sub.access_level == "pro"
    assert sub.synced_at >= before


# ==================== apply_profile_resync (getProfile, docs §3) — регресс ====================


def _profile(*, access_level="pro", is_active=True, **kw):  # noqa: ANN001, ANN003
    return AdaptyProfile(
        access_level=access_level,
        is_active=is_active,
        product_id=kw.get("product_id"),
        store=kw.get("store"),
        expires_at=kw.get("expires_at"),
        started_at=kw.get("started_at"),
        will_renew=kw.get("will_renew", False),
        transaction_id=kw.get("transaction_id"),
        raw=kw.get("raw", {}),
    )


def _sub(status: str, *, access_level="pro", grace_until=None):  # noqa: ANN001
    return Subscription(
        id="s_x",
        user_id="u_x",
        access_level=access_level,
        status=status,
        will_renew=False,
        grace_until=grace_until,
        raw={},
    )


def test_resync_active_profile_sets_active_grace_null():
    sub = _sub(subscription_state.STATUS_GRACE, grace_until=datetime.now(UTC))
    subscription_state.apply_profile_resync(sub, _profile(is_active=True, access_level="pro"))
    assert sub.status == subscription_state.STATUS_ACTIVE
    assert sub.grace_until is None
    assert sub.access_level == "pro"


def test_resync_inactive_profile_outside_grace_sets_expired():
    sub = _sub(subscription_state.STATUS_ACTIVE)
    subscription_state.apply_profile_resync(sub, _profile(is_active=False))
    assert sub.status == subscription_state.STATUS_EXPIRED


def test_resync_does_not_force_grace_to_expired():
    grace_until = datetime.now(UTC) + timedelta(days=3)
    sub = _sub(subscription_state.STATUS_GRACE, grace_until=grace_until)
    subscription_state.apply_profile_resync(sub, _profile(is_active=False))
    assert sub.status == subscription_state.STATUS_GRACE
    assert sub.grace_until == grace_until
