"""RU-оплата через платёжный агрегатор CloudPayments/YooKassa (ADR-052).

Схема повторяет проверенную на соседнем проекте (`claude-ios`, его ADR-050/051/054):

1. **Checkout** — клиент с обычным Bearer'ом просит ссылку на оплату; сервер вызывает
   агрегатор `POST {base}/payments/link` со своим серверным токеном и подставляет `user_id`
   **только** из аутентифицированного пользователя, никогда из тела запроса. Иначе платёж
   уходит на чужой аккаунт и «теряется».
2. **Callback** — агрегатор дергает наш вебхук **без подписи и без авторизации**. Поэтому телу
   callback'а не верят: оно лишь ТРИГГЕР. Начисление выполняется только после сверки платежей
   пользователя через API агрегатора (`GET {base}/users/{user_id}/payments`) нашим токеном.
3. **Начисление** — суммы берутся из серверных настроек по `product.code`
   (`TOKEN_PACK_PRODUCTS`, `SUBSCRIPTION_PRODUCT_*`), НИКОГДА из поля `amount` агрегатора:
   иначе подделанный callback или скомпрометированный ответ определял бы, сколько выдать.

Идемпотентность — по `payment_id` агрегатора (`cloudpayments_payments`), а не по id события:
один callback сверяет список платежей, и ключ обязан быть per-payment.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.subscription_state import (
    apply_admin_grant,
    grant_tokens,
    resolve_consumable_tokens,
)
from app.core.config import Settings
from app.core.logging import get_logger
from app.db.models import CloudPaymentsPayment, User

logger = get_logger(__name__)

# Таймаут исходящих вызовов агрегатора: короткий бюджет партнёрского API. Отдельного env нет —
# трёх ключей CLOUDPAYMENTS_* достаточно.
HTTP_TIMEOUT_SECONDS = 15.0

# Класс платежа, приходящий от агрегатора в `product.payment_type`.
KIND_TOKENS = "tokens"
KIND_SUBSCRIPTION = "subscription"

# Длительность подписки по коду продукта: срок выводится из имени, потому что агрегатор не
# сообщает период. Первое совпадение выигрывает.
_SUBSCRIPTION_DAYS: tuple[tuple[str, int], ...] = (
    ("year", 365),
    ("annual", 365),
    ("month", 30),
    ("week", 7),
    ("day", 1),
)

# Идентификаторы пользователя подставляются в URL агрегатора, поэтому формат проверяется
# строго: только наш opaque-формат (`u_` + base32-подобный хвост). Это же отсекает
# path-traversal и SSRF-подстановки из тела callback'а.
_USER_ID_RE = re.compile(r"^u_[a-z0-9]{1,64}$")


class CloudPaymentsNotConfigured(RuntimeError):
    """RU-оплата не настроена на инстансе (нет базы/app_id/токена)."""


class CloudPaymentsUnavailable(RuntimeError):
    """Транзиентный сбой агрегатора: вебхук → 500, агрегатор повторит доставку."""


class CloudPaymentsUpstreamError(RuntimeError):
    """Агрегатор не выдал ссылку на оплату (checkout → 502, без деталей наружу)."""


@dataclass(frozen=True)
class CheckoutLink:
    """Ответ агрегатора на создание платёжной ссылки — отдаётся клиенту как есть."""

    payment_id: str
    payment_url: str
    status: str
    expires_at: str | None


@dataclass(frozen=True)
class CreditablePayment:
    """Платёж агрегатора, прошедший сверку и готовый к начислению."""

    payment_id: str
    product_code: str
    payment_type: str
    status: str
    paid_at: datetime


@dataclass(frozen=True)
class WebhookOutcome:
    """Итог обработки одного callback'а. Роутер отвечает `200 {"code": 0}` в любом случае."""

    result: str  # "applied" | "duplicate" | "ignored"
    reason: str | None = None
    credited: int = 0


def _headers(settings: Settings) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.cloudpayments_api_token}",
        "Accept": "application/json",
    }


def valid_user_id(value: Any) -> str | None:
    """Наш `user_id` из произвольного значения, если он валиден; иначе `None`."""
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    return candidate if _USER_ID_RE.match(candidate) else None


def extract_user_id(raw_body: bytes) -> str | None:
    """Идентификатор пользователя из сырого тела callback'а (ADR-052 §B).

    Тело читается сырым и разбирается терпимо: агрегатор шлёт JSON, но кривое тело обязано
    дать `ignored`, а не `422` — иначе агрегатор будет ретраить заведомо неисправимое.
    Значение служит только адресом для сверки: сами суммы берутся из подтверждённых данных.
    """
    if not raw_body:
        return None
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    # Разные агрегаторы называют поле по-разному; проверяем известные варианты в фиксированном
    # порядке, первое валидное значение выигрывает.
    for key in ("user_id", "userId", "AccountId", "account_id"):
        found = valid_user_id(payload.get(key))
        if found is not None:
            return found
    return None


async def create_payment_link(
    settings: Settings, *, user_id: str, product_id: str, customer_email: str
) -> CheckoutLink:
    """Создаёт ссылку на оплату у агрегатора (`POST {base}/payments/link`).

    `user_id` — аутентифицированный пользователь, а не значение из тела запроса: именно по
    нему callback потом найдёт, кому начислять. Любой сбой апстрима превращается в
    `CloudPaymentsUpstreamError` без утечки его тела/статуса и нашего токена.
    """
    if not settings.cloudpayments_configured():
        raise CloudPaymentsNotConfigured("cloudpayments is not configured")

    url = f"{settings.cloudpayments_api_base.rstrip('/')}/payments/link"
    # multipart/form-data через files= — Content-Type с boundary проставляет httpx сам.
    files: dict[str, tuple[None, str]] = {
        "app_id": (None, settings.cloudpayments_app_id),
        "product_id": (None, product_id),
        "user_id": (None, user_id),
        "customer_email": (None, customer_email),
    }
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            response = await client.post(url, files=files, headers=_headers(settings))
    except httpx.HTTPError as exc:
        logger.warning("cloudpayments_checkout_failed", extra={"reason": "transport"})
        raise CloudPaymentsUpstreamError("payment provider unavailable") from exc

    if not (200 <= response.status_code < 300):
        logger.warning("cloudpayments_checkout_failed", extra={"reason": "upstream_status"})
        raise CloudPaymentsUpstreamError("payment provider unavailable")

    try:
        body = response.json()
    except (ValueError, UnicodeDecodeError) as exc:
        logger.warning("cloudpayments_checkout_failed", extra={"reason": "malformed_response"})
        raise CloudPaymentsUpstreamError("payment provider unavailable") from exc

    payment_url = body.get("payment_url") if isinstance(body, dict) else None
    if not isinstance(payment_url, str) or not payment_url:
        logger.warning("cloudpayments_checkout_failed", extra={"reason": "no_payment_url"})
        raise CloudPaymentsUpstreamError("payment provider unavailable")

    expires_at = body.get("expires_at")
    link = CheckoutLink(
        payment_id=str(body.get("payment_id") or ""),
        payment_url=payment_url,
        status=str(body.get("status") or ""),
        expires_at=expires_at if isinstance(expires_at, str) else None,
    )
    logger.info(
        "cloudpayments_checkout_created",
        extra={"user_id": user_id, "product_id": product_id, "payment_id": link.payment_id},
    )
    return link


async def list_payments(settings: Settings, *, user_id: str) -> list[dict[str, Any]]:
    """Платежи пользователя у агрегатора (`GET {base}/users/{user_id}/payments`).

    `404` — «платежей нет» (постоянный ответ, не ошибка) → пустой список. Любой транзиентный
    сбой → `CloudPaymentsUnavailable`, чтобы агрегатор повторил доставку callback'а, а не
    считал платёж обработанным.
    """
    if not settings.cloudpayments_configured():
        raise CloudPaymentsNotConfigured("cloudpayments is not configured")

    url = f"{settings.cloudpayments_api_base.rstrip('/')}/users/{user_id}/payments"
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            response = await client.get(url, headers=_headers(settings))
    except httpx.HTTPError as exc:
        raise CloudPaymentsUnavailable("verification unavailable") from exc

    if response.status_code == 404:
        return []
    if not (200 <= response.status_code < 300):
        raise CloudPaymentsUnavailable("verification unavailable")

    try:
        body = response.json()
    except (ValueError, UnicodeDecodeError) as exc:
        raise CloudPaymentsUnavailable("verification unavailable") from exc
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list):
        raise CloudPaymentsUnavailable("verification unavailable")
    return [item for item in data if isinstance(item, dict)]


def _parse_paid_at(value: Any) -> datetime | None:
    """ISO-8601 → aware UTC; нераспознанное → `None` (платёж пропускается, не начисляется)."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def select_creditable_payments(
    data: list[dict[str, Any]],
    *,
    paid_statuses: frozenset[str],
    now: datetime,
    freshness_hours: int,
) -> list[CreditablePayment]:
    """Чистая сверка: какие платежи из ответа агрегатора можно начислять (ADR-052 §C).

    Платёж проходит, только если выполнено ВСЁ: статус входит в набор «оплачено», оплата
    свежее окна, и есть непустые `payment_id`, `product.code`, `product.payment_type`. Без
    I/O и мутаций — проверяется отдельно от сети и БД.
    """
    cutoff = now - timedelta(hours=freshness_hours)
    creditable: list[CreditablePayment] = []
    for item in data:
        status = str(item.get("status") or "").strip().lower()
        if status not in paid_statuses:
            continue
        paid_at = _parse_paid_at(item.get("paid_at"))
        if paid_at is None or paid_at < cutoff:
            continue
        payment_id = item.get("payment_id")
        if not isinstance(payment_id, str) or not payment_id.strip():
            continue
        product = item.get("product")
        if not isinstance(product, dict):
            continue
        code = product.get("code")
        payment_type = product.get("payment_type")
        if not isinstance(code, str) or not code.strip():
            continue
        if not isinstance(payment_type, str) or not payment_type.strip():
            continue
        creditable.append(
            CreditablePayment(
                payment_id=payment_id.strip(),
                product_code=code.strip(),
                payment_type=payment_type.strip().lower(),
                status=status,
                paid_at=paid_at,
            )
        )
    return creditable


def subscription_days(product_code: str) -> int:
    """Срок подписки по коду продукта; неизвестный код → 30 дней.

    Агрегатор не сообщает период, поэтому он выводится из имени (`week_6.99_nottrial` → 7).
    Дефолт «месяц» выбран как самый частый период: он не даёт бесконечного доступа при
    неизвестном коде и не обесценивает годовую покупку до одного дня.
    """
    lowered = product_code.lower()
    for keyword, days in _SUBSCRIPTION_DAYS:
        if keyword in lowered:
            return days
    return 30


def classify(payment: CreditablePayment, settings: Settings) -> str:
    """Класс начисления: `tokens` | `subscription`.

    Первым выигрывает подтверждённый `payment_type` агрегатора; если он невнятен — код
    продукта сверяется с картой токен-паков (`TOKEN_PACK_PRODUCTS`), иначе это подписка.
    """
    if payment.payment_type in {"one_time", "onetime", "one-time", KIND_TOKENS}:
        return KIND_TOKENS
    if payment.payment_type in {"subscription", "recurrent", "recurring"}:
        return KIND_SUBSCRIPTION
    return (
        KIND_TOKENS
        if resolve_consumable_tokens(payment.product_code, settings) is not None
        else KIND_SUBSCRIPTION
    )


async def _already_processed(session: AsyncSession, payment_id: str) -> bool:
    row = await session.get(CloudPaymentsPayment, payment_id)
    return row is not None


async def credit_payment(
    session: AsyncSession, settings: Settings, *, user: User, payment: CreditablePayment
) -> WebhookOutcome:
    """Начисляет один подтверждённый платёж. Коммит — на стороне вызывающего.

    Сумма берётся из серверных настроек по коду продукта, а не из данных агрегатора
    (анти-подмена). Повторная обработка того же `payment_id` — `duplicate` без начисления.
    """
    if await _already_processed(session, payment.payment_id):
        return WebhookOutcome(result="duplicate", reason="already_processed")

    kind = classify(payment, settings)
    tokens = 0
    if kind == KIND_TOKENS:
        amount = resolve_consumable_tokens(payment.product_code, settings)
        if amount is None or amount <= 0:
            # Пак, которого нет в TOKEN_PACK_PRODUCTS: начислять нечего, но платёж фиксируем —
            # иначе каждый следующий callback снова пытался бы его обработать.
            logger.warning(
                "cloudpayments_unknown_product",
                extra={"product_id": payment.product_code, "user_id": user.id},
            )
        else:
            tokens = await grant_tokens(
                session,
                user_id=user.id,
                event_id=payment.payment_id,
                event_type="cloudpayments_purchase",
                amount=amount,
                created_by="cloudpayments",
                reason=f"cloudpayments:{payment.product_code}",
                idempotency_key=f"cloudpayments:{payment.payment_id}",
            )
    else:
        now = datetime.now(UTC)
        sub = await apply_admin_grant(
            session,
            user_id=user.id,
            expires_at=now + timedelta(days=subscription_days(payment.product_code)),
        )
        # Происхождение подписки видно в карточке и при разборе инцидентов: RU-канал, а не
        # ручная выдача оператором и не App Store.
        sub.product_id = payment.product_code
        sub.store = "cloudpayments"

    session.add(
        CloudPaymentsPayment(
            payment_id=payment.payment_id,
            user_id=user.id,
            product_id=payment.product_code,
            kind=kind,
            tokens_granted=tokens,
            paid_at=payment.paid_at,
        )
    )
    logger.info(
        "cloudpayments_payment_applied",
        extra={
            "user_id": user.id,
            "payment_id": payment.payment_id,
            "product_id": payment.product_code,
            "kind": kind,
            "tokens": tokens,
        },
    )
    return WebhookOutcome(result="applied", credited=tokens)


async def handle_webhook(
    session: AsyncSession, settings: Settings, raw_body: bytes
) -> WebhookOutcome:
    """Обрабатывает callback: триггер → сверка у агрегатора → начисление (ADR-052 §B).

    Возвращает исход; HTTP-код выбирает роутер. Не поднимает исключений на «плохих» телах —
    только на том, что должно привести к повтору доставки: не сконфигурировано и недоступна
    сверка.
    """
    if not settings.cloudpayments_configured():
        raise CloudPaymentsNotConfigured("cloudpayments is not configured")

    user_id = extract_user_id(raw_body)
    if user_id is None:
        return WebhookOutcome(result="ignored", reason="no_user_id")

    user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        logger.warning("cloudpayments_user_not_found", extra={"user_id": user_id})
        return WebhookOutcome(result="ignored", reason="user_not_found")

    payments = await list_payments(settings, user_id=user_id)
    creditable = select_creditable_payments(
        payments,
        paid_statuses=settings.cloudpayments_paid_status_set(),
        now=datetime.now(UTC),
        freshness_hours=settings.cloudpayments_payment_freshness_hours,
    )
    if not creditable:
        return WebhookOutcome(result="ignored", reason="no_creditable_payment")

    applied = 0
    credited = 0
    for payment in creditable:
        outcome = await credit_payment(session, settings, user=user, payment=payment)
        if outcome.result == "applied":
            applied += 1
            credited += outcome.credited
    await session.commit()

    if applied == 0:
        return WebhookOutcome(result="duplicate", reason="already_processed")
    return WebhookOutcome(result="applied", credited=credited)


__all__ = [
    "KIND_SUBSCRIPTION",
    "KIND_TOKENS",
    "CheckoutLink",
    "CloudPaymentsNotConfigured",
    "CloudPaymentsUnavailable",
    "CloudPaymentsUpstreamError",
    "CreditablePayment",
    "WebhookOutcome",
    "classify",
    "create_payment_link",
    "credit_payment",
    "extract_user_id",
    "handle_webhook",
    "list_payments",
    "select_creditable_payments",
    "subscription_days",
    "valid_user_id",
]
