"""ADR-046: job_sections — план сайта и посекционный прогресс генерации.

docs/03-data-model.md → job_sections. План формирует Agent 2 (`sections` в его выводе),
статус `done` проставляется при распознавании секции в потоке кодогенерации Agent 3.
Строки живут в рамках джобы; отсутствие строк = плана нет (правка/откат, старые джобы).

Обычный ТРАНЗАКЦИОННЫЙ op.create_table (нет non-transactional DDL вроде ALTER TYPE ADD VALUE
или CREATE INDEX CONCURRENTLY; create_table штатно транзакционен на sync-движке psycopg
env.py, ADR-031). Без backfill (новая таблица): у уже выполненных джоб плана не было.

Revision ID: 20260902_0001
Revises: 20260708_0001
Create Date: 2026-09-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260902_0001"
down_revision: str | None = "20260708_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "job_sections",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("job_id", sa.String(), nullable=False),
        sa.Column("section_id", sa.String(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["job_id"], ["generation_jobs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "section_id", name="uq_job_sections_job_section"),
    )
    op.create_index("ix_job_sections_job_id_position", "job_sections", ["job_id", "position"])


def downgrade() -> None:
    op.drop_index("ix_job_sections_job_id_position", table_name="job_sections")
    op.drop_table("job_sections")
