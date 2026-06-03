"""Integration: getProfile-ресинк подписок (ADR-009, docs/billing/03 §3).

- Ресинк не перетирает свежее вебхук-состояние (по synced_at: свежая строка не выбирается).
- fail-open на кэш при недоступности Adapty (мок AdaptyError) — lazy не блокирует.
- lazy-ресинк на гейте при протухшем synced_at тянет getProfile и обновляет кэш.
- периодический ресинк: протухшие + grace/billing_issue выбираются; AdaptyError по одному
  пользователю не валит весь батч (per-user изоляция).

Adapty-граница (httpx getProfile) изолирована моком get_adapty_client / клиента.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.billing import resync, subscription_state
from app.billing.adapty_client import AdaptyError, AdaptyProfile, AdaptyTransientError
from app.core.config import get_settings
from app.core.ids import new_subscription_id
from app.db.models import Subscription, User

pytestmark = pytest.mark.asyncio

_TTL = get_settings().billing_resync_interval_s


def _profile(*, access_level="pro", is_active=True):  # noqa: ANN001
    return AdaptyProfile(
        access_level=access_level,
        is_active=is_active,
        product_id="lovable.pro.monthly",
        store="app_store",
        expires_at="2026-08-01T00:00:00Z",
        started_at="2026-06-01T00:00:00Z",
        will_renew=True,
        transaction_id="tx1",
        raw={"data": {"profile": "ok"}},
    )


class _FakeClient:
    """Мок AdaptyClient: возвращает заранее заданный профиль / поднимает ошибку."""

    def __init__(self, profile=None, error=None):  # noqa: ANN001
        self._profile = profile
        self._error = error
        self.calls: list[str] = []

    async def get_profile(self, customer_user_id):  # noqa: ANN001, ANN202
        self.calls.append(customer_user_id)
        if self._error is not None:
            raise self._error
        return self._profile


async def _user(session, uid: str) -> User:  # noqa: ANN001
    user = User(id=uid, api_key_hash=None, monthly_budget_usd=Decimal("50.0000"), status="active")
    session.add(user)
    await session.flush()
    return user


async def _sub(session, uid, *, status="active", access_level="free", synced_at=None):  # noqa: ANN001
    sub = Subscription(
        id=new_subscription_id(),
        user_id=uid,
        access_level=access_level,
        status=status,
        will_renew=False,
        raw={},
        synced_at=synced_at or datetime.now(UTC),
    )
    session.add(sub)
    await session.flush()
    return sub


# --- Периодический ресинк ---


async def test_periodic_resync_picks_stale_and_updates(session):
    user = await _user(session, "u_rs_stale000000001")
    stale = datetime.now(UTC) - timedelta(seconds=_TTL + 60)
    await _sub(session, user.id, status="active", access_level="free", synced_at=stale)
    client = _FakeClient(profile=_profile(access_level="pro", is_active=True))

    count = await resync.run_periodic_resync(session, client)
    assert count == 1
    assert client.calls == [user.id]
    sub = await subscription_state.get_subscription(session, user.id)
    assert sub.access_level == "pro"
    assert sub.synced_at > stale


async def test_periodic_resync_does_not_overwrite_fresh_webhook_state(session):
    """Свежая (synced_at=now, не grace/billing_issue) строка НЕ выбирается ресинком."""
    user = await _user(session, "u_rs_fresh000000001")
    fresh = datetime.now(UTC)
    await _sub(session, user.id, status="active", access_level="pro", synced_at=fresh)
    client = _FakeClient(profile=_profile(access_level="free", is_active=False))

    count = await resync.run_periodic_resync(session, client)
    # Свежая active-подписка не тронута — getProfile не вызывался для неё.
    assert user.id not in client.calls
    assert count == 0
    sub = await subscription_state.get_subscription(session, user.id)
    assert sub.access_level == "pro"  # вебхук-состояние сохранено


async def test_periodic_resync_picks_grace_even_if_fresh(session):
    """grace/billing_issue выбираются всегда (нужна свежесть перед teardown), даже synced=now."""
    user = await _user(session, "u_rs_grace000000001")
    await _sub(session, user.id, status="grace", access_level="pro", synced_at=datetime.now(UTC))
    client = _FakeClient(profile=_profile(access_level="pro", is_active=True))

    await resync.run_periodic_resync(session, client)
    assert user.id in client.calls


async def test_periodic_resync_transient_error_does_not_break_batch(session):
    """AdaptyTransientError по одному пользователю не валит батч (per-user изоляция)."""
    u1 = await _user(session, "u_rs_ok00000000001")
    u2 = await _user(session, "u_rs_fail0000000001")
    stale = datetime.now(UTC) - timedelta(seconds=_TTL + 60)
    await _sub(session, u1.id, access_level="free", synced_at=stale)
    await _sub(session, u2.id, status="grace", access_level="pro", synced_at=stale)

    class _MixedClient:
        def __init__(self):  # noqa: ANN204
            self.calls = []

        async def get_profile(self, uid):  # noqa: ANN001, ANN202
            self.calls.append(uid)
            if uid == "u_rs_fail0000000001":
                raise AdaptyTransientError("429 from Adapty")
            return _profile(access_level="pro", is_active=True)

    client = _MixedClient()
    count = await resync.run_periodic_resync(session, client)
    # Оба запрошены, успешный применён, упавший не уронил батч.
    assert set(client.calls) == {"u_rs_ok00000000001", "u_rs_fail0000000001"}
    assert count == 1


# --- Lazy-ресинк (гейт / billing/me) ---


async def test_lazy_resync_fresh_does_not_call_adapty(session, monkeypatch):
    user = await _user(session, "u_rs_lazyfresh00001")
    await _sub(session, user.id, access_level="pro", synced_at=datetime.now(UTC))
    client = _FakeClient(profile=_profile())
    monkeypatch.setattr(resync, "get_adapty_client", lambda: client)

    sub = await resync.lazy_resync_if_stale(session, user.id)
    assert sub is not None
    assert client.calls == []  # свежий кэш — Adapty не дёргается


async def test_lazy_resync_stale_calls_adapty_and_updates(session, monkeypatch):
    user = await _user(session, "u_rs_lazystale0001")
    stale = datetime.now(UTC) - timedelta(seconds=_TTL + 60)
    await _sub(session, user.id, access_level="free", synced_at=stale)
    client = _FakeClient(profile=_profile(access_level="pro", is_active=True))
    monkeypatch.setattr(resync, "get_adapty_client", lambda: client)

    sub = await resync.lazy_resync_if_stale(session, user.id)
    assert client.calls == [user.id]
    assert sub.access_level == "pro"


async def test_lazy_resync_fail_open_on_adapty_error(session, monkeypatch):
    """Недоступность Adapty → fail-open на кэш (не блокирует, отдаёт строку как есть)."""
    user = await _user(session, "u_rs_failopen00001")
    stale = datetime.now(UTC) - timedelta(seconds=_TTL + 60)
    await _sub(session, user.id, status="active", access_level="pro", synced_at=stale)
    client = _FakeClient(error=AdaptyError("Adapty unavailable"))
    monkeypatch.setattr(resync, "get_adapty_client", lambda: client)

    sub = await resync.lazy_resync_if_stale(session, user.id)
    # Не упало: вернулась закэшированная строка.
    assert sub is not None
    assert sub.access_level == "pro"
    assert client.calls == [user.id]


async def test_lazy_resync_no_subscription_returns_none_without_adapty(session, monkeypatch):
    user = await _user(session, "u_rs_nosub00000001")
    client = _FakeClient(profile=_profile())
    monkeypatch.setattr(resync, "get_adapty_client", lambda: client)
    sub = await resync.lazy_resync_if_stale(session, user.id)
    assert sub is None
    assert client.calls == []  # нет строки → free-дефолт, Adapty не дёргается на горячем пути
