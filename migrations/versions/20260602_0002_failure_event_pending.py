"""Add generation_jobs.failure_event_pending (no-progress vs crash-resume).

Колонка-дискриминатор для гарда no-progress (docs §C(d), ADR-005): True, когда
произведён новый failure-event (enter_fixing / невалидный патч Agent 4), ещё не
потреблённый гардом. Отличает реальный no-progress (повтор сигнатуры на НОВОМ
событии) от crash-resume (reconciler ре-диспетчеризовал task_fix по тому же логу).
Внутреннее состояние гарда (как last_failure_signature), не внешний контракт.

Revision ID: 20260602_0002
Revises: 20260602_0001
Create Date: 2026-06-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260602_0002"
down_revision: str | None = "20260602_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "generation_jobs",
        sa.Column(
            "failure_event_pending",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("generation_jobs", "failure_event_pending")
