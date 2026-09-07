"""ADR-051: projects.model_id — выбранный пользователем пресет модели генерации.

docs/03-data-model.md → projects. NULL = пресет не выбран (поведение как до ADR-051: каждый
агент идёт со своим AGENTn_MODEL). Значение — id из каталога `GET /v1/models` (`fast`/
`quality`), а не идентификатор модели провайдера: каталог провайдер-специфичен и меняется
вместе с кодом, id пресета стабилен.

Обычный ТРАНЗАКЦИОННЫЙ op.add_column nullable-колонки БЕЗ server_default: существующие
проекты остаются NULL, переписывания таблицы не происходит (ADR-031, sync psycopg env.py).

Revision ID: 20260907_0001
Revises: 20260902_0001
Create Date: 2026-09-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260907_0001"
down_revision: str | None = "20260902_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("model_id", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("projects", "model_id")
