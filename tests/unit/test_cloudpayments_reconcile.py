"""Unit: разбор callback'а и сверка платежей RU-канала (ADR-052).

Проверяется то, что решает, начислять ли деньги, — без сети и БД: извлечение адресата из
сырого тела, отбор оплаченных и свежих платежей, классификация продукта и срок подписки.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.billing import cloudpayments
from app.core.config import Settings

_NOW = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
_PAID = frozenset({"succeeded"})


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "cloudpayments_api_base": "https://pay.example.dev/api/v1",
        "cloudpayments_app_id": "app-1",
        "cloudpayments_api_token": "token-1",
        "token_pack_products": "100_tokens_9.99:100,250_tokens_19.99:250",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _payment(**overrides: object) -> dict[str, object]:
    payment: dict[str, object] = {
        "payment_id": "pay-1",
        "status": "succeeded",
        "paid_at": "2026-09-08T11:00:00+00:00",
        "product": {"code": "100_tokens_9.99", "payment_type": "one_time"},
    }
    payment.update(overrides)
    return payment


# ============================ адресат callback'а ============================


@pytest.mark.parametrize("key", ["user_id", "userId", "AccountId", "account_id"])
def test_user_id_extracted_from_known_keys(key):
    raw = json.dumps({key: "u_abc123"}).encode()
    assert cloudpayments.extract_user_id(raw) == "u_abc123"


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"not json",
        b"[]",
        json.dumps({"user_id": ""}).encode(),
        json.dumps({"user_id": 42}).encode(),
        # Чужой формат id и попытки traversal/SSRF через путь сверки.
        json.dumps({"user_id": "../../admin"}).encode(),
        json.dumps({"user_id": "u_abc/../../x"}).encode(),
        json.dumps({"user_id": "https://evil.example/"}).encode(),
    ],
)
def test_bad_body_yields_no_user_id(raw):
    """Кривое тело не даёт адресата — вебхук ответит `ignored`, а не 422/500."""
    assert cloudpayments.extract_user_id(raw) is None


# ============================ сверка платежей ============================


def test_only_paid_and_fresh_payments_are_creditable():
    data = [
        _payment(payment_id="ok"),
        _payment(payment_id="pending", status="pending"),
        _payment(payment_id="stale", paid_at="2026-09-01T00:00:00+00:00"),
    ]

    creditable = cloudpayments.select_creditable_payments(
        data, paid_statuses=_PAID, now=_NOW, freshness_hours=72
    )

    assert [p.payment_id for p in creditable] == ["ok"]


@pytest.mark.parametrize(
    "broken",
    [
        {"payment_id": ""},
        {"product": {}},
        {"product": {"code": "x"}},
        {"product": {"payment_type": "one_time"}},
        {"paid_at": "not-a-date"},
        {"paid_at": None},
    ],
)
def test_incomplete_payment_is_skipped(broken):
    """Платёж без обязательных полей не начисляется: лучше пропустить, чем угадать."""
    data = [_payment(**broken)]

    assert (
        cloudpayments.select_creditable_payments(
            data, paid_statuses=_PAID, now=_NOW, freshness_hours=72
        )
        == []
    )


def test_paid_at_with_z_suffix_is_parsed():
    data = [_payment(paid_at="2026-09-08T11:00:00Z")]

    creditable = cloudpayments.select_creditable_payments(
        data, paid_statuses=_PAID, now=_NOW, freshness_hours=72
    )

    assert creditable[0].paid_at == datetime(2026, 9, 8, 11, 0, tzinfo=UTC)


def test_freshness_boundary_is_inclusive():
    edge = (_NOW - timedelta(hours=72)).isoformat()
    data = [_payment(paid_at=edge)]

    assert (
        len(
            cloudpayments.select_creditable_payments(
                data, paid_statuses=_PAID, now=_NOW, freshness_hours=72
            )
        )
        == 1
    )


# ============================ классификация и срок ============================


def _creditable(product_code: str, payment_type: str) -> cloudpayments.CreditablePayment:
    return cloudpayments.CreditablePayment(
        payment_id="pay-1",
        product_code=product_code,
        payment_type=payment_type,
        status="succeeded",
        paid_at=_NOW,
    )


@pytest.mark.parametrize("payment_type", ["one_time", "onetime", "one-time", "tokens"])
def test_one_time_payment_is_tokens(payment_type):
    assert cloudpayments.classify(_creditable("100_tokens_9.99", payment_type), _settings()) == (
        cloudpayments.KIND_TOKENS
    )


@pytest.mark.parametrize("payment_type", ["subscription", "recurrent", "recurring"])
def test_recurring_payment_is_subscription(payment_type):
    assert cloudpayments.classify(_creditable("week_6.99_nottrial", payment_type), _settings()) == (
        cloudpayments.KIND_SUBSCRIPTION
    )


def test_unclear_payment_type_falls_back_to_product_map():
    """Невнятный класс от провайдера → решает наш каталог токен-паков, а не догадка."""
    settings = _settings()

    assert cloudpayments.classify(_creditable("250_tokens_19.99", "??"), settings) == (
        cloudpayments.KIND_TOKENS
    )
    assert cloudpayments.classify(_creditable("yearly_49.99", "??"), settings) == (
        cloudpayments.KIND_SUBSCRIPTION
    )


@pytest.mark.parametrize(
    ("code", "days"),
    [
        ("week_6.99_nottrial", 7),
        ("Week_6.99_nottrial", 7),
        ("yearly_49.99_nottrial", 365),
        ("annual_plan", 365),
        ("monthly_pro", 30),
        ("day_pass", 1),
        ("mystery_plan", 30),
    ],
)
def test_subscription_period_from_product_code(code, days):
    assert cloudpayments.subscription_days(code) == days
