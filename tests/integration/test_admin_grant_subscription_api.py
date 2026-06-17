"""Integration: POST /v1/admin/users/{user_id}/subscription — admin-grant pro (ADR-037).

Источник истины — ADR-037 (§A коды/тело/валидация, §B поля subscriptions, §C бессрочно=NULL,
§D идемпотентность, §F audit) + docs/modules/admin/02-api-contracts.md §3.5.

Реальный Postgres (client/session шарят одну тест-сессию с savepoint-откатом; сервисный
session.commit() завершает SAVEPOINT, не внешнюю транзакцию — данные читаемы через ту же
session, как в test_admin_credits/test_admin_api). plan_quotas (pro: monthly_generations=100,
max_projects=NULL, cap=3) сидированы миграцией 20260602_0004. Защита — X-Admin-Key.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.billing.subscription_state import get_subscription
from app.core.security import hash_api_key
from app.db.models import BillingEvent, CreditGrant, Subscription, User

pytestmark = pytest.mark.asyncio

_SUB_PATH = "/v1/admin/users/{uid}/subscription"


async def _user(session, uid: str, *, balance: int = 0) -> User:  # noqa: ANN001
    user = User(
        id=uid,
        api_key_hash=hash_api_key(f"{uid}-legacy-key"),
        monthly_budget_usd=Decimal("50.0000"),
        status="active",
        bonus_generations_balance=balance,
    )
    session.add(user)
    await session.flush()
    return user


def _grant(client, uid: str, body: dict, headers):  # noqa: ANN001, ANN202
    return client.post(_SUB_PATH.format(uid=uid), json=body, headers=headers)


# ============================ auth (X-Admin-Key) ============================


async def test_grant_subscription_without_key_401_problem_json(client, admin_headers):
    """Без X-Admin-Key → 401 application/problem+json (плоскость защищена)."""
    resp = await client.post(_SUB_PATH.format(uid="u_x"), json={})
    assert resp.status_code == 401
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert resp.json()["title"] == "Unauthorized"


async def test_grant_subscription_invalid_key_401(client):
    """Неверный X-Admin-Key → 401 (без раскрытия причины)."""
    resp = await client.post(_SUB_PATH.format(uid="u_x"), json={}, headers={"X-Admin-Key": "nope"})
    assert resp.status_code == 401


async def test_grant_subscription_disabled_when_key_unconfigured(
    client, admin_headers, monkeypatch
):
    """Пустой ADMIN_API_KEY → даже валидный (прежде) ключ → 401 (плоскость отключена)."""
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "admin_api_key", None, raising=False)
    resp = await client.post(_SUB_PATH.format(uid="u_x"), json={}, headers=admin_headers)
    assert resp.status_code == 401


# ============================ 200: duration_days ============================


async def test_grant_duration_days_sets_pro_active_with_expiry(client, session, admin_headers):
    """duration_days=30 → pro/active, expires_at≈now+30д, маркеры admin-grant (store/raw)."""
    user = await _user(session, "u_sub_dur00000001")
    before = datetime.now(UTC)
    resp = await _grant(client, user.id, {"duration_days": 30}, admin_headers)
    after = datetime.now(UTC)
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == user.id
    assert body["access_level"] == "pro"
    assert body["status"] == "active"

    sub = await get_subscription(session, user.id)
    assert sub is not None
    assert sub.access_level == "pro"
    assert sub.status == "active"
    assert sub.will_renew is False
    assert sub.grace_until is None
    assert sub.store == "admin"
    assert sub.product_id is None
    assert sub.raw["source"] == "admin_grant"
    # expires_at UTC-aware и ≈ now + 30д (в окне обработки запроса).
    assert sub.expires_at is not None
    assert sub.expires_at.tzinfo is not None
    assert before + timedelta(days=30) - timedelta(seconds=5) <= sub.expires_at
    assert sub.expires_at <= after + timedelta(days=30) + timedelta(seconds=5)


# ============================ 200: бессрочно ============================


@pytest.mark.parametrize(
    "body",
    [{}, {"duration_days": None, "expires_at": None}],
    ids=["empty_body", "both_null"],
)
async def test_grant_indefinite_sets_expires_at_null(client, session, admin_headers, body):
    """Тело {} / оба null → бессрочно: expires_at IS NULL, pro/active (ADR-037 §C)."""
    uid = f"u_sub_inf{abs(hash(str(body))) % 10**8:08d}"
    await _user(session, uid)
    resp = await _grant(client, uid, body, admin_headers)
    assert resp.status_code == 200
    assert resp.json()["access_level"] == "pro"
    assert resp.json()["status"] == "active"

    sub = await get_subscription(session, uid)
    assert sub is not None
    assert sub.expires_at is None  # бессрочно
    assert sub.access_level == "pro"
    assert sub.status == "active"
    assert sub.raw["expires_at"] is None


# ============================ 200: явный expires_at ============================


async def test_grant_explicit_expires_at_stored_as_passed(client, session, admin_headers):
    """expires_at=<future ISO> → subscriptions.expires_at == переданному (UTC-aware)."""
    uid = "u_sub_exp00000001"
    await _user(session, uid)
    target = (datetime.now(UTC) + timedelta(days=90)).replace(microsecond=0)
    resp = await _grant(client, uid, {"expires_at": target.isoformat()}, admin_headers)
    assert resp.status_code == 200

    sub = await get_subscription(session, uid)
    assert sub is not None
    assert sub.expires_at is not None
    assert sub.expires_at.tzinfo is not None
    assert sub.expires_at == target
    assert sub.access_level == "pro"


# ============================ 422: невалидная форма срока ============================


async def test_grant_both_fields_422_problem_json(client, session, admin_headers):
    """duration_days и expires_at одновременно → 422 application/problem+json."""
    uid = "u_sub_both00000001"
    await _user(session, uid)
    future = (datetime.now(UTC) + timedelta(days=10)).isoformat()
    resp = await _grant(client, uid, {"duration_days": 30, "expires_at": future}, admin_headers)
    assert resp.status_code == 422
    assert resp.headers["content-type"].startswith("application/problem+json")


@pytest.mark.parametrize("days", [0, -1])
async def test_grant_duration_days_non_positive_422(client, session, admin_headers, days):
    """duration_days<=0 (0 и -1) → 422 problem+json."""
    uid = f"u_sub_neg{abs(days):08d}01"
    await _user(session, uid)
    resp = await _grant(client, uid, {"duration_days": days}, admin_headers)
    assert resp.status_code == 422
    assert resp.headers["content-type"].startswith("application/problem+json")


@pytest.mark.parametrize("offset_s", [-86400, 0], ids=["past", "now"])
async def test_grant_expires_at_not_future_422(client, session, admin_headers, offset_s):
    """expires_at в прошлом/настоящем → 422 problem+json."""
    uid = f"u_sub_pst{abs(offset_s) % 10**8:08d}"
    await _user(session, uid)
    ts = (datetime.now(UTC) + timedelta(seconds=offset_s)).isoformat()
    resp = await _grant(client, uid, {"expires_at": ts}, admin_headers)
    assert resp.status_code == 422
    assert resp.headers["content-type"].startswith("application/problem+json")


# ============================ 404: несуществующий user ============================


async def test_grant_unknown_user_404_no_user_no_subscription(client, session, admin_headers):
    """Несуществующий user_id → 404; юзер НЕ создаётся, строка subscriptions не появляется."""
    uid = "u_sub_nosuch0001"
    resp = await _grant(client, uid, {"duration_days": 30}, admin_headers)
    assert resp.status_code == 404
    assert resp.json()["status"] == 404

    # В отличие от login-as — юзер НЕ создан.
    assert await session.get(User, uid) is None
    # Строка subscriptions не появилась.
    sub = await get_subscription(session, uid)
    assert sub is None


# ============================ идемпотентность (ADR-037 §D) ============================


async def test_grant_idempotent_same_row_started_at_preserved(client, session, admin_headers):
    """Повтор на тот же user_id обновляет ТУ ЖЕ строку (одна на user_id):

    - дубля subscriptions нет;
    - started_at от первого grant сохраняется при повторе;
    - expires_at обновляется новым сроком.
    """
    uid = "u_sub_idem00000001"
    await _user(session, uid)

    r1 = await _grant(client, uid, {"duration_days": 10}, admin_headers)
    assert r1.status_code == 200
    sub1 = await get_subscription(session, uid)
    assert sub1 is not None
    started_first = sub1.started_at
    expires_first = sub1.expires_at
    assert started_first is not None

    r2 = await _grant(client, uid, {"duration_days": 60}, admin_headers)
    assert r2.status_code == 200

    # Ровно одна строка subscriptions на user_id.
    count = await session.scalar(
        select(func.count()).select_from(Subscription).where(Subscription.user_id == uid)
    )
    assert count == 1

    sub2 = await get_subscription(session, uid)
    assert sub2 is not None
    assert sub2.id == sub1.id  # та же строка
    assert sub2.started_at == started_first  # started_at сохранён
    assert sub2.expires_at is not None and expires_first is not None
    assert sub2.expires_at > expires_first  # срок обновлён (60д > 10д)
    assert sub2.access_level == "pro"


# ==================== токены НЕ начисляются (ADR-037 требование 2) ====================


async def test_grant_does_not_touch_tokens(client, session, admin_headers):
    """Токены НЕ начисляются: balance/credit_grants/billing_events не меняются после grant."""
    uid = "u_sub_notok00001"
    await _user(session, uid, balance=7)

    resp = await _grant(client, uid, {"duration_days": 30}, admin_headers)
    assert resp.status_code == 200

    user = await session.get(User, uid)
    await session.refresh(user)
    assert user.bonus_generations_balance == 7  # не изменён

    grants = await session.scalar(
        select(func.count()).select_from(CreditGrant).where(CreditGrant.user_id == uid)
    )
    assert grants == 0  # ни одной строки credit_grants

    events = await session.scalar(
        select(func.count()).select_from(BillingEvent).where(BillingEvent.user_id == uid)
    )
    assert events == 0  # ни одной строки billing_events

    # В ответе balance отражён без изменений.
    assert resp.json()["bonus_generations_balance"] == 7


# ==================== ответ == снимок GET /admin/users (ADR-037 §A) ====================


async def test_grant_response_equals_get_user_snapshot(client, session, admin_headers):
    """Ответ 200 == снимок GET /admin/users/{user_id} (access_level=pro, обновлённые pro-квоты)."""
    uid = "u_sub_snap00000001"
    await _user(session, uid, balance=3)

    grant_resp = await _grant(client, uid, {"duration_days": 30}, admin_headers)
    assert grant_resp.status_code == 200
    grant_body = grant_resp.json()

    get_resp = await client.get(f"/v1/admin/users/{uid}", headers=admin_headers)
    assert get_resp.status_code == 200
    assert get_resp.json() == grant_body  # снимки идентичны

    # Содержательно: pro-квоты применены.
    assert grant_body["access_level"] == "pro"
    q = grant_body["quota"]
    assert q["monthly_generations"] == 100  # pro plan_quota
    assert q["max_projects"] is None  # pro: безлимит проектов
    assert q["max_concurrent_jobs"] == 3  # pro cap
