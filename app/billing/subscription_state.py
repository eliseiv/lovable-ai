"""Применение состояния подписки к локальному кэшу subscriptions.

Общая логика для вебхука (app/billing/webhook_handler), периодического и lazy-ресинка
(app/billing/resync). docs/modules/billing/03-architecture.md §2.3 (нормативная таблица
маппинга event_type → subscriptions) и §3 (ресинк не перетирает более свежее вебхук-
состояние). Источник истины — Adapty (ADR-004/009).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.adapty_client import AdaptyProfile
from app.core.config import get_settings
from app.core.ids import new_subscription_id
from app.core.logging import get_logger
from app.db.models import Subscription

logger = get_logger(__name__)

# Adapty webhook v2 event types (docs/03-data-model.md → billing_events.event_type).
EVENT_STARTED = "subscription_started"
EVENT_RENEWED = "subscription_renewed"
EVENT_EXPIRED = "subscription_expired"
EVENT_REFUNDED = "subscription_refunded"
EVENT_BILLING_ISSUE = "billing_issue_detected"
EVENT_ACCESS_LEVEL_UPDATED = "access_level_updated"

# Статусы subscriptions.status.
STATUS_ACTIVE = "active"
STATUS_EXPIRED = "expired"
STATUS_GRACE = "grace"
STATUS_BILLING_ISSUE = "billing_issue"

# Статусы, проходящие quota-gate (docs §4).
GATE_PASS_STATUSES: frozenset[str] = frozenset({STATUS_ACTIVE, STATUS_GRACE})

DEFAULT_ACCESS_LEVEL = "free"


def _parse_ts(value: Any) -> datetime | None:
    """ISO-8601 строка → aware datetime (UTC). None при пустом/невалидном значении."""
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


async def get_subscription(session: AsyncSession, user_id: str) -> Subscription | None:
    """Строка-кэш подписки пользователя (одна на user_id) или None."""
    result = await session.execute(select(Subscription).where(Subscription.user_id == user_id))
    return result.scalar_one_or_none()


async def get_subscription_for_update(session: AsyncSession, user_id: str) -> Subscription | None:
    """Строка подписки под SELECT ... FOR UPDATE (гонка renew↔sweep, docs §6)."""
    result = await session.execute(
        select(Subscription).where(Subscription.user_id == user_id).with_for_update()
    )
    return result.scalar_one_or_none()


def _ensure_row(session: AsyncSession, user_id: str, existing: Subscription | None) -> Subscription:
    if existing is not None:
        return existing
    sub = Subscription(
        id=new_subscription_id(),
        user_id=user_id,
        access_level=DEFAULT_ACCESS_LEVEL,
        status=STATUS_ACTIVE,
        will_renew=False,
        raw={},
    )
    session.add(sub)
    return sub


async def apply_webhook_event(
    session: AsyncSession,
    *,
    user_id: str,
    event_type: str,
    profile: dict[str, Any],
    subscription_payload: dict[str, Any],
    raw_payload: dict[str, Any],
) -> Subscription:
    """Применяет событие вебхука к subscriptions по нормативной таблице (docs §2.3).

    Коммит — на стороне вызывающего (одна транзакция с billing_events). synced_at
    выставляется в now() — отметка свежести вебхук-состояния (приоритет над ресинком §3).
    """
    settings = get_settings()
    existing = await get_subscription(session, user_id)
    sub = _ensure_row(session, user_id, existing)

    profile_access_level = profile.get("access_level")
    profile_is_active = bool(profile.get("is_active", False))
    expires_at = _parse_ts(subscription_payload.get("expires_at"))
    started_at = _parse_ts(subscription_payload.get("started_at"))
    grace_days = timedelta(days=settings.grace_period_days)
    now = datetime.now(UTC)

    # Общие поля из payload (где применимо).
    if subscription_payload.get("product_id"):
        sub.product_id = subscription_payload["product_id"]
    if subscription_payload.get("store"):
        sub.store = subscription_payload["store"]
    if subscription_payload.get("transaction_id"):
        sub.adapty_transaction_id = subscription_payload["transaction_id"]

    if event_type in (EVENT_STARTED, EVENT_RENEWED):
        # started/renewed → active; renew/start в grace/billing_issue → grace_until=NULL.
        sub.status = STATUS_ACTIVE
        if profile_access_level:
            sub.access_level = profile_access_level
        sub.grace_until = None
        if expires_at is not None:
            sub.expires_at = expires_at
        if started_at is not None:
            sub.started_at = started_at
        sub.will_renew = bool(subscription_payload.get("will_renew", True))
    elif event_type == EVENT_ACCESS_LEVEL_UPDATED:
        # Новый уровень из профиля; status=active если профиль активен.
        if profile_access_level:
            sub.access_level = profile_access_level
        if profile_is_active:
            sub.status = STATUS_ACTIVE
            sub.grace_until = None
    elif event_type == EVENT_EXPIRED:
        # expired → grace, grace_until = expires_at + GRACE_PERIOD_DAYS. access_level
        # сохраняется (grace проходит гейт §4). will_renew=false.
        sub.status = STATUS_GRACE
        base = expires_at or sub.expires_at or now
        sub.grace_until = base + grace_days
        sub.will_renew = False
    elif event_type == EVENT_REFUNDED:
        # refunded → grace, grace_until = now() + GRACE_PERIOD_DAYS.
        sub.status = STATUS_GRACE
        sub.grace_until = now + grace_days
        sub.will_renew = False
    elif event_type == EVENT_BILLING_ISSUE:
        # billing_issue → НЕ-активный на гейте (§4). grace_until=NULL.
        sub.status = STATUS_BILLING_ISSUE
        sub.grace_until = None
        sub.will_renew = bool(subscription_payload.get("will_renew", sub.will_renew))
    else:
        # Неизвестный event_type: не меняем status, фиксируем raw (для аудита/алерта).
        logger.warning(
            "billing_unknown_event_type", extra={"event_type": event_type, "user_id": user_id}
        )

    sub.raw = raw_payload
    sub.synced_at = now
    return sub


def apply_profile_resync(sub: Subscription, profile: AdaptyProfile) -> None:
    """Апдейт subscriptions из getProfile-профиля (идемпотентно, upsert по user_id, docs §3).

    Не трогает grace-ветку напрямую: ресинк отражает access_level/active-состояние из
    Adapty. Если профиль активен — active; если неактивен и подписка не в grace —
    expired. Подписку в grace/billing_issue ресинк НЕ форсит в expired (teardown — дело
    sweep по grace_until); обновляет лишь access_level/expires/will_renew + synced_at.
    grace_until не меняется ресинком (управляется вебхуком/sweep).
    """
    sub.access_level = profile.access_level if profile.is_active else sub.access_level
    if profile.product_id:
        sub.product_id = profile.product_id
    if profile.store:
        sub.store = profile.store
    expires_at = _parse_ts(profile.expires_at)
    if expires_at is not None:
        sub.expires_at = expires_at
    started_at = _parse_ts(profile.started_at)
    if started_at is not None:
        sub.started_at = started_at
    if profile.transaction_id:
        sub.adapty_transaction_id = profile.transaction_id
    sub.will_renew = profile.will_renew

    if profile.is_active:
        # Активный профиль → active (renew/возврат прав). Отменяет pending-teardown.
        sub.status = STATUS_ACTIVE
        sub.grace_until = None
    elif sub.status not in (STATUS_GRACE, STATUS_BILLING_ISSUE):
        # Неактивный профиль и подписка не под grace/billing_issue → expired.
        sub.status = STATUS_EXPIRED

    sub.raw = profile.raw
    sub.synced_at = datetime.now(UTC)
