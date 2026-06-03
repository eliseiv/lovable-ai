"""Minor-фиксы (прод 2026-06-04): app-level RFC-7807 на 422 + /metrics bare-Route без 307.

Реальный Postgres + Redis (conftest). Нормативный источник — app/api/main.py (app-level
RequestValidationError-handler + точный bare-Route GET /metrics), app/api/errors.py
(validation_exception_handler), docs/modules/api/02-api-contracts.md (RFC-7807),
docs/modules/observability/03-architecture.md §1 (экспозиция /metrics, инвариант I1).

Minor 1: app-level RequestValidationError → application/problem+json (RFC-7807) для ВСЕХ
эндпоинтов, включая публичный POST /auth/apple без identity_token. Прочие 422
(/devices без обязательного поля, /projects без Idempotency-Key) остаются problem+json.

Minor 2: GET /metrics — точный bare-Route (observability §1 I1), отдаёт 200 prometheus-text
напрямую БЕЗ 307/308. Прежняя регрессия: mount канонизировал к /metrics/ (307 scheme-downgrade
за TLS-прокси при redirect_slashes=True, либо 404 при redirect_slashes=False). Bare-Route
матчит ровно `/metrics` без slash-канонизации → 200 без редиректа (прод-фикс раунд 2).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio

_PROBLEM_CT = "application/problem+json"


def _is_rfc7807(body: dict) -> bool:
    """RFC-7807 обязательные поля Problem (type/title/status/detail)."""
    return all(k in body for k in ("type", "title", "status", "detail"))


# ---------------------------------------------------------------------------
# Minor 1: POST /auth/apple без identity_token → 422 application/problem+json (RFC-7807).
# ---------------------------------------------------------------------------


async def test_auth_apple_missing_identity_token_is_problem_json(client):  # noqa: ANN001
    """POST /auth/apple без identity_token → 422 application/problem+json + RFC-7807."""
    resp = await client.post("/v1/auth/apple", json={"nonce": "n"})
    assert resp.status_code == 422
    assert resp.headers["content-type"].startswith(_PROBLEM_CT)
    body = resp.json()
    assert _is_rfc7807(body)
    assert body["status"] == 422
    # Доменное поле errors[] перечисляет поля-ошибки (точечный loc + msg + type).
    assert "errors" in body
    locs = [e["loc"] for e in body["errors"]]
    assert any("identity_token" in loc for loc in locs)


async def test_auth_apple_empty_body_is_problem_json(client):  # noqa: ANN001
    """POST /auth/apple с пустым телом → 422 application/problem+json (не дефолтный json)."""
    resp = await client.post("/v1/auth/apple", json={})
    assert resp.status_code == 422
    assert resp.headers["content-type"].startswith(_PROBLEM_CT)
    assert _is_rfc7807(resp.json())


# ---------------------------------------------------------------------------
# Minor 1 (прочие 422): /devices без обязательного поля + /projects без Idempotency-Key.
# ---------------------------------------------------------------------------


async def test_devices_missing_field_is_problem_json(client, seeded_user, auth_headers):  # noqa: ANN001
    """POST /devices без обязательного apns_token → 422 application/problem+json (RFC-7807).

    seeded_user — Bearer-ключ auth_headers резолвится в юзера (иначе 401 до валидации тела).
    """
    # platform/environment заданы, apns_token отсутствует → RequestValidationError.
    resp = await client.post(
        "/v1/devices",
        json={"platform": "ios", "environment": "sandbox"},
        headers=auth_headers,
    )
    assert resp.status_code == 422
    assert resp.headers["content-type"].startswith(_PROBLEM_CT)
    body = resp.json()
    assert _is_rfc7807(body)
    assert any("apns_token" in e["loc"] for e in body["errors"])


async def test_projects_missing_idempotency_key_is_problem_json(client, seeded_user, auth_headers):  # noqa: ANN001
    """POST /projects без Idempotency-Key → 422 application/problem+json (ProblemException-путь).

    Это «другой» 422 (доменный unprocessable() → ProblemException), не RequestValidationError —
    он тоже обязан быть problem+json (RFC-7807).
    """
    resp = await client.post(
        "/v1/projects",
        json={"prompt": "build me a landing page"},
        headers=auth_headers,
    )
    assert resp.status_code == 422
    assert resp.headers["content-type"].startswith(_PROBLEM_CT)
    assert _is_rfc7807(resp.json())


# ---------------------------------------------------------------------------
# Minor 2: GET /metrics — точный bare-Route, 200 напрямую БЕЗ 307/308.
# ---------------------------------------------------------------------------


async def test_metrics_no_307_scheme_downgrade(client):  # noqa: ANN001
    """GET /metrics (bare, без follow) НЕ возвращает 307/308 (точный bare-Route, прод-фикс раунд 2).

    Прежняя регрессия: за TLS-прокси mount строил 307 Location: http://.../metrics/ (scheme-
    downgrade), т.к. uvicorn не знал о внешнем TLS. Точный bare-Route матчит ровно `/metrics` без
    trailing-slash-канонизации → 200 напрямую, без промежуточного редиректа (observability §1 I1).
    """
    resp = await client.get("/metrics", follow_redirects=False)  # ловим именно статус-код роута
    assert resp.status_code not in (307, 308), (
        f"bare /metrics не должен редиректить, got {resp.status_code}"
    )
    assert resp.status_code == 200


async def test_metrics_returns_200_with_prometheus_metrics(client):  # noqa: ANN001
    """GET /metrics (bare) → 200, prometheus content-type (CONTENT_TYPE_LATEST), lovable_*."""
    from prometheus_client import CONTENT_TYPE_LATEST

    resp = await client.get("/metrics", follow_redirects=False)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith(CONTENT_TYPE_LATEST.split(";")[0])
    assert "lovable_" in resp.text
