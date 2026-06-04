"""Integration: админ-плоскость ADR-021 (login-as + credits + require_admin через HTTP).

Источник истины — docs/modules/admin/02-api-contracts.md + 03-architecture.md.
Реальный Postgres (client шарит тест-сессию); миграция 20260604_0001 применена QA до прогона.
Защита эндпоинтов — X-Admin-Key (не Bearer). Ключ выдаётся login-as один раз, проверяем что
им проходит current_user за того же user_id.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.security import hash_api_key
from app.db.models import CreditGrant, User

pytestmark = pytest.mark.asyncio

_ADMIN_PATHS = (
    ("post", "/v1/admin/login-as"),
    ("post", "/v1/admin/users/u_x/credits"),
    ("get", "/v1/admin/users/u_x"),
)


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


# ============================ require_admin через HTTP ============================


async def _admin_request(client, method, path, **kwargs):  # noqa: ANN001
    """POST шлёт пустое тело; GET не несёт body (httpx.get не принимает json=)."""
    if method == "post":
        return await client.post(path, json={}, **kwargs)
    return await client.get(path, **kwargs)


@pytest.mark.parametrize(("method", "path"), _ADMIN_PATHS)
async def test_admin_endpoint_without_key_401_problem_json(client, method, path):
    """Любой админ-эндпоинт без X-Admin-Key → 401 application/problem+json (RFC-7807)."""
    resp = await _admin_request(client, method, path)
    assert resp.status_code == 401
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert body["status"] == 401
    assert body["title"] == "Unauthorized"
    assert {"type", "title", "status", "detail"} <= set(body)


@pytest.mark.parametrize(("method", "path"), _ADMIN_PATHS)
async def test_admin_endpoint_invalid_key_401(client, method, path):
    """Неверный X-Admin-Key → 401 (без раскрытия причины)."""
    resp = await _admin_request(client, method, path, headers={"X-Admin-Key": "nope"})
    assert resp.status_code == 401
    assert resp.headers["content-type"].startswith("application/problem+json")


async def test_admin_plane_disabled_when_key_unconfigured(client, admin_headers, monkeypatch):
    """ADMIN_API_KEY пуст → даже валидный (прежде) ключ → 401 (плоскость отключена)."""
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "admin_api_key", None, raising=False)
    resp = await client.post("/v1/admin/login-as", json={}, headers=admin_headers)
    assert resp.status_code == 401


# ================================ login-as ================================


async def test_login_as_existing_user_issues_working_bearer(client, session, admin_headers):
    """Существующий user_id → выпуск Bearer; этим ключом current_user → тот же user."""
    user = await _user(session, "u_admin_login_exist01")
    resp = await client.post("/v1/admin/login-as", json={"user_id": user.id}, headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == user.id
    assert body["api_key"].startswith("lv_")
    assert body["token_id"].startswith("t_")

    # Полученным ключом проходит обычная Bearer-аутентификация за того же user_id.
    me = await client.get("/v1/billing/me", headers={"Authorization": f"Bearer {body['api_key']}"})
    assert me.status_code == 200


async def test_login_as_nonexistent_user_creates_user_without_apple(client, session, admin_headers):
    """Несуществующий user_id → создаётся User(apple_sub=NULL, adapty_customer_user_id=id)."""
    target = "u_admin_created00001"
    resp = await client.post("/v1/admin/login-as", json={"user_id": target}, headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["user_id"] == target

    created = await session.get(User, target)
    assert created is not None
    assert created.apple_sub is None
    assert created.adapty_customer_user_id == target
    assert created.status == "active"
    assert created.bonus_generations_balance == 0


async def test_login_as_omitted_user_id_generates_new(client, session, admin_headers):
    """user_id опущен → сервер генерирует новый u_... и создаёт юзера."""
    resp = await client.post("/v1/admin/login-as", json={}, headers=admin_headers)
    assert resp.status_code == 200
    new_id = resp.json()["user_id"]
    assert new_id.startswith("u_")
    assert await session.get(User, new_id) is not None


async def test_login_as_key_returned_once_only(client, session, admin_headers):
    """Ключ возвращается один раз: повторный login-as того же user даёт ДРУГОЙ ключ."""
    user = await _user(session, "u_admin_once00000001")
    r1 = await client.post("/v1/admin/login-as", json={"user_id": user.id}, headers=admin_headers)
    r2 = await client.post("/v1/admin/login-as", json={"user_id": user.id}, headers=admin_headers)
    assert r1.json()["api_key"] != r2.json()["api_key"]
    assert r1.json()["token_id"] != r2.json()["token_id"]


# ================================ credits ================================


async def test_grant_credits_positive_increments_balance_and_writes_ledger(
    client, session, admin_headers
):
    """amount>0 → balance += amount + строка credit_grants(created_by='admin')."""
    user = await _user(session, "u_admin_credpos0001", balance=5)
    resp = await client.post(
        f"/v1/admin/users/{user.id}/credits",
        json={"amount": 10, "reason": "promo"},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == user.id
    assert body["amount_applied"] == 10
    assert body["bonus_generations_balance"] == 15

    await session.refresh(user)
    assert user.bonus_generations_balance == 15

    from sqlalchemy import select

    grants = (
        (await session.execute(select(CreditGrant).where(CreditGrant.user_id == user.id)))
        .scalars()
        .all()
    )
    assert len(grants) == 1
    assert grants[0].amount == 10
    assert grants[0].reason == "promo"
    assert grants[0].created_by == "admin"
    assert grants[0].id.startswith("cg_")


async def test_grant_credits_idempotent_replay_no_double(client, session, admin_headers):
    """Повтор с тем же Idempotency-Key → no-op (баланс не удваивается, одна строка ledger)."""
    user = await _user(session, "u_admin_idem000001", balance=0)
    headers = {**admin_headers, "Idempotency-Key": "grant-idem-1"}
    r1 = await client.post(
        f"/v1/admin/users/{user.id}/credits", json={"amount": 7}, headers=headers
    )
    r2 = await client.post(
        f"/v1/admin/users/{user.id}/credits", json={"amount": 7}, headers=headers
    )
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["bonus_generations_balance"] == 7
    assert r2.json()["bonus_generations_balance"] == 7  # НЕ 14

    await session.refresh(user)
    assert user.bonus_generations_balance == 7

    from sqlalchemy import func, select

    count = await session.scalar(
        select(func.count()).select_from(CreditGrant).where(CreditGrant.user_id == user.id)
    )
    assert count == 1


async def test_negative_correction_below_zero_409(client, session, admin_headers):
    """amount<0, уводящий баланс < 0 → 409 RFC-7807 (HTTP-контракт).

    Корректность rollback DB-состояния (баланс цел, строка ledger не пишется) — на автономной
    сессии в test_admin_credits_service.py (сервис делает РЕАЛЬНЫЙ session.rollback(), который
    несовместим с savepoint-обёрткой общей тест-сессии HTTP-клиента). Здесь — HTTP-статус/тело.
    """
    user = await _user(session, "u_admin_neg000001", balance=3)
    resp = await client.post(
        f"/v1/admin/users/{user.id}/credits", json={"amount": -10}, headers=admin_headers
    )
    assert resp.status_code == 409
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert body["status"] == 409
    assert body["title"] == "Conflict"
    # detail указывает текущий баланс (docs/admin §02).
    assert "3" in body["detail"]


async def test_negative_correction_to_exact_zero_allowed(client, session, admin_headers):
    """amount<0, уводящий баланс ровно в 0 (>= 0) → 200 (инвариант >= 0 соблюдён)."""
    user = await _user(session, "u_admin_negzero001", balance=4)
    resp = await client.post(
        f"/v1/admin/users/{user.id}/credits", json={"amount": -4}, headers=admin_headers
    )
    assert resp.status_code == 200
    assert resp.json()["bonus_generations_balance"] == 0


async def test_amount_zero_422(client, session, admin_headers):
    """amount==0 → 422 (application/problem+json)."""
    user = await _user(session, "u_admin_zero000001")
    resp = await client.post(
        f"/v1/admin/users/{user.id}/credits", json={"amount": 0}, headers=admin_headers
    )
    assert resp.status_code == 422
    assert resp.headers["content-type"].startswith("application/problem+json")


async def test_credits_unknown_user_404(client, admin_headers):
    """Несуществующий user_id → 404."""
    resp = await client.post(
        "/v1/admin/users/u_admin_nosuch0001/credits",
        json={"amount": 5},
        headers=admin_headers,
    )
    assert resp.status_code == 404
    assert resp.json()["status"] == 404


# ============================ GET /admin/users/{user_id} ============================


async def test_get_user_unknown_404(client, admin_headers):
    """GET несуществующего user → 404."""
    resp = await client.get("/v1/admin/users/u_admin_getnone01", headers=admin_headers)
    assert resp.status_code == 404


async def test_get_user_returns_balance_and_quota(client, session, admin_headers):
    """GET → bonus_generations_balance + квота (generations_remaining учитывает кредиты).

    Free-дефолт: monthly_generations=3, used=3 (исчерпан план) + balance=25 кредитов →
    generations_remaining = max(0, 3-3) + 25 = 25 (docs/admin §02 пример).
    """
    from datetime import UTC, datetime

    from app.db.models import UsageCounter

    user = await _user(session, "u_admin_getq00001", balance=25)
    period = datetime.now(UTC).strftime("%Y-%m")
    session.add(UsageCounter(user_id=user.id, period=period, generations_used=3))
    await session.flush()

    resp = await client.get(f"/v1/admin/users/{user.id}", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == user.id
    assert body["bonus_generations_balance"] == 25
    q = body["quota"]
    assert q["monthly_generations"] == 3
    assert q["generations_used"] == 3
    assert q["generations_remaining"] == 25  # план 0 + 25 кредитов
