"""Схемы CRM Admin API (broad-crm «Пользователи бэков», контракт v1 2026-07-23).

Префикс: /v1/admin. Auth: X-Admin-Key. Даты — ISO 8601 UTC.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field, field_validator


class CrmUserListItem(BaseModel):
    id: str
    external_id: str | None = None
    is_paid: bool
    payments_count: int
    renewals_count: int
    tokens: float
    subscription_active: bool
    subscription_expires_at: str | None = None
    plan_id: str | None = None
    registered_at: str


class CrmUserListResponse(BaseModel):
    total: int
    items: list[CrmUserListItem]


class CrmUserBalance(BaseModel):
    tokens: float
    credited_total: float | None = None
    spent_total: float | None = None


class CrmUserSubscription(BaseModel):
    plan_id: str | None = None
    plan_name: str | None = None
    price: str | None = None
    active: bool
    expires_at: str | None = None
    last_payment_at: str | None = None
    last_payment_method: str | None = None


class CrmUserRevenue(BaseModel):
    income_usd: float
    api_cost_usd: float
    providers: dict[str, float]


class CrmMediaStatsBucket(BaseModel):
    total: int
    success: int
    failed: int


class CrmMediaAvgGenerationSec(BaseModel):
    photo: float | None = None
    video: float | None = None
    overall: float | None = None


class CrmMediaStats(BaseModel):
    photos: CrmMediaStatsBucket
    videos: CrmMediaStatsBucket
    avg_generation_sec: CrmMediaAvgGenerationSec


class CrmUserDetailResponse(BaseModel):
    id: str
    external_id: str | None = None
    registered_at: str
    balance: CrmUserBalance
    subscription: CrmUserSubscription
    revenue: CrmUserRevenue | None = None
    media_stats: CrmMediaStats | None = None


class CrmPaymentItem(BaseModel):
    title: str
    description: str | None = None
    amount: float
    currency: str
    status: str
    occurred_at: str


class CrmPaymentListResponse(BaseModel):
    total: int
    items: list[CrmPaymentItem]


class CrmRequestItem(BaseModel):
    endpoint: str
    prompt_preview: str | None = None
    status_code: int
    status: str
    duration_sec: float | None = None
    sent_at: str


class CrmRequestListResponse(BaseModel):
    total: int
    items: list[CrmRequestItem]


class CrmStatsResponse(BaseModel):
    users_total: int
    paid_users: int
    payments_sum_usd: float


class CrmProductItem(BaseModel):
    product_id: str
    name: str
    price: str | None = None
    period: str | None = None


class CrmProductListResponse(BaseModel):
    items: list[CrmProductItem]


class CrmAdjustTokensRequest(BaseModel):
    amount: int


class CrmAdjustTokensResponse(BaseModel):
    id: str
    tokens: float


class CrmGrantSubscriptionRequest(BaseModel):
    product_id: str = Field(min_length=1)
    expires_in_days: int = Field(gt=0)
    grant_id: str = Field(min_length=1, max_length=255)

    @field_validator("grant_id")
    @classmethod
    def _strip_grant_id(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            msg = "grant_id must be non-empty."
            raise ValueError(msg)
        return stripped


class CrmGrantSubscriptionResponse(BaseModel):
    id: str
    tokens: float
    subscription_active: bool
    subscription_expires_at: str | None = None
    applied: bool


def format_utc(dt: datetime | None) -> str | None:
    """Сериализует datetime в ISO 8601 UTC (…Z)."""
    if dt is None:
        return None
    dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")
