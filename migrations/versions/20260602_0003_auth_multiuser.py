"""Auth & multi-user (Sprint 3): users.apple_sub + api_tokens (ADR-007/008, TD-004).

- users.apple_sub: NULL UNIQUE — стабильный sub Apple identity token (identity-якорь).
- users.api_key_hash: NOT NULL → nullable (legacy S1 fallback на время миграции, ADR-008).
- api_tokens: opaque-токены lv_<key_id>_<secret>, индексируемый O(1) lookup по UNIQUE key_id.

Revision ID: 20260602_0003
Revises: 20260602_0002
Create Date: 2026-06-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260602_0003"
down_revision: str | None = "20260602_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- users.apple_sub (NULL UNIQUE) ---
    op.add_column("users", sa.Column("apple_sub", sa.Text(), nullable=True))
    op.create_unique_constraint("uq_users_apple_sub", "users", ["apple_sub"])

    # --- users.api_key_hash → nullable (legacy fallback, ADR-008 «Миграционный путь») ---
    op.alter_column("users", "api_key_hash", existing_type=sa.Text(), nullable=True)

    # --- api_tokens (Sprint 3): мульти-устройство + индексируемый lookup ---
    op.create_table(
        "api_tokens",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("key_id", sa.String(), nullable=False),
        sa.Column("key_hash", sa.Text(), nullable=False),
        sa.Column("device_label", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_api_tokens_user"),
    )
    # UNIQUE key_id — единственная точка O(1) lookup токена (ADR-008, B-tree-индекс).
    op.create_index("uq_api_tokens_key_id", "api_tokens", ["key_id"], unique=True)
    # Индекс по user_id для листинга устройств (GET /v1/auth/tokens).
    op.create_index("ix_api_tokens_user_id", "api_tokens", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_api_tokens_user_id", table_name="api_tokens")
    op.drop_index("uq_api_tokens_key_id", table_name="api_tokens")
    op.drop_table("api_tokens")
    op.alter_column("users", "api_key_hash", existing_type=sa.Text(), nullable=False)
    op.drop_constraint("uq_users_apple_sub", "users", type_="unique")
    op.drop_column("users", "apple_sub")
