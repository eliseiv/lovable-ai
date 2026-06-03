"""Billing Sprint 3.5: subscriptions + plan_quotas + usage_counters + billing_events.

docs/03-data-model.md (Биллинг) + docs/modules/billing/ + ADR-009. Сидинг plan_quotas
Free=3/1/1, Pro=100/null/3 (нормативная таблица docs §plan_quotas / 08 §3.5).

Revision ID: 20260602_0004
Revises: 20260602_0003
Create Date: 2026-06-02
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260602_0004"
down_revision: str | None = "20260602_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Технический cost-cap тарифа (= env JOB_BUDGET_USD дефолт; две независимые величины с
# бизнес-квотой monthly_generations, docs §8). Численно совпадает для обоих тарифов в S3.5.
_JOB_BUDGET_USD = Decimal("5.0000")


def upgrade() -> None:
    # --- subscriptions (локальный кэш Adapty, одна строка на user_id) ---
    op.create_table(
        "subscriptions",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("access_level", sa.String(), nullable=False, server_default="free"),
        sa.Column("product_id", sa.Text(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column("store", sa.String(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("grace_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("will_renew", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("adapty_transaction_id", sa.Text(), nullable=True),
        sa.Column(
            "raw", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"
        ),
        sa.Column(
            "synced_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_subscriptions_user"),
        sa.UniqueConstraint("user_id", name="uq_subscriptions_user"),
    )
    op.create_index("ix_subscriptions_user_id", "subscriptions", ["user_id"])
    # Индекс для grace-sweep (status='grace' AND grace_until < now()).
    op.create_index(
        "ix_subscriptions_status_grace_until", "subscriptions", ["status", "grace_until"]
    )

    # --- plan_quotas (лимиты тарифа, ключ access_level) ---
    op.create_table(
        "plan_quotas",
        sa.Column("access_level", sa.String(), primary_key=True),
        sa.Column("monthly_generations", sa.Integer(), nullable=False),
        sa.Column("max_concurrent_jobs", sa.Integer(), nullable=True),
        sa.Column("max_projects", sa.Integer(), nullable=True),
        sa.Column("job_budget_usd", sa.Numeric(10, 4), nullable=False),
    )

    # --- usage_counters (PK (user_id, period), YYYY-MM) ---
    op.create_table(
        "usage_counters",
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("period", sa.String(), nullable=False),
        sa.Column("generations_used", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_usage_counters_user"),
        sa.PrimaryKeyConstraint("user_id", "period", name="pk_usage_counters"),
    )

    # --- billing_events (Adapty webhook ledger; adapty_event_id UNIQUE = идемпотентность) ---
    op.create_table(
        "billing_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("adapty_event_id", sa.Text(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=True),
        sa.Column(
            "payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_billing_events_user"),
        sa.UniqueConstraint("adapty_event_id", name="uq_billing_events_adapty_event_id"),
    )

    # --- Сидинг plan_quotas (нормативная таблица docs §plan_quotas / 08 §3.5) ---
    # Free = 3 ген / 1 проект / 1 конкурентная; Pro = 100 ген / безлимит проектов / 3.
    plan_quotas = sa.table(
        "plan_quotas",
        sa.column("access_level", sa.String()),
        sa.column("monthly_generations", sa.Integer()),
        sa.column("max_concurrent_jobs", sa.Integer()),
        sa.column("max_projects", sa.Integer()),
        sa.column("job_budget_usd", sa.Numeric(10, 4)),
    )
    op.bulk_insert(
        plan_quotas,
        [
            {
                "access_level": "free",
                "monthly_generations": 3,
                "max_concurrent_jobs": 1,
                "max_projects": 1,
                "job_budget_usd": _JOB_BUDGET_USD,
            },
            {
                "access_level": "pro",
                "monthly_generations": 100,
                "max_concurrent_jobs": 3,
                "max_projects": None,  # безлимит (Pro)
                "job_budget_usd": _JOB_BUDGET_USD,
            },
        ],
    )


def downgrade() -> None:
    op.drop_table("billing_events")
    op.drop_table("usage_counters")
    op.drop_table("plan_quotas")
    op.drop_index("ix_subscriptions_status_grace_until", table_name="subscriptions")
    op.drop_index("ix_subscriptions_user_id", table_name="subscriptions")
    op.drop_table("subscriptions")
