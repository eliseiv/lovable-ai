"""ADR-021: бонус-генерации (кредиты) — users.bonus_generations_balance + credit_grants.

docs/03-data-model.md (users.bonus_generations_balance + credit_grants) + ADR-021 §D.
Накопительный денормализованный баланс (источник истины величины) + append-only ledger
начислений/коррекций админом (аудит + идемпотентность по партиальному UNIQUE
(user_id, idempotency_key)). Списание кредитов строку ledger НЕ создаёт.

Revision ID: 20260604_0001
Revises: 20260603_0007
Create Date: 2026-06-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260604_0001"
down_revision: str | None = "20260603_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Денормализованный накопительный баланс бонус-генераций (NOT NULL DEFAULT 0).
    op.add_column(
        "users",
        sa.Column(
            "bonus_generations_balance",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )

    # 2. Append-only ledger начислений/коррекций (аудит + идемпотентность).
    op.create_table(
        "credit_grants",
        sa.Column("id", sa.String(), primary_key=True),  # cg_...
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Text(), nullable=False, server_default="admin"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_credit_grants_user"),
    )
    op.create_index("ix_credit_grants_user_id", "credit_grants", ["user_id"])
    # Партиальный UNIQUE (user_id, idempotency_key) WHERE idempotency_key IS NOT NULL —
    # дедуп начисления по заголовку Idempotency-Key (повтор → no-op).
    op.create_index(
        "uq_credit_grants_user_idempotency",
        "credit_grants",
        ["user_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_credit_grants_user_idempotency", table_name="credit_grants")
    op.drop_index("ix_credit_grants_user_id", table_name="credit_grants")
    op.drop_table("credit_grants")
    op.drop_column("users", "bonus_generations_balance")
