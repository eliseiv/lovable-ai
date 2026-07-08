"""ADR-039: store_transactions — глобальный реестр прямого StoreKit-пути.

docs/03-data-model.md → store_transactions + ADR-039 §D. Единственная точка идемпотентности
прямого StoreKit-канала — ГЛОБАЛЬНАЯ по transaction_id (PK): одна Apple-транзакция редимится
ровно один раз во всей системе, ровно одному user_id (блокирует кросс-аккаунтную переигровку
чужого валидного JWS).

Обычный ТРАНЗАКЦИОННЫЙ op.create_table (НЕ autocommit_block — нет non-transactional DDL вроде
ALTER TYPE ADD VALUE / CREATE INDEX CONCURRENTLY; create_table штатно транзакционен на
sync-движке psycopg env.py, ADR-031). Без backfill (новая таблица).

Revision ID: 20260708_0001
Revises: 20260617_0001
Create Date: 2026-07-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260708_0001"
down_revision: str | None = "20260617_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "store_transactions",
        # Apple transactionId — глобально уникален. PK ⇒ глобальный дедуп.
        sa.Column("transaction_id", sa.Text(), primary_key=True),
        sa.Column("original_transaction_id", sa.Text(), nullable=True),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("product_id", sa.Text(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=True),
        sa.Column("environment", sa.Text(), nullable=True),
        sa.Column("amount", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_store_transactions_user"),
    )
    op.create_index("ix_store_transactions_user_id", "store_transactions", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_store_transactions_user_id", table_name="store_transactions")
    op.drop_table("store_transactions")
