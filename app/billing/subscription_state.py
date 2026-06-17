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
from app.core.config import Settings, get_settings
from app.core.ids import new_credit_grant_id, new_subscription_id
from app.core.logging import get_logger
from app.db.models import CreditGrant, Subscription

logger = get_logger(__name__)

# Adapty webhook v2 event types (docs/03-data-model.md → billing_events.event_type).
EVENT_STARTED = "subscription_started"
EVENT_RENEWED = "subscription_renewed"
EVENT_EXPIRED = "subscription_expired"
EVENT_REFUNDED = "subscription_refunded"
EVENT_BILLING_ISSUE = "billing_issue_detected"
EVENT_ACCESS_LEVEL_UPDATED = "access_level_updated"
EVENT_CANCELLED = "subscription_cancelled"

# Известные event_type Adapty (docs §2.3). Неизвестный → 200 ignored (event_type).
KNOWN_EVENT_TYPES: frozenset[str] = frozenset(
    {
        EVENT_STARTED,
        EVENT_RENEWED,
        EVENT_EXPIRED,
        EVENT_REFUNDED,
        EVENT_BILLING_ISSUE,
        EVENT_ACCESS_LEVEL_UPDATED,
        EVENT_CANCELLED,
    }
)

# event_type → token-grant начисляется (docs §11.2: только started/renewed).
TOKEN_GRANT_EVENT_TYPES: frozenset[str] = frozenset({EVENT_STARTED, EVENT_RENEWED})

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
    elif event_type == EVENT_CANCELLED:
        # cancelled (ADR-027 §F): «не продлится» — will_renew=false. status/access_level/
        # grace_until НЕ меняем (teardown позже по subscription_expired). Токены не трогаем.
        sub.will_renew = False
    else:
        # Неизвестный event_type: не меняем status, фиксируем raw (для аудита/алерта).
        logger.warning(
            "billing_unknown_event_type", extra={"event_type": event_type, "user_id": user_id}
        )

    sub.raw = raw_payload
    sub.synced_at = now
    return sub


async def apply_admin_grant(
    session: AsyncSession,
    *,
    user_id: str,
    expires_at: datetime | None,
) -> Subscription:
    """Ставит pro-доступ выбранному юзеру напрямую (admin-grant, docs §12.1, ADR-037 §B).

    Переиспускает _ensure_row (одна строка на user_id, idempotent upsert; повтор = обновление
    срока) — НЕ прямой upsert, чтобы не дублировать state-machine §2.3. Коммит — на стороне
    вызывающего (admin_service, одна транзакция). Токены НЕ начисляются
    (bonus_generations_balance/credit_grants/billing_events не трогаются — отличие от §11).
    adapty_transaction_id не трогается.

    Поля: access_level=pro, status=active (проходит гейт §4), grace_until=NULL,
    will_renew=false (admin-grant не автопродляется), expires_at из параметра (или NULL —
    бессрочно, §12.2), started_at=now() если не задан (существующий сохраняется),
    synced_at=now(), store='admin' (маркер происхождения), product_id=NULL,
    raw={source:'admin_grant', granted_at, expires_at}.
    """
    existing = await get_subscription(session, user_id)
    sub = _ensure_row(session, user_id, existing)

    now = datetime.now(UTC)
    sub.access_level = "pro"
    sub.status = STATUS_ACTIVE
    sub.grace_until = None
    sub.will_renew = False
    sub.expires_at = expires_at
    if sub.started_at is None:
        sub.started_at = now
    sub.synced_at = now
    sub.store = "admin"
    sub.product_id = None
    sub.raw = {
        "source": "admin_grant",
        "granted_at": now.isoformat(),
        "expires_at": expires_at.isoformat() if expires_at is not None else None,
    }
    return sub


def resolve_tier_tokens(vendor_product_id: str | None, settings: Settings) -> int:
    """Число токенов (кредитов) для начисления по тиру vendor_product_id (docs §11.1).

    `== SUBSCRIPTION_PRODUCT_WEEKLY` → `SUBSCRIPTION_TOKENS_WEEKLY`;
    `== SUBSCRIPTION_PRODUCT_YEARLY` → `SUBSCRIPTION_TOKENS_YEARLY`;
    иной/неизвестный SKU → fallback `SUBSCRIPTION_TOKENS_GRANT` (начисление не теряется).
    """
    if vendor_product_id and vendor_product_id == settings.subscription_product_weekly:
        return settings.subscription_tokens_weekly
    if vendor_product_id and vendor_product_id == settings.subscription_product_yearly:
        return settings.subscription_tokens_yearly
    return settings.subscription_tokens_grant


async def grant_subscription_tokens(
    session: AsyncSession,
    *,
    user_id: str,
    event_id: str,
    event_type: str,
    vendor_product_id: str | None,
) -> int:
    """Начисляет пакет генераций по тиру подписки (ADR-027 §E, docs §11.2).

    Относительный атомарный UPDATE users.bonus_generations_balance += tier_tokens (та же
    механика, что admin `_apply_balance_delta`) + insert credit_grants(created_by='adapty',
    idempotency_key=event_id) — БЕЗ commit (вызывающий коммитит в ТОЙ ЖЕ транзакции, что
    insert billing_events). Дедуп начисления — UNIQUE billing_events.adapty_event_id (повтор
    event_id → 200 duplicate, сюда не доходит) + партиальный UNIQUE credit_grants(user_id,
    idempotency_key=event_id) как вторая страховка. Возвращает число начисленных токенов.
    """
    # Лениво: избегаем импорт-цикла billing↔services и переиспользуем существующую механику
    # относительного атомарного UPDATE (НЕ дублируем логику начисления, docs §11.2).
    from app.services.admin_service import _apply_balance_delta

    settings = get_settings()
    tier_tokens = resolve_tier_tokens(vendor_product_id, settings)

    session.add(
        CreditGrant(
            id=new_credit_grant_id(),
            user_id=user_id,
            amount=tier_tokens,
            reason=f"adapty:{event_type}",
            idempotency_key=event_id,
            created_by="adapty",
        )
    )
    # tier_tokens >= 0 (env-конфиг), инвариант balance >= 0 не нарушается → None не ожидается.
    await _apply_balance_delta(session, user_id, tier_tokens)
    logger.info(
        "billing_token_grant",
        extra={
            "user_id": user_id,
            "event_id": event_id,
            "vendor_product_id": vendor_product_id,
            "tier_tokens": tier_tokens,
        },
    )
    return tier_tokens


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
