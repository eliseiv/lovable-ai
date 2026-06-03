"""Sprint 5 (ADR-013/ADR-014): device_tokens + edit_usage_counters + plan_quotas.monthly_edits.

docs/03-data-model.md (device_tokens / edit_usage_counters / plan_quotas) + ADR-013 (APNs)
+ ADR-014 (отдельный лимит правок). Сидинг monthly_edits: Free=5, Pro=NULL (безлимит) —
дополняет существующие строки plan_quotas (08 §5-2 / docs §plan_quotas).

Revision ID: 20260602_0006
Revises: 20260602_0005
Create Date: 2026-06-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260602_0006"
down_revision: str | None = "20260602_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- device_tokens (APNs push, ADR-013) ---
    op.create_table(
        "device_tokens",
        sa.Column("id", sa.String(), primary_key=True),  # dev_...
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("apns_token", sa.Text(), nullable=False),
        sa.Column("platform", sa.String(), nullable=False, server_default="ios"),
        sa.Column("environment", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("last_push_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_device_tokens_user"),
        # UNIQUE (user_id, apns_token) — точка upsert при регистрации (POST /v1/devices).
        sa.UniqueConstraint("user_id", "apns_token", name="uq_device_tokens_user_token"),
    )
    # Индекс по (user_id) для выборки активных устройств при отправке push.
    op.create_index("ix_device_tokens_user_id", "device_tokens", ["user_id"])

    # --- edit_usage_counters (отдельный счётчик правок, ADR-014) ---
    op.create_table(
        "edit_usage_counters",
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("period", sa.String(), nullable=False),  # YYYY-MM (UTC)
        sa.Column("edits_used", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_edit_usage_counters_user"),
        sa.PrimaryKeyConstraint("user_id", "period", name="pk_edit_usage_counters"),
    )

    # --- plan_quotas.monthly_edits (NULL = безлимит) ---
    op.add_column("plan_quotas", sa.Column("monthly_edits", sa.Integer(), nullable=True))

    # Сидинг monthly_edits существующих строк (data-migration, ADR-014):
    # Free = 5/мес; Pro = NULL (безлимит). Идемпотентно — UPDATE по access_level.
    plan_quotas = sa.table(
        "plan_quotas",
        sa.column("access_level", sa.String()),
        sa.column("monthly_edits", sa.Integer()),
    )
    op.execute(
        plan_quotas.update()
        .where(plan_quotas.c.access_level == op.inline_literal("free"))
        .values(monthly_edits=5)
    )
    # Pro остаётся NULL (безлимит) — явный UPDATE для детерминизма.
    op.execute(
        plan_quotas.update()
        .where(plan_quotas.c.access_level == op.inline_literal("pro"))
        .values(monthly_edits=None)
    )


def downgrade() -> None:
    op.drop_column("plan_quotas", "monthly_edits")
    op.drop_table("edit_usage_counters")
    op.drop_index("ix_device_tokens_user_id", table_name="device_tokens")
    op.drop_table("device_tokens")
