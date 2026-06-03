"""Sprint 4 (ADR-011): projects.deleted_at soft-delete-маркер + индекс активных.

DELETE /projects/{pid} ставит projects.deleted_at = now() (soft-delete) → проект
исчезает из всех GET-листингов/деталей (фильтр deleted_at IS NULL) и из подсчёта
max_projects quota-gate; затем Celery project.gc делает полный GC ресурсов и
hard-delete строки. Частичный индекс ix_projects_user_active ускоряет горячий
фильтр активных проектов пользователя (deleted_at IS NULL).

Revision ID: 20260602_0005
Revises: 20260602_0004
Create Date: 2026-06-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260602_0005"
down_revision: str | None = "20260602_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # projects.deleted_at (nullable timestamptz) — soft-delete-маркер (ADR-011).
    op.add_column(
        "projects",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Частичный индекс активных проектов: горячий фильтр листингов/деталей/quota-gate.
    op.create_index(
        "ix_projects_user_active",
        "projects",
        ["user_id"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_projects_user_active", table_name="projects")
    op.drop_column("projects", "deleted_at")
