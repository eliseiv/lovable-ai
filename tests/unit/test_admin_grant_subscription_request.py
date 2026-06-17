"""Unit: валидация формы срока AdminGrantSubscriptionRequest (ADR-037 §A, admin §02).

Поля duration_days / expires_at взаимоисключающие, оба опциональны. Оба null (или тело {})
→ бессрочно (валидно). Оба заданы → ошибка. duration_days<=0 → ошибка. expires_at в
прошлом/настоящем → ошибка. Невалидная форма транслируется FastAPI в 422 application/
problem+json (HTTP-уровень покрыт интеграционными тестами роута).

Чистая валидация Pydantic — без I/O. Источник истины — ADR-037 §A + AdminGrantSubscriptionRequest.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.schemas.api import AdminGrantSubscriptionRequest


def test_empty_body_is_indefinite() -> None:
    """Тело {} → оба поля None (бессрочно, expires_at подписки = NULL)."""
    req = AdminGrantSubscriptionRequest()
    assert req.duration_days is None
    assert req.expires_at is None


def test_both_null_explicit_is_indefinite() -> None:
    """Явные оба null → бессрочно (валидно)."""
    req = AdminGrantSubscriptionRequest(duration_days=None, expires_at=None)
    assert req.duration_days is None
    assert req.expires_at is None


def test_duration_days_only_valid() -> None:
    """Только duration_days>0 → валидно."""
    req = AdminGrantSubscriptionRequest(duration_days=30)
    assert req.duration_days == 30
    assert req.expires_at is None


def test_expires_at_future_only_valid() -> None:
    """Только expires_at в будущем → валидно."""
    future = datetime.now(UTC) + timedelta(days=10)
    req = AdminGrantSubscriptionRequest(expires_at=future)
    assert req.expires_at == future
    assert req.duration_days is None


def test_both_fields_set_rejected() -> None:
    """duration_days И expires_at одновременно → ValidationError (неоднозначный срок → 422)."""
    future = datetime.now(UTC) + timedelta(days=10)
    with pytest.raises(ValidationError) as exc:
        AdminGrantSubscriptionRequest(duration_days=30, expires_at=future)
    assert "mutually exclusive" in str(exc.value)


@pytest.mark.parametrize("bad", [0, -1, -30])
def test_duration_days_non_positive_rejected(bad: int) -> None:
    """duration_days <= 0 (0 и отрицательные) → ValidationError → 422."""
    with pytest.raises(ValidationError) as exc:
        AdminGrantSubscriptionRequest(duration_days=bad)
    assert "greater than zero" in str(exc.value)


def test_expires_at_in_past_rejected() -> None:
    """expires_at в прошлом → ValidationError → 422."""
    past = datetime.now(UTC) - timedelta(days=1)
    with pytest.raises(ValidationError) as exc:
        AdminGrantSubscriptionRequest(expires_at=past)
    assert "future" in str(exc.value)


def test_expires_at_now_rejected() -> None:
    """expires_at == текущему моменту (не строго в будущем) → ValidationError → 422."""
    with pytest.raises(ValidationError) as exc:
        AdminGrantSubscriptionRequest(expires_at=datetime.now(UTC))
    assert "future" in str(exc.value)


def test_expires_at_naive_in_past_rejected() -> None:
    """Naive datetime трактуется как UTC; naive в прошлом → отклонён (tz-нормализация)."""
    naive_past = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1)
    with pytest.raises(ValidationError):
        AdminGrantSubscriptionRequest(expires_at=naive_past)
