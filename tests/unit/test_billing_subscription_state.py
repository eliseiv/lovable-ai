"""Unit: маппинг event_type → subscriptions (нормативная таблица docs/billing/03 §2.3).

Чистая логика apply_webhook_event на реальной (in-transaction) сессии: started/renewed→
active+grace_until=NULL; expired→grace+grace_until=expires_at+GRACE; refunded→grace+now+
GRACE; billing_issue_detected→billing_issue; access_level_updated→новый уровень; renew/
start в grace→active+grace_until=NULL (отмена pending-teardown). access_level в grace
сохраняется. apply_profile_resync: активный→active; неактивный вне grace→expired; grace
не форсится в expired.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.billing import subscription_state
from app.billing.adapty_client import AdaptyProfile
from app.core.config import get_settings
from app.db.models import Subscription, User

pytestmark = pytest.mark.asyncio

_GRACE_DAYS = get_settings().grace_period_days


async def _user(session, uid: str = "u_substate0000000000001") -> User:  # noqa: ANN001
    user = User(id=uid, api_key_hash=None, monthly_budget_usd=Decimal("50.0000"), status="active")
    session.add(user)
    await session.flush()
    return user


def _payload(event_type: str, *, access_level="pro", is_active=True, **sub):  # noqa: ANN001, ANN003
    return {
        "event_id": f"evt_{event_type}",
        "event_type": event_type,
        "customer_user_id": "u_substate0000000000001",
        "profile": {"access_level": access_level, "is_active": is_active},
        "subscription": sub,
    }


async def _apply(session, user_id, payload):  # noqa: ANN001
    p = payload
    sub = await subscription_state.apply_webhook_event(
        session,
        user_id=user_id,
        event_type=p["event_type"],
        profile=p.get("profile", {}),
        subscription_payload=p.get("subscription", {}),
        raw_payload=p,
    )
    # В production каждый вебхук — отдельная транзакция (commit). flush делает строку
    # видимой для get_subscription следующего _apply (autoflush=False в тестах), иначе
    # _ensure_row создаст дубль вместо апдейта существующей подписки.
    await session.flush()
    return sub


@pytest.mark.parametrize("event_type", ["subscription_started", "subscription_renewed"])
async def test_started_renewed_set_active_grace_null(session, event_type):
    user = await _user(session)
    expires = "2026-07-02T00:00:00Z"
    sub = await _apply(
        session,
        user.id,
        _payload(
            event_type, expires_at=expires, will_renew=True, started_at="2026-06-02T00:00:00Z"
        ),
    )
    assert sub.status == subscription_state.STATUS_ACTIVE
    assert sub.access_level == "pro"
    assert sub.grace_until is None
    assert sub.will_renew is True
    assert sub.expires_at == datetime(2026, 7, 2, tzinfo=UTC)


async def test_expired_sets_grace_until_expires_plus_grace(session):
    user = await _user(session)
    # В реальном флоу expired приходит на уже-активную pro-подписку.
    await _apply(session, user.id, _payload("subscription_started", access_level="pro"))
    expires = "2026-06-10T00:00:00Z"
    sub = await _apply(session, user.id, _payload("subscription_expired", expires_at=expires))
    assert sub.status == subscription_state.STATUS_GRACE
    # access_level сохраняется (grace проходит гейт, сайты под teardown по grace_until).
    assert sub.access_level == "pro"
    assert sub.grace_until == datetime(2026, 6, 10, tzinfo=UTC) + timedelta(days=_GRACE_DAYS)
    assert sub.will_renew is False


async def test_refunded_sets_grace_until_now_plus_grace(session):
    user = await _user(session)
    before = datetime.now(UTC)
    sub = await _apply(session, user.id, _payload("subscription_refunded"))
    after = datetime.now(UTC)
    assert sub.status == subscription_state.STATUS_GRACE
    assert sub.grace_until is not None
    lo = before + timedelta(days=_GRACE_DAYS)
    hi = after + timedelta(days=_GRACE_DAYS)
    assert lo <= sub.grace_until <= hi
    assert sub.will_renew is False


async def test_billing_issue_sets_billing_issue_status(session):
    user = await _user(session)
    sub = await _apply(session, user.id, _payload("billing_issue_detected"))
    assert sub.status == subscription_state.STATUS_BILLING_ISSUE
    assert sub.grace_until is None
    # На гейте billing_issue НЕ проходит (§4).
    assert sub.status not in subscription_state.GATE_PASS_STATUSES


async def test_access_level_updated_changes_level(session):
    user = await _user(session)
    # Сначала active free.
    await _apply(session, user.id, _payload("subscription_started", access_level="free"))
    sub = await _apply(
        session, user.id, _payload("access_level_updated", access_level="pro", is_active=True)
    )
    assert sub.access_level == "pro"
    assert sub.status == subscription_state.STATUS_ACTIVE


async def test_renew_in_grace_cancels_teardown_grace_null(session):
    user = await _user(session)
    # Активная pro → grace (expired).
    await _apply(session, user.id, _payload("subscription_started", access_level="pro"))
    sub = await _apply(
        session, user.id, _payload("subscription_expired", expires_at="2026-06-10T00:00:00Z")
    )
    assert sub.status == subscription_state.STATUS_GRACE
    assert sub.grace_until is not None
    # Renew в grace → active + grace_until=NULL (pending-teardown отменён, §6).
    sub2 = await _apply(session, user.id, _payload("subscription_renewed", will_renew=True))
    assert sub2.status == subscription_state.STATUS_ACTIVE
    assert sub2.grace_until is None


async def test_start_in_billing_issue_returns_active_grace_null(session):
    user = await _user(session)
    await _apply(session, user.id, _payload("billing_issue_detected"))
    sub = await _apply(session, user.id, _payload("subscription_started", will_renew=True))
    assert sub.status == subscription_state.STATUS_ACTIVE
    assert sub.grace_until is None


async def test_unknown_event_type_does_not_change_status(session):
    user = await _user(session)
    await _apply(session, user.id, _payload("subscription_started"))
    sub = await _apply(session, user.id, _payload("some_future_event"))
    # Status не меняется на неизвестном событии, raw фиксируется.
    assert sub.status == subscription_state.STATUS_ACTIVE
    assert sub.raw["event_type"] == "some_future_event"


# --- apply_profile_resync (getProfile, docs §3) ---


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
    # grace не форсится в expired ресинком (teardown — дело sweep по grace_until).
    assert sub.status == subscription_state.STATUS_GRACE
    assert sub.grace_until == grace_until
