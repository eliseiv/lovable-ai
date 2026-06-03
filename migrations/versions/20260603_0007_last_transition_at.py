"""Add generation_jobs.last_transition_at (heartbeat прогресса, ADR-019).

Новый heartbeat-столбец: момент последнего входа в текущий state. Обновляется только
при смене state (та же транзакция, что state+job_events+publish), в отличие от updated_at
(дёргается cost-ledger'ом). Reconciler (docs §E2) использует его для stuck-критерия всех
активных нетерминальных состояний → fail-stuck/ре-диспетчеризация против concurrency-leak.

Backfill существующих строк значением updated_at (ADR-019 Consequences): для уже-живущих
джоб heartbeat стартует с их последнего апдейта строки — консервативно (не «оживляет» и не
ложно фейлит свежие).

Revision ID: 20260603_0007
Revises: 20260602_0006
Create Date: 2026-06-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260603_0007"
down_revision: str | None = "20260602_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Добавляем nullable с server_default now() — новые строки получат heartbeat при insert.
    op.add_column(
        "generation_jobs",
        sa.Column(
            "last_transition_at",
            sa.DateTime(timezone=True),
            nullable=True,
            server_default=sa.text("now()"),
        ),
    )
    # 2. Backfill существующих строк значением updated_at (ADR-019): heartbeat для старых
    #    джоб стартует с их последнего апдейта.
    op.execute(
        "UPDATE generation_jobs SET last_transition_at = updated_at "
        "WHERE last_transition_at IS NULL"
    )
    # 3. Делаем NOT NULL (после backfill все строки заполнены).
    op.alter_column("generation_jobs", "last_transition_at", nullable=False)


def downgrade() -> None:
    op.drop_column("generation_jobs", "last_transition_at")
