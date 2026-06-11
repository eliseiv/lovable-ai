"""Add generation_jobs.content_language (deterministic source-prompt language, ADR-028).

docs/03-data-model.md (generation_jobs.content_language) + ADR-028 (ревизует ADR-025).
BCP-47 язык контента сайта, детектится детерминированно на сервере из ИСХОДНОГО
project.prompt (script-эвристика) один раз на старте фазы interview, ДО Agent 1.
Crash-устойчивый якорь языка: переживает рестарт воркера между фазами.

Аддитивна: add_column text NOT NULL default 'en'. server_default 'en' backfill'ит
существующие строки (fallback-язык ADR-028 §5 совпадает с default), поэтому NOT NULL
не ломает существующие джобы.

Revision ID: 20260611_0001
Revises: 20260608_0001
Create Date: 2026-06-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260611_0001"
down_revision: str | None = "20260608_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # NOT NULL default 'en': server_default backfill'ит существующие строки значением 'en'
    # (fallback-язык ADR-028 §5), поэтому добавление NOT NULL-колонки безопасно для
    # существующих джоб без отдельного UPDATE.
    op.add_column(
        "generation_jobs",
        sa.Column(
            "content_language",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'en'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("generation_jobs", "content_language")
