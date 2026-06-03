"""Unit: ApnsClient.send — маппинг HTTP-ответов APNs (Sprint 5, ADR-013, docs/06 §S5).

Внешняя граница APNs HTTP/2 изолирована: httpx.AsyncClient подменяется фейком, реальная
сеть к Apple не вызывается. Покрывает:
  - 200 → ok (без инвалидации);
  - 410 Unregistered → invalid_token (инвалидация);
  - 400 BadDeviceToken → invalid_token; 400 иной reason → НЕ инвалидация;
  - 429 → ApnsTransientError (Celery retry); 5xx → ApnsTransientError;
  - прочие 4xx → ok=False best-effort drop (без retry/инвалидации);
  - запрос несёт authorization: bearer <provider-jwt>, apns-topic=bundle_id, HTTP/2.
"""

from __future__ import annotations

import json

import pytest

from app.notify.apns_client import ApnsClient, ApnsTransientError

pytestmark = pytest.mark.asyncio


class _FakeResponse:
    def __init__(self, status_code: int, body: dict | None = None) -> None:
        self.status_code = status_code
        self._body = body
        self.reason_phrase = "x"

    def json(self):  # noqa: ANN202
        if self._body is None:
            raise ValueError("no body")
        return self._body


class _FakeAsyncClient:
    """Фейк httpx.AsyncClient: ловит kwargs конструктора + аргументы post."""

    last_init_kwargs: dict = {}
    last_post: dict = {}

    def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
        _FakeAsyncClient.last_init_kwargs = kwargs

    async def __aenter__(self):  # noqa: ANN202
        return self

    async def __aexit__(self, *exc):  # noqa: ANN002, ANN202
        return False

    async def post(self, url, json=None, headers=None):  # noqa: ANN001, ANN202, A002
        _FakeAsyncClient.last_post = {"url": url, "json": json, "headers": headers}
        return _FakeAsyncClient.response


def _patch_httpx(monkeypatch, response: _FakeResponse) -> type[_FakeAsyncClient]:
    import app.notify.apns_client as mod

    _FakeAsyncClient.response = response
    monkeypatch.setattr(mod.httpx, "AsyncClient", _FakeAsyncClient)
    return _FakeAsyncClient


async def _send(settings, **kw):  # noqa: ANN001, ANN003
    client = ApnsClient(settings)
    return await client.send(
        apns_token=kw.get("apns_token", "tok123456"),
        device_environment=kw.get("device_environment", "sandbox"),
        payload=kw.get("payload", {"aps": {}}),
    )


async def test_send_200_ok(apns_credentials, monkeypatch):
    _patch_httpx(monkeypatch, _FakeResponse(200))
    result = await _send(apns_credentials["settings"])
    assert result.ok is True
    assert result.invalid_token is False
    assert result.status_code == 200


async def test_send_410_invalidates_token(apns_credentials, monkeypatch):
    _patch_httpx(monkeypatch, _FakeResponse(410, {"reason": "Unregistered"}))
    result = await _send(apns_credentials["settings"])
    assert result.ok is False
    assert result.invalid_token is True
    assert result.status_code == 410


async def test_send_400_bad_device_token_invalidates(apns_credentials, monkeypatch):
    _patch_httpx(monkeypatch, _FakeResponse(400, {"reason": "BadDeviceToken"}))
    result = await _send(apns_credentials["settings"])
    assert result.invalid_token is True


async def test_send_400_other_reason_not_invalidated(apns_credentials, monkeypatch):
    _patch_httpx(monkeypatch, _FakeResponse(400, {"reason": "PayloadTooLarge"}))
    result = await _send(apns_credentials["settings"])
    assert result.ok is False
    assert result.invalid_token is False


@pytest.mark.parametrize("code", [429, 500, 503])
async def test_send_transient_raises_for_retry(apns_credentials, monkeypatch, code):
    _patch_httpx(monkeypatch, _FakeResponse(code, {"reason": "TooManyRequests"}))
    with pytest.raises(ApnsTransientError):
        await _send(apns_credentials["settings"])


async def test_send_other_4xx_best_effort_drop(apns_credentials, monkeypatch):
    _patch_httpx(monkeypatch, _FakeResponse(403, {"reason": "Forbidden"}))
    result = await _send(apns_credentials["settings"])
    assert result.ok is False
    assert result.invalid_token is False
    assert result.status_code == 403


async def test_send_uses_http2_and_provider_jwt_and_topic(apns_credentials, monkeypatch):
    cred = apns_credentials
    fake = _patch_httpx(monkeypatch, _FakeResponse(200))
    await _send(cred["settings"], apns_token="devicetoken99", device_environment="production")

    assert fake.last_init_kwargs.get("http2") is True
    assert fake.last_post["url"] == "https://api.push.apple.com/3/device/devicetoken99"
    headers = fake.last_post["headers"]
    assert headers["authorization"].startswith("bearer ")
    assert headers["apns-topic"] == cred["settings"].apns_bundle_id
    # Тело — переданный payload (round-trip JSON-сериализуемо).
    assert json.dumps(fake.last_post["json"]) is not None
