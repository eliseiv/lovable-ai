"""Unit: верификация подписи вебхука Adapty (HMAC-SHA256, docs/billing/03 §2.1, ADR-009).

Валидная подпись→True; невалидная/отсутствующая/пустой секрет/изменённое тело→False
(на HTTP даёт 401 без раскрытия причины). Сравнение constant-time (hmac.compare_digest).
"""

from __future__ import annotations

import hashlib
import hmac

from app.billing.adapty_client import verify_webhook_signature

_SECRET = "adapty-test-secret"  # noqa: S105 - тестовый секрет, не production credential
_BODY = b'{"event_id":"evt_1","event_type":"subscription_started","customer_user_id":"u_1"}'


def _sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def test_valid_signature_returns_true():
    sig = _sign(_SECRET, _BODY)
    assert verify_webhook_signature(_SECRET, _BODY, sig) is True


def test_invalid_signature_returns_false():
    assert verify_webhook_signature(_SECRET, _BODY, "deadbeef" * 8) is False


def test_missing_signature_returns_false():
    assert verify_webhook_signature(_SECRET, _BODY, None) is False
    assert verify_webhook_signature(_SECRET, _BODY, "") is False


def test_empty_secret_returns_false():
    sig = _sign(_SECRET, _BODY)
    assert verify_webhook_signature("", _BODY, sig) is False


def test_tampered_body_fails_verification():
    sig = _sign(_SECRET, _BODY)
    tampered = _BODY.replace(b"u_1", b"u_2")
    assert verify_webhook_signature(_SECRET, tampered, sig) is False


def test_wrong_secret_fails_verification():
    sig = _sign("other-secret", _BODY)
    assert verify_webhook_signature(_SECRET, _BODY, sig) is False


def test_signature_with_surrounding_whitespace_is_stripped():
    sig = _sign(_SECRET, _BODY)
    assert verify_webhook_signature(_SECRET, _BODY, f"  {sig}  ") is True
