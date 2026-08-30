"""CRM Admin API — сервисный слой (broad-crm контракт v1).

Агрегирует users/subscriptions/billing_events/generation_jobs для операторской CRM-панели.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from datetime import date as date_
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.api.errors import bad_request, not_found
from app.billing.subscription_state import STATUS_ACTIVE, STATUS_GRACE, apply_admin_grant
from app.core.config import Settings, get_settings
from app.core.ids import new_credit_grant_id
from app.db.models import (
    BillingEvent,
    CreditGrant,
    GenerationJob,
    LlmUsage,
    Project,
    StoreTransaction,
    Subscription,
    User,
)
from app.schemas.crm_admin import (
    CrmDailyCostItem,
    CrmDailyCostsResponse,
    CrmGrantSubscriptionResponse,
    CrmMediaAvgGenerationSec,
    CrmMediaStats,
    CrmMediaStatsBucket,
    CrmPaymentItem,
    CrmPaymentListResponse,
    CrmProductItem,
    CrmProductListResponse,
    CrmRequestItem,
    CrmRequestListResponse,
    CrmStatsResponse,
    CrmUserBalance,
    CrmUserDetailResponse,
    CrmUserListItem,
    CrmUserListResponse,
    CrmUserRevenue,
    CrmUserSubscription,
    format_utc,
)
from app.services.admin_service import _apply_balance_delta, _get_user

_PAYMENT_SUCCESS_EVENTS = frozenset(
    {
        "subscription_started",
        "subscription_renewed",
        "non_subscription_purchase",
        "trial_converted",
    }
)
_RENEWAL_EVENTS = frozenset({"subscription_renewed"})
_TERMINAL_OK_STATES = frozenset({"LIVE"})
_TERMINAL_FAIL_STATES = frozenset({"FAILED"})


@dataclass(frozen=True)
class _UserPaymentAgg:
    payments_count: int
    renewals_count: int
    payments_sum_usd: float
    last_payment_at: datetime | None
    last_payment_method: str | None
    is_paid: bool


def _subscription_active(sub: Subscription | None, *, now: datetime | None = None) -> bool:
    if sub is None:
        return False
    if sub.status not in {STATUS_ACTIVE, STATUS_GRACE}:
        return False
    if sub.access_level != "pro":
        return False
    if sub.expires_at is not None:
        ref = now or datetime.now(UTC)
        if sub.expires_at <= ref:
            return False
    return True


def _known_subscription_product_ids(settings: Settings) -> frozenset[str]:
    return frozenset({settings.subscription_product_weekly, settings.subscription_product_yearly})


def _product_display(product_id: str | None, settings: Settings) -> tuple[str | None, str | None]:
    if not product_id:
        return None, None
    if product_id == settings.subscription_product_weekly:
        return "Pro Weekly", "$6.99/week"
    if product_id == settings.subscription_product_yearly:
        return "Pro Yearly", "$49.99/year"
    pack_amount = settings.token_pack_map().get(product_id)
    if pack_amount is not None:
        return f"{pack_amount} tokens", None
    return product_id.replace("_", " ").title(), None


def _extract_event_properties(payload: dict[str, Any]) -> dict[str, Any]:
    props = payload.get("event_properties")
    return props if isinstance(props, dict) else {}


def _payment_amount_usd(props: dict[str, Any]) -> tuple[float, str]:
    price = props.get("price")
    currency = str(props.get("currency") or "USD").upper()
    try:
        amount = float(price) if price is not None else 0.0
    except (TypeError, ValueError):
        amount = 0.0
    if currency != "USD":
        return 0.0, currency
    return amount, currency


def _store_label(store: str | None) -> str | None:
    if not store:
        return None
    mapping = {
        "app_store": "App Store",
        "play_store": "Google Play",
        "storekit": "App Store",
        "adapty": "App Store",
    }
    return mapping.get(store.lower(), store.replace("_", " ").title())


def _payment_title(event_type: str, product_id: str | None) -> str:
    if event_type == "non_subscription_purchase":
        return f"Покупка токенов {product_id or ''}".strip()
    if event_type == "subscription_renewed":
        return f"Продление подписки {product_id or 'Pro'}".strip()
    if event_type == "subscription_started":
        return f"Подписка {product_id or 'Pro'}".strip()
    if event_type == "trial_converted":
        return f"Конверсия триала {product_id or 'Pro'}".strip()
    return event_type.replace("_", " ").title()


async def _payment_agg_for_user(session: AsyncSession, user_id: str) -> _UserPaymentAgg:
    events = (
        (
            await session.execute(
                select(BillingEvent)
                .where(
                    BillingEvent.user_id == user_id,
                    BillingEvent.event_type.in_(tuple(_PAYMENT_SUCCESS_EVENTS)),
                    BillingEvent.processed_at.is_not(None),
                )
                .order_by(BillingEvent.received_at.desc())
            )
        )
        .scalars()
        .all()
    )
    store_rows = (
        (
            await session.execute(
                select(StoreTransaction)
                .where(StoreTransaction.user_id == user_id)
                .order_by(StoreTransaction.created_at.desc())
            )
        )
        .scalars()
        .all()
    )

    payments_count = len(events) + len(store_rows)
    renewals_count = sum(1 for e in events if e.event_type in _RENEWAL_EVENTS)
    payments_sum = 0.0
    last_at: datetime | None = None
    last_method: str | None = None

    for event in events:
        props = _extract_event_properties(event.payload)
        amount, _ = _payment_amount_usd(props)
        payments_sum += amount
        if last_at is None or event.received_at > last_at:
            last_at = event.received_at
            last_method = _store_label(props.get("store") if props else None)

    for row in store_rows:
        if last_at is None or row.created_at > last_at:
            last_at = row.created_at
            last_method = "App Store"

    is_paid = payments_count > 0
    return _UserPaymentAgg(
        payments_count=payments_count,
        renewals_count=renewals_count,
        payments_sum_usd=payments_sum,
        last_payment_at=last_at,
        last_payment_method=last_method,
        is_paid=is_paid,
    )


def _list_item_from_row(
    user: User,
    sub: Subscription | None,
    agg: _UserPaymentAgg,
) -> CrmUserListItem:
    return CrmUserListItem(
        id=user.id,
        external_id=user.adapty_customer_user_id,
        is_paid=agg.is_paid,
        payments_count=agg.payments_count,
        renewals_count=agg.renewals_count,
        tokens=float(user.bonus_generations_balance),
        subscription_active=_subscription_active(sub),
        subscription_expires_at=format_utc(sub.expires_at if sub else None),
        plan_id=sub.product_id if sub else None,
        registered_at=format_utc(user.created_at) or "",
    )


async def list_users(
    session: AsyncSession,
    *,
    limit: int,
    offset: int,
    search: str | None,
    date_from: datetime | None,
    date_to: datetime | None,
    is_paid: bool | None,
) -> CrmUserListResponse:
    sub_alias = aliased(Subscription)
    base = (
        select(User, sub_alias)
        .outerjoin(sub_alias, sub_alias.user_id == User.id)
        .order_by(User.created_at.desc())
    )
    filters = []
    if search:
        pattern = f"%{search.strip()}%"
        filters.append(
            or_(
                User.id.ilike(pattern),
                User.adapty_customer_user_id.ilike(pattern),
            )
        )
    if date_from is not None:
        filters.append(User.created_at >= date_from)
    if date_to is not None:
        filters.append(User.created_at <= date_to)
    if filters:
        base = base.where(and_(*filters))

    rows = (await session.execute(base)).all()
    items: list[CrmUserListItem] = []
    for user, sub in rows:
        agg = await _payment_agg_for_user(session, user.id)
        if is_paid is not None and agg.is_paid != is_paid:
            continue
        items.append(_list_item_from_row(user, sub, agg))

    total = len(items)
    page = items[offset : offset + limit]
    return CrmUserListResponse(total=total, items=page)


async def get_user_detail(session: AsyncSession, user_id: str) -> CrmUserDetailResponse:
    user = await _get_user(session, user_id)
    if user is None:
        raise not_found("User not found.")

    sub = (
        await session.execute(select(Subscription).where(Subscription.user_id == user_id))
    ).scalar_one_or_none()
    agg = await _payment_agg_for_user(session, user_id)

    credited = (
        await session.execute(
            select(func.coalesce(func.sum(CreditGrant.amount), 0)).where(
                CreditGrant.user_id == user_id,
                CreditGrant.amount > 0,
            )
        )
    ).scalar_one()
    spent_jobs = (
        await session.execute(
            select(func.coalesce(func.sum(GenerationJob.spend_usd), 0)).where(
                GenerationJob.user_id == user_id
            )
        )
    ).scalar_one()

    plan_name, price = _product_display(sub.product_id if sub else None, get_settings())
    subscription = CrmUserSubscription(
        plan_id=sub.product_id if sub else None,
        plan_name=plan_name,
        price=price,
        active=_subscription_active(sub),
        expires_at=format_utc(sub.expires_at if sub else None),
        last_payment_at=format_utc(agg.last_payment_at),
        last_payment_method=agg.last_payment_method,
    )
    balance = CrmUserBalance(
        tokens=float(user.bonus_generations_balance),
        credited_total=float(credited or 0),
        spent_total=float(spent_jobs or 0),
    )
    revenue = None
    if agg.payments_sum_usd > 0 or float(spent_jobs or 0) > 0:
        revenue = CrmUserRevenue(
            income_usd=agg.payments_sum_usd,
            api_cost_usd=float(spent_jobs or 0),
            providers=await _spend_by_provider(session, user_id, total_usd=float(spent_jobs or 0)),
        )

    media_stats = await _build_media_stats(session, user_id)

    return CrmUserDetailResponse(
        id=user.id,
        external_id=user.adapty_customer_user_id,
        registered_at=format_utc(user.created_at) or "",
        balance=balance,
        subscription=subscription,
        revenue=revenue,
        media_stats=media_stats,
    )


async def _build_media_stats(session: AsyncSession, user_id: str) -> CrmMediaStats | None:
    rows = (
        await session.execute(
            select(GenerationJob.state, GenerationJob.created_at, GenerationJob.updated_at).where(
                GenerationJob.user_id == user_id
            )
        )
    ).all()
    if not rows:
        return None

    success = sum(1 for state, _, _ in rows if state.value in _TERMINAL_OK_STATES)
    failed = sum(1 for state, _, _ in rows if state.value in _TERMINAL_FAIL_STATES)
    total = len(rows)
    durations: list[float] = []
    for _, created_at, updated_at in rows:
        if updated_at and created_at:
            durations.append(max(0.0, (updated_at - created_at).total_seconds()))
    overall = sum(durations) / len(durations) if durations else None
    return CrmMediaStats(
        photos=CrmMediaStatsBucket(total=total, success=success, failed=failed),
        videos=CrmMediaStatsBucket(total=0, success=0, failed=0),
        avg_generation_sec=CrmMediaAvgGenerationSec(photo=overall, video=None, overall=overall),
    )


async def list_payments(
    session: AsyncSession,
    user_id: str,
    *,
    limit: int,
    offset: int,
) -> CrmPaymentListResponse:
    if await _get_user(session, user_id) is None:
        raise not_found("User not found.")

    events = (
        (
            await session.execute(
                select(BillingEvent)
                .where(
                    BillingEvent.user_id == user_id,
                    BillingEvent.event_type.in_(tuple(_PAYMENT_SUCCESS_EVENTS)),
                    BillingEvent.processed_at.is_not(None),
                )
                .order_by(BillingEvent.received_at.desc())
            )
        )
        .scalars()
        .all()
    )
    store_rows = (
        (
            await session.execute(
                select(StoreTransaction)
                .where(StoreTransaction.user_id == user_id)
                .order_by(StoreTransaction.created_at.desc())
            )
        )
        .scalars()
        .all()
    )

    items: list[CrmPaymentItem] = []
    for event in events:
        props = _extract_event_properties(event.payload)
        amount, currency = _payment_amount_usd(props)
        product_id = props.get("vendor_product_id") or props.get("product_id")
        items.append(
            CrmPaymentItem(
                title=_payment_title(event.event_type, str(product_id) if product_id else None),
                description=_store_label(props.get("store") if props else None),
                amount=amount,
                currency=currency,
                status="success",
                occurred_at=format_utc(event.received_at) or "",
            )
        )
    for row in store_rows:
        title = "Покупка токенов" if row.kind == "tokens_purchase" else "Подписка StoreKit"
        if row.product_id:
            title = f"{title} {row.product_id}"
        items.append(
            CrmPaymentItem(
                title=title,
                description="App Store",
                amount=0.0,
                currency="USD",
                status="success",
                occurred_at=format_utc(row.created_at) or "",
            )
        )

    items.sort(key=lambda item: item.occurred_at, reverse=True)
    total = len(items)
    return CrmPaymentListResponse(total=total, items=items[offset : offset + limit])


def _job_status(state_value: str, duration: float | None) -> tuple[int, str]:
    if state_value in _TERMINAL_OK_STATES:
        code = 200
        label = "slow" if duration is not None and duration > 120 else "ok"
    elif state_value in _TERMINAL_FAIL_STATES:
        code = 500
        label = "error"
    else:
        code = 202
        label = "ok"
    return code, label


async def list_requests(
    session: AsyncSession,
    user_id: str,
    *,
    limit: int,
    offset: int,
) -> CrmRequestListResponse:
    if await _get_user(session, user_id) is None:
        raise not_found("User not found.")

    rows = (
        await session.execute(
            select(GenerationJob, Project.prompt)
            .join(Project, Project.id == GenerationJob.project_id)
            .where(GenerationJob.user_id == user_id)
            .order_by(GenerationJob.created_at.desc())
        )
    ).all()

    items: list[CrmRequestItem] = []
    for job, prompt in rows:
        duration = None
        if job.updated_at and job.created_at:
            duration = max(0.0, (job.updated_at - job.created_at).total_seconds())
        status_code, status = _job_status(job.state.value, duration)
        preview = (prompt or "")[:120] or None
        items.append(
            CrmRequestItem(
                endpoint=job.kind,
                prompt_preview=preview,
                status_code=status_code,
                status=status,
                duration_sec=duration,
                sent_at=format_utc(job.created_at) or "",
            )
        )
    total = len(items)
    return CrmRequestListResponse(total=total, items=items[offset : offset + limit])


async def get_stats(
    session: AsyncSession,
    *,
    date_from: datetime | None,
    date_to: datetime | None,
) -> CrmStatsResponse:
    user_query = select(User.id, User.created_at)
    if date_from is not None:
        user_query = user_query.where(User.created_at >= date_from)
    if date_to is not None:
        user_query = user_query.where(User.created_at <= date_to)
    user_rows = (await session.execute(user_query)).all()

    users_total = len(user_rows)
    paid_users = 0
    payments_sum = 0.0
    for user_id, _ in user_rows:
        agg = await _payment_agg_for_user(session, user_id)
        if agg.is_paid:
            paid_users += 1
        payments_sum += agg.payments_sum_usd

    return CrmStatsResponse(
        users_total=users_total,
        paid_users=paid_users,
        payments_sum_usd=payments_sum,
    )


def list_products(settings: Settings | None = None) -> CrmProductListResponse:
    cfg = settings or get_settings()
    items = [
        CrmProductItem(
            product_id=cfg.subscription_product_weekly,
            name="Pro Weekly",
            price="$6.99",
            period="week",
        ),
        CrmProductItem(
            product_id=cfg.subscription_product_yearly,
            name="Pro Yearly",
            price="$49.99",
            period="year",
        ),
    ]
    for product_id, amount in sorted(cfg.token_pack_map().items()):
        items.append(
            CrmProductItem(
                product_id=product_id,
                name=f"{amount} tokens",
                price=None,
                period=None,
            )
        )
    return CrmProductListResponse(items=items)


async def adjust_tokens(
    session: AsyncSession,
    *,
    user_id: str,
    amount: int,
) -> tuple[str, float]:
    """Начисляет/списывает токены (НЕ идемпотентно, CRM §3.1)."""
    user = await _get_user(session, user_id)
    if user is None:
        raise not_found("User not found.")
    if amount == 0:
        raise bad_request("amount must be non-zero.")

    grant = CreditGrant(
        id=new_credit_grant_id(),
        user_id=user_id,
        amount=amount,
        reason="crm:adjust_tokens",
        idempotency_key=None,
        created_by="crm",
    )
    session.add(grant)
    new_balance = await _apply_balance_delta(session, user_id, amount)
    if new_balance is None:
        current = user.bonus_generations_balance
        await session.rollback()
        raise bad_request(f"Balance cannot be negative (current={current}, amount={amount}).")
    await session.commit()
    return user_id, float(new_balance)


def _crm_grants_map(sub: Subscription | None) -> dict[str, Any]:
    if sub is None or not isinstance(sub.raw, dict):
        return {}
    grants = sub.raw.get("crm_grants")
    return grants if isinstance(grants, dict) else {}


async def grant_subscription_crm(
    session: AsyncSession,
    *,
    user_id: str,
    product_id: str,
    expires_in_days: int,
    grant_id: str,
) -> CrmGrantSubscriptionResponse:
    settings = get_settings()
    if product_id not in _known_subscription_product_ids(settings):
        raise bad_request(f"Unknown product_id: {product_id}")

    user = await _get_user(session, user_id)
    if user is None:
        raise not_found("User not found.")

    sub = (
        await session.execute(select(Subscription).where(Subscription.user_id == user_id))
    ).scalar_one_or_none()
    grants = _crm_grants_map(sub)
    if grant_id in grants:
        fresh_sub = (
            await session.execute(select(Subscription).where(Subscription.user_id == user_id))
        ).scalar_one_or_none()
        return CrmGrantSubscriptionResponse(
            id=user_id,
            tokens=float(user.bonus_generations_balance),
            subscription_active=_subscription_active(fresh_sub),
            subscription_expires_at=format_utc(fresh_sub.expires_at if fresh_sub else None),
            applied=False,
        )

    now = datetime.now(UTC)
    base = now
    if sub is not None and sub.expires_at is not None and sub.expires_at > now:
        base = sub.expires_at
    expires_at = base + timedelta(days=expires_in_days)

    updated_sub = await apply_admin_grant(session, user_id=user_id, expires_at=expires_at)
    updated_sub.product_id = product_id
    raw = dict(updated_sub.raw) if isinstance(updated_sub.raw, dict) else {}
    crm_grants = dict(_crm_grants_map(updated_sub))
    crm_grants[grant_id] = {
        "product_id": product_id,
        "expires_in_days": expires_in_days,
        "granted_at": format_utc(now),
    }
    raw["crm_grants"] = crm_grants
    raw["source"] = "crm_grant"
    updated_sub.raw = raw
    await session.commit()

    fresh_user = await _get_user(session, user_id)
    assert fresh_user is not None
    return CrmGrantSubscriptionResponse(
        id=user_id,
        tokens=float(fresh_user.bonus_generations_balance),
        subscription_active=_subscription_active(updated_sub),
        subscription_expires_at=format_utc(updated_sub.expires_at),
        applied=True,
    )


# ============ Расходы LLM: провайдер по модели + дневная агрегация ============
#
# broad-crm нормализует `provider` сама (её ADR-084: точные ключи + префиксы `gpt*`/`claude*`),
# но опирается на то, что бэк отдаёт СВОЙ сырой ключ. Наш сырой ключ расхода — модель
# (`llm_usage.model`), поэтому провайдер выводится из имени модели, а НЕ из `LLM_PROVIDER`
# инстанса: в ledger одного инстанса сосуществуют записи обоих провайдеров (инстанс мог
# переключаться, ADR-032), и подстановка текущего провайдера переписала бы историю.

# Префиксы имён моделей → провайдер. Порядок не важен (префиксы не пересекаются).
_PROVIDER_BY_MODEL_PREFIX: tuple[tuple[str, str], ...] = (
    ("claude", "anthropic"),
    ("gpt", "openai"),
)

# Максимальная длина периода `GET /admin/costs/daily` (контракт broad-crm v1.3).
MAX_DAILY_COSTS_PERIOD_DAYS = 92


def provider_of_model(model: str) -> str:
    """Провайдер по имени модели cost-ledger; нераспознанная модель → её сырое имя.

    Нераспознанное имя отдаётся как есть (а не «other»): CRM отнесёт его в `other`, но
    оператор увидит, какая именно модель не покрыта маппингом.
    """
    normalized = model.strip().lower()
    for prefix, provider in _PROVIDER_BY_MODEL_PREFIX:
        if normalized.startswith(prefix):
            return provider
    return model.strip() or "other"


async def _spend_by_provider(
    session: AsyncSession, user_id: str, *, total_usd: float
) -> dict[str, float]:
    """Разбивка расхода пользователя по провайдерам из cost-ledger (`llm_usage`).

    Сумма разбивки равна `generation_jobs.spend_usd` пользователя: агрегат джобы
    инкрементируется той же записью ledger (`app/pipeline/cost.record_usage`). Если ledger
    по джобам пуст, а агрегат ненулевой (исторические строки) — расход отдаётся как `other`,
    чтобы не потерять сумму.
    """
    rows = (
        await session.execute(
            select(LlmUsage.model, func.coalesce(func.sum(LlmUsage.cost_usd), 0))
            .join(GenerationJob, GenerationJob.id == LlmUsage.job_id)
            .where(GenerationJob.user_id == user_id)
            .group_by(LlmUsage.model)
        )
    ).all()

    breakdown: dict[str, float] = {}
    for model, spend in rows:
        provider = provider_of_model(model)
        breakdown[provider] = round(breakdown.get(provider, 0.0) + float(spend or 0), 4)
    if not breakdown and total_usd > 0:
        breakdown["other"] = total_usd
    return breakdown


async def daily_costs(
    session: AsyncSession,
    *,
    date_from: date_,
    date_to: date_,
    limit: int,
    offset: int,
) -> CrmDailyCostsResponse:
    """Дневные расходы день × провайдер за период (расширение контракта broad-crm v1.3).

    Границы периода — календарные дни UTC, включительно с обеих сторон. Отсутствие строки
    за (день, провайдер) означает «расхода не было» — нули не досыпаются. Сортировка
    `date ASC, provider ASC` (пара уникальна → пагинация `limit/offset` стабильна).
    """
    if date_from > date_to:
        raise bad_request("date_from must not be after date_to.")
    if (date_to - date_from).days + 1 > MAX_DAILY_COSTS_PERIOD_DAYS:
        raise bad_request(f"Period must not exceed {MAX_DAILY_COSTS_PERIOD_DAYS} days.")

    start = datetime.combine(date_from, time.min, tzinfo=UTC)
    end = datetime.combine(date_to, time.min, tzinfo=UTC) + timedelta(days=1)
    day = func.date(func.timezone("UTC", LlmUsage.created_at)).label("day")
    tokens_sum = func.sum(
        LlmUsage.input_tokens
        + LlmUsage.output_tokens
        + LlmUsage.cache_read_tokens
        + LlmUsage.cache_write_tokens
    )

    rows = (
        await session.execute(
            select(
                day,
                LlmUsage.model,
                func.count().label("requests"),
                func.coalesce(func.sum(LlmUsage.cost_usd), 0).label("spend_usd"),
                func.coalesce(tokens_sum, 0).label("tokens"),
            )
            .where(LlmUsage.created_at >= start, LlmUsage.created_at < end)
            .group_by(day, LlmUsage.model)
        )
    ).all()

    # Свёртка модель → провайдер выполняется в приложении: маппинг живёт в коде, а не в SQL.
    aggregated: dict[tuple[str, str], dict[str, float]] = {}
    for day_value, model, requests, spend_usd, tokens in rows:
        key = (day_value.isoformat(), provider_of_model(model))
        bucket = aggregated.setdefault(key, {"spend_usd": 0.0, "requests": 0.0, "tokens": 0.0})
        bucket["spend_usd"] += float(spend_usd or 0)
        bucket["requests"] += int(requests or 0)
        bucket["tokens"] += float(tokens or 0)

    ordered = sorted(aggregated.items())
    page = ordered[offset : offset + limit]
    return CrmDailyCostsResponse(
        total=len(ordered),
        items=[
            CrmDailyCostItem(
                date=day_value,
                provider=provider,
                spend_usd=round(bucket["spend_usd"], 4),
                requests=int(bucket["requests"]),
                tokens=bucket["tokens"],
            )
            for (day_value, provider), bucket in page
        ],
    )
