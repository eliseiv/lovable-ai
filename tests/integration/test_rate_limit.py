"""Integration: rate-limit token bucket (Redis), 60/min на key_id + login по IP.

docs/modules/auth/03-architecture.md §5, docs/05-security.md, docs/06-testing-strategy §Sprint 3.
Реальный Redis (token bucket). Превышение → allowed=False + retry_after (Retry-After).
Разные key_id одного user НЕ делят bucket; анонимный /auth/apple лимитируется по IP.
"""

from __future__ import annotations

import pytest

from app.auth.rate_limit import check_key_rate_limit, check_login_rate_limit
from app.core.config import get_settings

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("flush_redis")]


async def test_within_limit_allowed():
    limit = get_settings().rate_limit_per_min
    last = None
    for _ in range(limit):
        last = await check_key_rate_limit("kid-within")
    assert last is not None
    assert last.allowed is True
    assert last.retry_after_s == 0


async def test_exceeding_limit_returns_429_with_retry_after():
    limit = get_settings().rate_limit_per_min
    # Исчерпываем bucket (limit запросов разрешены), (limit+1)-й → отказ.
    for _ in range(limit):
        res = await check_key_rate_limit("kid-exceed")
        assert res.allowed is True
    over = await check_key_rate_limit("kid-exceed")
    assert over.allowed is False
    assert over.retry_after_s > 0  # для заголовка Retry-After


async def test_distinct_key_ids_have_independent_buckets():
    """Разные key_id (мульти-устройство одного user) не делят bucket — масштабируются независимо."""
    limit = get_settings().rate_limit_per_min
    for _ in range(limit):
        await check_key_rate_limit("kid-A")
    # kid-A исчерпан, но kid-B имеет свежий bucket.
    assert (await check_key_rate_limit("kid-A")).allowed is False
    assert (await check_key_rate_limit("kid-B")).allowed is True


async def test_login_rate_limit_per_ip_independent():
    """Анонимный /auth/apple лимит по IP (rl:apple:{ip}) — защита от брутфорса логина."""
    limit = get_settings().rate_limit_per_min
    for _ in range(limit):
        await check_login_rate_limit("10.0.0.1")
    assert (await check_login_rate_limit("10.0.0.1")).allowed is False
    assert (await check_login_rate_limit("10.0.0.2")).allowed is True


async def test_login_bucket_separate_from_key_bucket():
    """rl:apple:{ip} и rl:{key_id} — разные пространства ключей Redis (не пересекаются)."""
    limit = get_settings().rate_limit_per_min
    for _ in range(limit):
        await check_login_rate_limit("kid-shared")  # как IP
    # тот же литерал как key_id → отдельный bucket.
    assert (await check_key_rate_limit("kid-shared")).allowed is True


# --- HTTP-уровень: 429 + Retry-After через endpoint (лимит понижен для скорости) ---


async def test_bearer_request_returns_429_when_key_limit_exceeded(
    client, make_apple_token, patch_apple_jwks, monkeypatch
):
    """Превышение лимита ключом на реальном endpoint → 429 + Retry-After (RFC-7807)."""
    settings = get_settings()
    monkeypatch.setattr(settings, "rate_limit_per_min", 3)

    r = await client.post(
        "/v1/auth/apple", json={"identity_token": make_apple_token(sub="apple-rl")}
    )
    api_key = r.json()["api_key"]
    headers = {"Authorization": f"Bearer {api_key}"}

    statuses = []
    for _ in range(6):
        resp = await client.get("/v1/auth/tokens", headers=headers)
        statuses.append(resp.status_code)
        if resp.status_code == 429:
            assert resp.headers["content-type"].startswith("application/problem+json")
            assert "Retry-After" in resp.headers
            assert int(resp.headers["Retry-After"]) > 0
    assert 429 in statuses, statuses
