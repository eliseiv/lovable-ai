"""ADR-024: клиентская аутентификация по user_id + секрет — users.auth_secret_hash.

docs/03-data-model.md (users.auth_secret_hash) + ADR-024 §3. Аддитивный
add_column users.auth_secret_hash text NULL (без backfill — существующие Apple/admin-юзеры
остаются с NULL). Поле nullable и БЕЗ UNIQUE (auth_secret_hash — не identity-якорь, лишь
верификатор; им остаётся id/apple_sub) → миграция не ломает существующие строки.

Revision ID: 20260608_0001
Revises: 20260604_0001
Create Date: 2026-06-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260608_0001"
down_revision: str | None = "20260604_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # argon2id-хэш клиентского секрета (ADR-024). NULL у Apple/admin-юзеров без секрета.
    # Без UNIQUE: не identity-якорь, UNIQUE по хэшу не имеет смысла.
    op.add_column(
        "users",
        sa.Column("auth_secret_hash", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "auth_secret_hash")
