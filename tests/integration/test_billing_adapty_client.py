"""Integration: AdaptyClient.get_profile + rate-limit (docs/billing/03 §3.1, ADR-009).

Rate-limit к Adapty (Redis token-bucket, ключ adapty:rl) — превышение → AdaptyTransientError
(backoff-ретрай). Маппинг статусов: 404→None, 429/5xx→AdaptyTransientError, прочее→AdaptyError,
200→AdaptyProfile. httpx-граница изолирована моком httpx.AsyncClient.

Real Redis (flush_redis для изоляции token-bucket).
"""

from __future__ import annotations

import pytest

from app.billing import adapty_client as ac
from app.billing.adapty_client import AdaptyClient, AdaptyError, AdaptyTransientError
from app.core.config import get_settings

pytestmark = pytest.mark.asyncio


class _FakeResp:
    def __init__(self, status_code, payload=None):  # noqa: ANN001
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):  # noqa: ANN202
        return self._payload


class _FakeAsyncClient:
    """Мок httpx.AsyncClient: возвращает заранее заданный ответ на .get()."""

    def __init__(self, resp):  # noqa: ANN001
        self._resp = resp

    async def __aenter__(self):  # noqa: ANN204
        return self

    async def __aexit__(self, *a):  # noqa: ANN002, ANN204
        return False

    async def get(self, *a, **k):  # noqa: ANN002, ANN003, ANN202
        return self._resp


def _patch_httpx(monkeypatch, resp):  # noqa: ANN001
    monkeypatch.setattr(ac.httpx, "AsyncClient", lambda *a, **k: _FakeAsyncClient(resp))


async def test_get_profile_404_returns_none(flush_redis, monkeypatch):
    _patch_httpx(monkeypatch, _FakeResp(404))
    client = AdaptyClient(get_settings())
    assert await client.get_profile("u_x") is None


async def test_get_profile_429_raises_transient(flush_redis, monkeypatch):
    _patch_httpx(monkeypatch, _FakeResp(429))
    client = AdaptyClient(get_settings())
    with pytest.raises(AdaptyTransientError):
        await client.get_profile("u_x")


async def test_get_profile_5xx_raises_transient(flush_redis, monkeypatch):
    _patch_httpx(monkeypatch, _FakeResp(503))
    client = AdaptyClient(get_settings())
    with pytest.raises(AdaptyTransientError):
        await client.get_profile("u_x")


async def test_get_profile_4xx_raises_adapty_error(flush_redis, monkeypatch):
    _patch_httpx(monkeypatch, _FakeResp(400))
    client = AdaptyClient(get_settings())
    with pytest.raises(AdaptyError):
        await client.get_profile("u_x")


async def test_get_profile_200_parses_active_level(flush_redis, monkeypatch):
    payload = {
        "data": {
            "access_levels": {"pro": {"is_active": True, "vendor_product_id": "lovable.pro"}},
            "subscriptions": {
                "lovable.pro": {"will_renew": True, "expires_at": "2026-08-01T00:00:00Z"}
            },
        }
    }
    _patch_httpx(monkeypatch, _FakeResp(200, payload))
    client = AdaptyClient(get_settings())
    profile = await client.get_profile("u_x")
    assert profile is not None
    assert profile.access_level == "pro"
    assert profile.is_active is True
    assert profile.will_renew is True


async def test_rate_limit_blocks_after_max_window(flush_redis, monkeypatch):
    """Token-bucket: после исчерпания окна следующий getProfile → AdaptyTransientError."""
    _patch_httpx(monkeypatch, _FakeResp(404))
    client = AdaptyClient(get_settings())
    # Исчерпываем окно (лимит = ac._ADAPTY_RL_MAX_PER_WINDOW): эти проходят.
    for _ in range(ac._ADAPTY_RL_MAX_PER_WINDOW):
        await client.get_profile("u_rl")
    # Следующий в том же окне → rate-limit → transient.
    with pytest.raises(AdaptyTransientError):
        await client.get_profile("u_rl")
