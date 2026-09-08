"""ADR-053: generation_jobs.retry_of_job_id — цепочка повторных попыток генерации.

docs/03-data-model.md → generation_jobs. NULL = обычный запуск; непустое значение = эта
джоба создана ретраем упавшей. По цепочке считается, сколько бесплатных повторов уже сделано
для одной оплаченной генерации.

Обычный ТРАНЗАКЦИОННЫЙ op.add_column nullable-колонки БЕЗ server_default: существующие джобы
остаются NULL, таблица не переписывается (ADR-031, sync psycopg env.py). Индекс создаётся
обычным CREATE INDEX — таблица небольшая, блокировка на запись кратковременна.

Revision ID: 20260909_0001
Revises: 20260908_0001
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260909_0001"
down_revision: str | None = "20260908_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("generation_jobs", sa.Column("retry_of_job_id", sa.String(), nullable=True))
    op.create_foreign_key(
        "fk_generation_jobs_retry_of",
        "generation_jobs",
        "generation_jobs",
        ["retry_of_job_id"],
        ["id"],
    )
    op.create_index("ix_generation_jobs_retry_of_job_id", "generation_jobs", ["retry_of_job_id"])


def downgrade() -> None:
    op.drop_index("ix_generation_jobs_retry_of_job_id", table_name="generation_jobs")
    op.drop_constraint("fk_generation_jobs_retry_of", "generation_jobs", type_="foreignkey")
    op.drop_column("generation_jobs", "retry_of_job_id")
