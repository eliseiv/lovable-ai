"""Официальная форма payload вебхука Adapty — ПЕРЕИСПОЛЬЗУЕМАЯ фикстура (ADR-040/041 §F).

Root cause прод-инцидента nexoraweb.shop (2026-07-10): прежние contract/E2E-«проверки»
гонялись синтетическим payload'ом по НАШЕЙ ЖЕ (ошибочной) схеме — верхнеуровневый `event_id`
и объекты `profile`/`subscription`. Self-consistent payload проверяет код против его
собственных допущений и структурно НЕ способен поймать расхождение с реальной формой
провайдера → оба дефекта (ключ дедупа + field-extraction) дошли до прода незамеченными.

Эта фикстура ведётся ОТ ОФИЦИАЛЬНОГО образца Adapty (first-party документация, на которую
ссылается ADR-040/041 §Источник):
    https://adapty.io/docs/webhook-event-types-and-fields  (сверка 2026-07-10)

Установленные факты формы (ADR-040 факт №1/№2/№5, ADR-041 факт №1–5), закодированные здесь:
  * В payload НЕТ верхнеуровневых `event_id` / `id`. Идентификатор события —
    `event_properties.profile_event_id` (UUID).
  * НЕТ верхнеуровневых объектов `profile` / `subscription`. Верхний уровень — ПЛОСКИЙ.
  * `profile_event_id`, `subscription_expires_at`, `will_renew`, `is_active`,
    `is_in_grace_period`, `is_refund`, `vendor_product_id`, `transaction_id`,
    `original_transaction_id`, `store` — ВНУТРИ `event_properties`.
  * `customer_user_id`, `event_type`, `profile_id`, `email`, `idfa`, `idfv`,
    `advertising_id` — на ВЕРХНЕМ уровне.

`assert_official_shape()` — гард: любой собранный/переданный в тест payload обязан быть
официальной формы (нет `event_id`/`id`/`profile`/`subscription`), чтобы будущие тесты
сверялись с реальной формой провайдера, а не с нашей.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

OFFICIAL_SOURCE = (
    "https://adapty.io/docs/webhook-event-types-and-fields (сверка 2026-07-10, ADR-040/041)"
)

# Полный фактический перечень event_type Adapty (18) — ADR-040 §Источник факт №5.
KNOWN_EVENT_TYPES_18: tuple[str, ...] = (
    "subscription_started",
    "subscription_renewed",
    "subscription_renewal_cancelled",
    "subscription_renewal_reactivated",
    "subscription_expired",
    "subscription_paused",
    "subscription_deferred",
    "non_subscription_purchase",
    "trial_started",
    "trial_converted",
    "trial_renewal_cancelled",
    "trial_renewal_reactivated",
    "trial_expired",
    "entered_grace_period",
    "billing_issue_detected",
    "subscription_refunded",
    "non_subscription_purchase_refunded",
    "access_level_updated",
)

# Сентинел «поле отсутствует» — отличаем «не передано» от «передано None/False».
_OMIT: Any = object()
# Сентинел «сгенерировать свежий UUID» для profile_event_id по умолчанию.
_AUTO: Any = object()

# Верхнеуровневые ключи, которых в официальной форме payload быть НЕ должно.
_FORBIDDEN_TOP_LEVEL = ("event_id", "id", "profile", "subscription")


def new_profile_event_id() -> str:
    """Свежий UUID (тип profile_event_id у Adapty — UUID, факт №1)."""
    return str(uuid.uuid4())


def make_event_properties(
    *,
    profile_event_id: Any = _AUTO,
    subscription_expires_at: Any = _OMIT,
    expires_at: Any = _OMIT,
    will_renew: Any = _OMIT,
    is_active: Any = _OMIT,
    is_in_grace_period: Any = _OMIT,
    is_refund: Any = _OMIT,
    vendor_product_id: Any = _OMIT,
    product_id: Any = _OMIT,
    transaction_id: Any = _OMIT,
    original_transaction_id: Any = _OMIT,
    store: Any = _OMIT,
    price: Any = _OMIT,
    currency: Any = _OMIT,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Собирает `event_properties` официальной формы (все подписочные/платёжные поля здесь).

    `profile_event_id` по умолчанию — свежий UUID; передайте `None` (или `_OMIT` через
    отдельный флаг билдера), чтобы смоделировать его ОТСУТСТВИЕ (fallback тир-2/3).
    Прочие поля включаются только если явно переданы (варьирование `event_properties` по
    типу события — факт №3).
    """
    props: dict[str, Any] = {}
    if profile_event_id is _AUTO:
        props["profile_event_id"] = new_profile_event_id()
    elif profile_event_id is not _OMIT and profile_event_id is not None:
        props["profile_event_id"] = profile_event_id

    for key, value in (
        ("subscription_expires_at", subscription_expires_at),
        ("expires_at", expires_at),
        ("will_renew", will_renew),
        ("is_active", is_active),
        ("is_in_grace_period", is_in_grace_period),
        ("is_refund", is_refund),
        ("vendor_product_id", vendor_product_id),
        ("product_id", product_id),
        ("transaction_id", transaction_id),
        ("original_transaction_id", original_transaction_id),
        ("store", store),
        ("price", price),
        ("currency", currency),
    ):
        if value is not _OMIT:
            props[key] = value

    if extra:
        props.update(extra)
    return props


def make_webhook_payload(
    *,
    event_type: str,
    customer_user_id: str | None = "u_adapty_default00001",
    profile_id: str = "a1b2c3d4-0000-4000-8000-000000000001",
    event_properties: dict[str, Any] | None = None,
    include_pii: bool = True,
    top_level_extra: dict[str, Any] | None = None,
    omit_customer_user_id: bool = False,
) -> dict[str, Any]:
    """Собирает payload вебхука Adapty ОФИЦИАЛЬНОЙ (плоской) формы.

    Гарантии формы (ADR-040/041 §Источник):
      * НЕТ верхнеуровневых `event_id`/`id`;
      * НЕТ объектов `profile`/`subscription`;
      * `event_type`/`customer_user_id`/`profile_id`/PII — верхний уровень;
      * все подписочные/платёжные поля — внутри `event_properties`.

    `include_pii=True` добавляет верхнеуровневые `email`/`idfa`/`idfv`/`advertising_id`
    (реальный payload их несёт) — используется тестом безопасности логов (эти значения
    НЕ должны попадать в диагностику, ADR-041 §D / 05-security).
    """
    payload: dict[str, Any] = {
        "profile_id": profile_id,
        "event_type": event_type,
        "event_datetime": "2026-07-10T12:00:00.000000+0000",
        "event_api_version": 1,
        "event_properties": event_properties if event_properties is not None else {},
    }
    if not omit_customer_user_id:
        payload["customer_user_id"] = customer_user_id
    if include_pii:
        payload.update(
            {
                "email": "buyer-pii@example.com",
                "idfa": "IDFA-00000000-0000-0000-0000-PIIVALUE",
                "idfv": "IDFV-11111111-1111-1111-1111-PIIVALUE",
                "advertising_id": "ADID-22222222-PIIVALUE",
                "profile_install_datetime": "2026-01-01T00:00:00.000000+0000",
                "user_agent": "Adapty/1.0",
            }
        )
    if top_level_extra:
        payload.update(top_level_extra)
    assert_official_shape(payload)
    return payload


def subscription_event(
    event_type: str,
    customer_user_id: str | None,
    *,
    profile_event_id: Any = _AUTO,
    subscription_expires_at: Any = _OMIT,
    will_renew: Any = _OMIT,
    vendor_product_id: Any = _OMIT,
    transaction_id: Any = _OMIT,
    store: Any = _OMIT,
    include_pii: bool = False,
    omit_customer_user_id: bool = False,
    extra_event_properties: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Удобный билдер подписочного/consumable события официальной формы."""
    props = make_event_properties(
        profile_event_id=profile_event_id,
        subscription_expires_at=subscription_expires_at,
        will_renew=will_renew,
        vendor_product_id=vendor_product_id,
        transaction_id=transaction_id,
        store=store,
        extra=extra_event_properties,
    )
    return make_webhook_payload(
        event_type=event_type,
        customer_user_id=customer_user_id,
        event_properties=props,
        include_pii=include_pii,
        omit_customer_user_id=omit_customer_user_id,
    )


def access_level_updated_event(
    customer_user_id: str | None,
    *,
    is_active: bool,
    is_in_grace_period: Any = _OMIT,
    is_refund: Any = _OMIT,
    profile_event_id: Any = _AUTO,
    expires_at: Any = _OMIT,
    will_renew: Any = _OMIT,
    include_pii: bool = False,
) -> dict[str, Any]:
    """Билдер `access_level_updated` — уровень-доступа-поля внутри `event_properties` (факт №4)."""
    props = make_event_properties(
        profile_event_id=profile_event_id,
        is_active=is_active,
        is_in_grace_period=is_in_grace_period,
        is_refund=is_refund,
        expires_at=expires_at,
        will_renew=will_renew,
    )
    return make_webhook_payload(
        event_type="access_level_updated",
        customer_user_id=customer_user_id,
        event_properties=props,
        include_pii=include_pii,
    )


def to_body(payload: dict[str, Any]) -> bytes:
    """Сериализует payload в детерминированные байты тела (для tier-3 хэша ключа дедупа)."""
    return json.dumps(payload).encode("utf-8")


def assert_official_shape(payload: dict[str, Any]) -> None:
    """Гард: payload — официальной формы Adapty (нет `event_id`/`id`/`profile`/`subscription`).

    Защищает от рецидива root cause: self-consistent payload по нашей схеме. Любой тест,
    строящий payload вручную, обязан пройти этот гард (или использовать билдеры выше).
    """
    for key in _FORBIDDEN_TOP_LEVEL:
        assert key not in payload, (
            f"payload содержит запрещённый верхнеуровневый ключ {key!r} — это НЕ официальная "
            f"форма Adapty ({OFFICIAL_SOURCE}); дедуп-ключ и field-extraction должны читаться "
            f"из event_properties/верхнего уровня, а не из {key!r}."
        )
