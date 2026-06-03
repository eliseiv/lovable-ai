"""Integration: миграция 20260602_0002 (failure_event_pending) up/down + default.

Реальный Postgres. Проверяет (ADR-005, docs §C(d)):
- колонка failure_event_pending существует после upgrade head, NOT NULL, server_default
  false → существующие джобы получают false;
- up/down/up реверсивна (downgrade удаляет колонку, upgrade восстанавливает) — на
  ОТДЕЛЬНОЙ временной БД, чтобы не ломать основную тест-схему.

Реверсивность гоняется alembic'ом на throwaway-БД (lovable_migtest_<pid>): создаётся
из postgres-DSN, прогоняется 0001→0002→0001→0002, проверяется наличие/отсутствие
колонки на каждом шаге, в конце БД удаляется.
"""

from __future__ import annotations

import os
import subprocess
import sys
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import session_scope

pytestmark = pytest.mark.asyncio


def _asyncpg_dsn(sqlalchemy_url: str, db: str | None = None) -> str:
    """Преобразует SQLAlchemy DSN (postgresql+asyncpg://...) в чистый asyncpg DSN."""
    parts = urlsplit(sqlalchemy_url.replace("postgresql+asyncpg://", "postgresql://"))
    path = f"/{db}" if db is not None else parts.path
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _col_exists_sql() -> str:
    return (
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_name='generation_jobs' AND column_name='failure_event_pending'"
    )


async def test_failure_event_pending_column_exists_not_null_default_false(autonomous_db):
    """После upgrade head колонка есть, NOT NULL, default false (миграция 0002)."""
    async with session_scope() as s:
        row = (
            await s.execute(
                text(
                    "SELECT data_type, is_nullable, column_default "
                    "FROM information_schema.columns "
                    "WHERE table_name = 'generation_jobs' "
                    "AND column_name = 'failure_event_pending'"
                )
            )
        ).one_or_none()
    assert row is not None, "колонка failure_event_pending должна существовать после миграции 0002"
    data_type, is_nullable, column_default = row
    assert data_type == "boolean"
    assert is_nullable == "NO"  # NOT NULL
    assert column_default is not None and "false" in column_default.lower()


async def test_existing_jobs_get_false_via_server_default(autonomous_db):
    """INSERT без явного failure_event_pending → false (server_default).

    Эмулирует «существующие джобы»: вставка строки, не указывающей колонку, должна
    получить false (поведение server_default, как на ALTER ... ADD COLUMN существующих).
    """
    from decimal import Decimal

    from app.core.ids import new_job_id, new_project_id
    from app.core.security import hash_api_key
    from app.db.models import GenerationJob, Project, User

    uid = "u_mig0002owner00000000"
    pid = new_project_id()
    jid = new_job_id()
    async with session_scope() as s:
        s.add(
            User(
                id=uid, api_key_hash=hash_api_key("mig-key"), monthly_budget_usd=Decimal("50.0000")
            )
        )
        s.add(Project(id=pid, user_id=uid, prompt="x", title=None))
        await s.flush()
        # Вставляем напрямую SQL без колонки failure_event_pending (как «старая» строка).
        await s.execute(
            text(
                "INSERT INTO generation_jobs (id, project_id, user_id, state, kind, "
                "retry_count, max_fix_attempts, budget_usd, spend_usd, created_at, updated_at) "
                "VALUES (:id, :pid, :uid, 'CREATED', 'generation', 0, 3, 5.0, 0.0, now(), now())"
            ),
            {"id": jid, "pid": pid, "uid": uid},
        )
        await s.commit()

    async with session_scope() as s:
        job = await s.get(GenerationJob, jid)
        assert job.failure_event_pending is False  # server_default false
        # cleanup
        await s.execute(text("DELETE FROM generation_jobs WHERE id = :id"), {"id": jid})
        await s.execute(text("DELETE FROM projects WHERE id = :id"), {"id": pid})
        await s.execute(text("DELETE FROM users WHERE id = :id"), {"id": uid})
        await s.commit()


async def test_migration_0002_up_down_up_reversible(autonomous_db):
    """alembic 0001→0002→0001→0002 на throwaway-БД: колонка появляется/исчезает.

    Гоняем alembic как subprocess с DATABASE_URL на временную БД (создаётся/удаляется
    через asyncpg), чтобы не трогать основную тест-схему. Проверяем фактическое
    наличие колонки на каждом шаге.
    """
    base_url = get_settings().database_url
    tmp_db = f"lovable_migtest_{os.getpid()}"
    admin_dsn = _asyncpg_dsn(base_url, db="postgres")

    admin = await asyncpg.connect(admin_dsn)
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{tmp_db}"')
        await admin.execute(f'CREATE DATABASE "{tmp_db}"')
    finally:
        await admin.close()

    env = dict(os.environ)
    env["DATABASE_URL"] = _asyncpg_dsn(base_url, db=tmp_db).replace(
        "postgresql://", "postgresql+asyncpg://"
    )

    def _alembic(*args: str) -> None:
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-m", "alembic", *args],
            cwd=os.getcwd(),
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"alembic {' '.join(args)} failed:\n{result.stderr}"

    async def _col_count() -> int:
        conn = await asyncpg.connect(_asyncpg_dsn(base_url, db=tmp_db))
        try:
            return await conn.fetchval(_col_exists_sql())
        finally:
            await conn.close()

    try:
        _alembic("upgrade", "20260602_0001")
        assert await _col_count() == 0  # на 0001 колонки нет

        # Целимся в 0002 явно (НЕ head): head со Sprint 3 = 0003, и downgrade -1
        # от head вернул бы лишь 0003→0002, не сняв колонку 0002.
        _alembic("upgrade", "20260602_0002")
        assert await _col_count() == 1  # 0002 добавил

        _alembic("downgrade", "20260602_0001")
        assert await _col_count() == 0  # downgrade 0002→0001 удалил колонку

        _alembic("upgrade", "20260602_0002")
        assert await _col_count() == 1  # повторный upgrade 0002 восстановил
    finally:
        admin = await asyncpg.connect(admin_dsn)
        try:
            # Сбросить активные соединения и удалить throwaway-БД.
            await admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = $1 AND pid <> pg_backend_pid()",
                tmp_db,
            )
            await admin.execute(f'DROP DATABASE IF EXISTS "{tmp_db}"')
        finally:
            await admin.close()
