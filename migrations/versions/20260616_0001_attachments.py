"""ADR-034: attachments — приложенные изображения (vision + реальный ассет сайта).

docs/03-data-model.md (attachments) + ADR-034 §D6. Новая таблица: vision-референс
агентам 1/2/4 + детерминированный инжект воркером в public/uploads/{att_id}.{ext}.
Источник истины «какие фото у проекта/джобы».

Обычный ТРАНЗАКЦИОННЫЙ op.create_table (НЕ autocommit_block — нет non-transactional DDL
вроде ALTER TYPE ADD VALUE; create_table штатно транзакционен на sync-движке psycopg
env.py, ADR-031). Backfill не нужен (новая таблица).

Revision ID: 20260616_0001
Revises: 20260612_0001
Create Date: 2026-06-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260616_0001"
down_revision: str | None = "20260612_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "attachments",
        sa.Column("id", sa.String(), primary_key=True),  # att_...
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("job_id", sa.String(), nullable=True),
        sa.Column("s3_ref", sa.Text(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=True),
        sa.Column("mime", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("sha256", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_attachments_project"),
        sa.ForeignKeyConstraint(["job_id"], ["generation_jobs.id"], name="fk_attachments_job"),
    )
    # Индекс по (project_id): выборка всех фото проекта для инжекта на build и для GC (§D4).
    op.create_index("ix_attachments_project_id", "attachments", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_attachments_project_id", table_name="attachments")
    op.drop_table("attachments")
