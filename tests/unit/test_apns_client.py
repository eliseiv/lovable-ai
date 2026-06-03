"""Unit: APNs provider-JWT ES256 + payload + host-выбор + should_push (Sprint 5, ADR-013).

Покрывает (docs/06 §S5 APNs unit):
  - provider-JWT ES256: claims iss/iat, header kid/alg, подпись проверяема публичным
    тест-ключом; кэш по TTL (повтор в окне TTL не переподписывает; протух → переподпись);
  - APNs-хост по environment (production/sandbox) + override-дефолт APNS_ENV;
  - build_payload: aps.alert loc-key по to_state, custom job_id/state, live_url только для LIVE;
  - should_push: только LIVE/FAILED/AWAITING_CLARIFICATION (промежуточные — нет);
  - no-op без credentials (apns_configured == False).

Внешний APNs HTTP/2 здесь НЕ дёргается (это про подпись/формат); сетевые кейсы — в
test_apns_send.py (mock httpx).
"""

from __future__ import annotations

import time

import jwt
import pytest

from app.notify import apns_client
from app.notify.apns_client import (
    _apns_host,
    _mask_token,
    build_payload,
)
from app.notify.tasks import should_push

# --- provider-JWT ES256 (claims/header/подпись) ---


def test_provider_jwt_es256_claims_and_signature_verifiable(apns_credentials):
    cred = apns_credentials
    cache = apns_client.get_token_cache()
    token = cache.get(cred["settings"])

    header = jwt.get_unverified_header(token)
    assert header["alg"] == "ES256"
    assert header["kid"] == cred["kid"]

    # Подпись валидируется публичным тест-ключом (реальный Apple-ключ не нужен).
    claims = jwt.decode(token, cred["public_key"], algorithms=["ES256"])
    assert claims["iss"] == cred["team_id"]
    assert "iat" in claims
    assert isinstance(claims["iat"], int)


def test_provider_jwt_cached_within_ttl(apns_credentials):
    cred = apns_credentials
    cache = apns_client.get_token_cache()
    first = cache.get(cred["settings"])
    second = cache.get(cred["settings"])
    # В пределах APNS_JWT_TTL_S — тот же объект-строка (не переподписывали).
    assert first == second


def test_provider_jwt_resigned_after_ttl_expiry(apns_credentials, monkeypatch):
    cred = apns_credentials
    cache = apns_client.get_token_cache()
    first = cache.get(cred["settings"])
    # Имитируем истечение TTL: сдвигаем _issued_at в прошлое за пределы ttl.
    cache._issued_at = time.monotonic() - (cred["settings"].apns_jwt_ttl_s + 10)
    # Гарантируем другой iat (секундная гранулярность).
    monkeypatch.setattr(time, "time", lambda: int(time.monotonic()) + 10_000)
    second = cache.get(cred["settings"])
    assert first != second


def test_provider_jwt_no_credentials_raises_config_error():
    """Без .p8-ключа → ApnsConfigError (push no-op путь, ADR-013 §5)."""
    from app.core.config import get_settings

    settings = get_settings()
    # Свежий кэш без credentials (env conftest не задаёт APNS_AUTH_KEY/key_id).
    cache = apns_client._ProviderTokenCache()
    assert settings.apns_configured is False
    with pytest.raises(apns_client.ApnsConfigError):
        cache.get(settings)


# --- APNs host по environment ---


@pytest.mark.parametrize(
    ("device_env", "expected"),
    [
        ("production", "api.push.apple.com"),
        ("sandbox", "api.sandbox.push.apple.com"),
    ],
)
def test_apns_host_by_device_environment(device_env, expected):
    from app.core.config import get_settings

    assert _apns_host(get_settings(), device_env) == expected


def test_apns_host_falls_back_to_default_env_when_device_blank():
    """Пустой device environment → дефолт APNS_ENV (sandbox в тестах)."""
    from app.core.config import get_settings

    assert _apns_host(get_settings(), "") == "api.sandbox.push.apple.com"


# --- build_payload ---


def test_build_payload_live_includes_live_url_and_loc_key():
    payload = build_payload("LIVE", "j_abc", "https://x.apps.localhost")
    assert payload["aps"]["alert"]["loc-key"] == "job_status_live"
    assert payload["job_id"] == "j_abc"
    assert payload["state"] == "LIVE"
    assert payload["live_url"] == "https://x.apps.localhost"


def test_build_payload_failed_omits_live_url():
    payload = build_payload("FAILED", "j_abc", None)
    assert payload["aps"]["alert"]["loc-key"] == "job_status_failed"
    assert "live_url" not in payload
    assert payload["state"] == "FAILED"


# --- should_push: нормативный перечень (ADR-013 §3) ---


@pytest.mark.parametrize("state", ["LIVE", "FAILED", "AWAITING_CLARIFICATION"])
def test_should_push_true_for_significant_states(state):
    assert should_push(state) is True


@pytest.mark.parametrize(
    "state",
    ["CREATED", "INTERVIEWING", "SPECCING", "BUILDING", "DEPLOYING", "FIXING"],
)
def test_should_push_false_for_intermediate_states(state):
    assert should_push(state) is False


# --- маскирование токена в логах ---


def test_mask_token_keeps_last_6():
    assert _mask_token("abcdef0123456789") == "**********456789"


def test_mask_token_short_fully_masked():
    assert _mask_token("abc") == "***"
