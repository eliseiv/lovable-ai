"""Unit: Bearer-авторизация вебхука Adapty (ADR-027 §A, docs/billing/02 §1 Auth).

HMAC-подпись убрана — webhook авторизуется статическим секретом ADAPTY_WEBHOOK_SECRET
через `Authorization: Bearer <secret>`, сравнение constant-time (hmac.compare_digest).
Здесь — чистые unit-проверки извлечения Bearer-токена и контракта сравнения (без HTTP/БД).
Поведение на HTTP-уровне (401/500, авторизация до парсинга тела) — в integration-тестах.
"""

from __future__ import annotations

import hmac

import pytest

from app.api.routers.billing import _extract_bearer_token

_SECRET = "adapty-test-secret"  # noqa: S105 - тестовый секрет, не production credential


def test_verify_webhook_signature_removed():
    """Регрессия ADR-027: HMAC verify_webhook_signature удалён из adapty_client."""
    import app.billing.adapty_client as adapty_client

    assert not hasattr(adapty_client, "verify_webhook_signature")


def test_extract_bearer_token_valid():
    assert _extract_bearer_token(f"Bearer {_SECRET}") == _SECRET


def test_extract_bearer_token_strips_whitespace():
    assert _extract_bearer_token(f"Bearer   {_SECRET}  ") == _SECRET


def test_extract_bearer_token_missing_header():
    assert _extract_bearer_token(None) is None
    assert _extract_bearer_token("") is None


def test_extract_bearer_token_wrong_scheme():
    # Не Bearer-схема (другой префикс) → None (на HTTP даёт 401).
    assert _extract_bearer_token(f"Basic {_SECRET}") is None
    assert _extract_bearer_token(_SECRET) is None


def test_extract_bearer_token_case_sensitive_scheme():
    # Схема "Bearer " чувствительна к регистру (lowercase 'bearer' не матчится).
    assert _extract_bearer_token(f"bearer {_SECRET}") is None


@pytest.mark.parametrize(
    ("token", "secret", "expected"),
    [
        (_SECRET, _SECRET, True),
        ("wrong-secret", _SECRET, False),
        ("", _SECRET, False),
        (_SECRET + "x", _SECRET, False),
    ],
)
def test_constant_time_compare_contract(token: str, secret: str, expected: bool):
    """Контракт сравнения токена — hmac.compare_digest (constant-time, как в роутере)."""
    assert hmac.compare_digest(token, secret) is expected
