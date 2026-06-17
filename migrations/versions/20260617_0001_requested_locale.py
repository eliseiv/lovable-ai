"""ADR-036: projects.requested_locale — явный locale-override языка генерации.

docs/03-data-model.md (projects) + ADR-036 §5/§8. Новое nullable-поле: нормализованный
BCP-47 (`ru`/`en`) из Form-поля `locale` POST /v1/projects; NULL = авто-детект из prompt
(ADR-028, обратная совместимость).

Обычный ТРАНЗАКЦИОННЫЙ op.add_column (НЕ autocommit_block — нет non-transactional DDL
вроде ALTER TYPE ADD VALUE; add_column штатно транзакционен на sync-движке psycopg
env.py, ADR-031). Без backfill (поле nullable — существующие проекты остаются NULL =
авто-детект, ADR-036 §8).

Revision ID: 20260617_0001
Revises: 20260616_0001
Create Date: 2026-06-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260617_0001"
down_revision: str | None = "20260616_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("requested_locale", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("projects", "requested_locale")
