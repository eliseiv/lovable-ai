"""ADR-052: cloudpayments_payments — журнал начисленных RU-платежей.

docs/03-data-model.md → cloudpayments_payments. PK = payment_id агрегатора: один callback
сверяет список платежей пользователя, поэтому идемпотентность обязана быть per-payment, а не
per-callback. Пустая таблица = RU-канал ещё ничего не начислял.

Обычный ТРАНЗАКЦИОННЫЙ op.create_table (нет non-transactional DDL; create_table штатно
транзакционен на sync-движке psycopg env.py, ADR-031). Без backfill (новая таблица).

Revision ID: 20260908_0001
Revises: 20260907_0001
Create Date: 2026-09-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260908_0001"
down_revision: str | None = "20260907_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cloudpayments_payments",
        sa.Column("payment_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("product_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("tokens_granted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("payment_id"),
    )
    op.create_index("ix_cloudpayments_payments_user_id", "cloudpayments_payments", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_cloudpayments_payments_user_id", table_name="cloudpayments_payments")
    op.drop_table("cloudpayments_payments")
