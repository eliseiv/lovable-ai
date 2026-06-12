"""Add job_state enum value 'EDITING' (visible edit-job state, ADR-030).

docs/03-data-model.md (generation_jobs.state) + ADR-030 §E.
EDITING — видимый промежуточный статус edit-джобы (Agent 4 editor) между CREATED и
BUILDING: активное нетерминальное LLM-фазное состояние. Маршрут EDITING → task_edit
(crash-resume editor'а, не task_fix).

Аддитивна: `ALTER TYPE job_state ADD VALUE 'EDITING'`. Не трогает существующие строки —
backfill не нужен (значение присваивается только новым edit-джобам в рантайме).

⚠️ ALTER TYPE ... ADD VALUE в PostgreSQL НЕЛЬЗЯ выполнять внутри транзакционного блока
(до PG12 строго; на PG12+ новое значение нельзя использовать в той же транзакции — ADR-030
§E). Alembic по умолчанию оборачивает upgrade() в транзакцию, поэтому ADD VALUE выполняется
ВНЕ её через `op.get_context().autocommit_block()` (на время блока соединение переводится в
AUTOCOMMIT, транзакция миграции приостановлена). IF NOT EXISTS — идемпотентность повторного
прогона (ADD VALUE не откатывается, см. downgrade no-op).

Revision ID: 20260612_0001
Revises: 20260611_0001
Create Date: 2026-06-12
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260612_0001"
down_revision: str | None = "20260611_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE вне транзакции миграции: autocommit_block приостанавливает
    # транзакцию Alembic и переводит соединение в AUTOCOMMIT — иначе Postgres отвергнет ADD
    # VALUE внутри транзакционного блока (ADR-030 §E). IF NOT EXISTS делает повторный прогон
    # безопасным (значение enum необратимо, downgrade — no-op).
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE job_state ADD VALUE IF NOT EXISTS 'EDITING'")


def downgrade() -> None:
    # ADD VALUE в PostgreSQL необратим: удалить значение из enum штатным DDL нельзя (требует
    # пересоздания типа с проверкой/перезаписью всех зависимых колонок). Аддитивный enum-член
    # не мешает прежней схеме (существующие строки его не используют), поэтому downgrade — no-op.
    pass
