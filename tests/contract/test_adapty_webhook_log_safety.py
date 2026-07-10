"""Contract: безопасность диагностических логов вебхука Adapty (ADR-040/041 §D, 05-security).

Наблюдаемость как security-сигнал: любой отброс/обработка события оставляет диагностический след,
НО значения payload логировать НЕЛЬЗЯ — там платёжные/персональные данные. Разрешён строго состав
из ADR-040 §D: reason, event_type, корреляционные customer_user_id (= наш user_id) и Adapty
profile_id (псевдонимный), результирующий adapty_event_id/маркер синтетического ключа (+ внутренние
user_id/event_id/amount/vendor_product_id-SKU/payload_type/error/outcome). ЗАПРЕЩЕНО: значения
event_properties (суммы, цены, transaction_id, receipt), email, idfa/idfv/advertising_id, сырой
payload.

Тест прогоняет несколько исходов и проверяет, что sentinel-значения чувствительных полей НЕ
попали ни в одно extra-поле лог-записи, а состав ключей — в разрешённом множестве.
"""

from __future__ import annotations

import logging
from decimal import Decimal

import pytest

from app.billing.webhook_handler import process_webhook
from app.core.config import get_settings
from app.db.models import User
from tests.support import adapty_payloads as ap

pytestmark = pytest.mark.asyncio

# Разрешённый состав extra-ключей диагностики (ADR-040 §D + внутренние корреляционные).
_ALLOWED_EXTRA_KEYS = {
    "reason",
    "event_type",
    "customer_user_id",
    "profile_id",
    "adapty_event_id",
    "user_id",
    "event_id",
    "amount",
    "vendor_product_id",
    "payload_type",
    "error",
    "outcome",
}

# Sentinel-значения чувствительных полей — НИ ОДНО не должно утечь в логи.
_EMAIL = "buyer-pii@example.com"
_IDFA = "IDFA-00000000-0000-0000-0000-PIIVALUE"
_IDFV = "IDFV-11111111-1111-1111-1111-PIIVALUE"
_ADID = "ADID-22222222-PIIVALUE"
_TXID = "TXN-SENSITIVE-9999"
_RECEIPT = "RECEIPT-SENSITIVE-BLOB-DEADBEEF"
_PRICE = "49.99-SENSITIVE"
_FORBIDDEN_VALUES = (_EMAIL, _IDFA, _IDFV, _ADID, _TXID, _RECEIPT, _PRICE)

_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()) | {
    "message",
    "asctime",
    "taskName",
}


class _CaptureHandler(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.NOTSET)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith("app.billing"):
            self.records.append(record)


def _extra_fields(record: logging.LogRecord) -> dict:
    return {
        k: v for k, v in record.__dict__.items() if k not in _RESERVED and not k.startswith("_")
    }


async def _user(session, uid: str) -> User:  # noqa: ANN001
    user = User(
        id=uid,
        adapty_customer_user_id=uid,
        api_key_hash=None,
        monthly_budget_usd=Decimal("50.0000"),
        status="active",
    )
    session.add(user)
    await session.flush()
    return user


async def _process(session, payload):  # noqa: ANN001
    return await process_webhook(session, payload, ap.to_body(payload))


def _sensitive_props(profile_event_id: str) -> dict:
    """event_properties с чувствительными значениями.

    tier-1: profile_event_id → txid не попадает в ключ дедупа.
    """
    return ap.make_event_properties(
        profile_event_id=profile_event_id,
        subscription_expires_at="2026-08-01T00:00:00Z",
        vendor_product_id="lovable.pro.weekly",
        transaction_id=_TXID,
        extra={"price": _PRICE, "currency": "USD", "receipt": _RECEIPT},
    )


@pytest.fixture
def capture_logs():
    handler = _CaptureHandler()
    root = logging.getLogger()
    prev_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        root.removeHandler(handler)
        root.setLevel(prev_level)


async def test_processed_event_logs_no_sensitive_values(session, capture_logs, monkeypatch):
    """Обработанное подписочное событие: логи НЕ несут email/idfa/txid/price/receipt/raw payload."""
    monkeypatch.setattr(get_settings(), "token_pack_products", "", raising=False)
    user = await _user(session, "u_logsafe_ok000001")
    peid = ap.new_profile_event_id()
    payload = ap.make_webhook_payload(
        event_type="subscription_started",
        customer_user_id=user.id,
        event_properties=_sensitive_props(peid),
        include_pii=True,  # верхнеуровневые email/idfa/idfv/advertising_id
    )
    await _process(session, payload)
    _assert_logs_clean(capture_logs, payload)


async def test_fallback_and_unknown_user_logs_no_sensitive_values(session, capture_logs):
    """Диагностические WARN (unknown_user) не несут PII/payment/raw payload."""
    peid = ap.new_profile_event_id()
    payload = ap.make_webhook_payload(
        event_type="subscription_started",
        customer_user_id="u_ghost_does_not_exist",
        event_properties=_sensitive_props(peid),
        include_pii=True,
    )
    await _process(session, payload)
    _assert_logs_clean(capture_logs, payload)


async def test_ignored_known_event_logs_no_sensitive_values(session, capture_logs):
    """CONSCIOUSLY_IGNORED (INFO unhandled_known_event) не несёт PII/payment/raw."""
    user = await _user(session, "u_logsafe_ign00001")
    peid = ap.new_profile_event_id()
    payload = ap.make_webhook_payload(
        event_type="trial_started",
        customer_user_id=user.id,
        event_properties=_sensitive_props(peid),
        include_pii=True,
    )
    await _process(session, payload)
    _assert_logs_clean(capture_logs, payload)


def _assert_logs_clean(handler: _CaptureHandler, payload: dict) -> None:
    assert handler.records, "ожидался хотя бы один диагностический лог-след (ADR-040 §D)"
    for rec in handler.records:
        extra = _extra_fields(rec)
        # (1) Состав ключей — в разрешённом множестве.
        unexpected = set(extra) - _ALLOWED_EXTRA_KEYS
        assert not unexpected, f"недопустимые extra-ключи в {rec.getMessage()}: {unexpected}"
        # (2) Значения чувствительных полей не утекли.
        blob = repr(extra) + " " + rec.getMessage()
        for forbidden in _FORBIDDEN_VALUES:
            assert forbidden not in blob, (
                f"чувствительное значение {forbidden!r} утекло в лог {rec.getMessage()}: {extra}"
            )
        # (3) Сырой payload целиком не логируется.
        for value in extra.values():
            assert value != payload, "сырой payload не должен попадать в extra-поля лога"
