"""Integration: миграция 20260616_0001 (ADR-034 attachments) — РЕАЛЬНОЕ применение DDL.

Throwaway-БД (lovable_mig0016_<pid>). Прогон alembic ТЕМ ЖЕ движком/механизмом, что прод-migrate
(sync psycopg по DATABASE_URL_SYNC через migrations/env.py, ADR-031) — НЕ отдельным sync-коннектом
мимо env.py. После upgrade head проверяется ФАКТИЧЕСКОЕ состояние схемы в information_schema /
pg_indexes (а не только exit 0 / alembic_version):
  - таблица attachments создана с колонками контракта (§D6);
  - индекс ix_attachments_project_id присутствует (§D4);
  - FK attachments.project_id→projects и attachments.job_id→generation_jobs существуют.
Плюс down_revision цепочки = 20260612_0001 (статически) и downgrade реверсивен.

Источник истины: docs/06-testing-strategy.md §Integration «Non-transactional DDL-миграции —
РЕАЛЬНОЕ применение DDL» (для DDL проверяем материализацию объекта в каталоге, не version).
Зеркалит test_migration_0005 (тот же throwaway-pattern, alembic subprocess через env.py).
"""

from __future__ import annotations

import os
import subprocess
import sys

import asyncpg
import pytest

from app.core.config import get_settings
from tests.integration._migration_env import alembic_env, asyncpg_dsn

pytestmark = pytest.mark.asyncio


async def test_migration_0016_down_revision_is_0012():
    """down_revision = 20260612_0001 (статически, без БД) — цепочка неразрывна."""
    import importlib

    mod = importlib.import_module("migrations.versions.20260616_0001_attachments")
    assert mod.revision == "20260616_0001"
    assert mod.down_revision == "20260612_0001"


async def test_migration_0016_creates_attachments_table_and_index(autonomous_db):
    base_url = get_settings().database_url
    tmp_db = f"lovable_mig0016_{os.getpid()}"
    admin_dsn = asyncpg_dsn(base_url, db="postgres")

    admin = await asyncpg.connect(admin_dsn)
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{tmp_db}"')
        await admin.execute(f'CREATE DATABASE "{tmp_db}"')
    finally:
        await admin.close()

    # ADR-031: alembic-движок — sync psycopg по DATABASE_URL_SYNC через migrations/env.py
    # (прод-путь). alembic_env кладёт оба DSN на throwaway-БД.
    env = alembic_env(base_url, tmp_db)

    def _alembic(*args: str) -> None:
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-m", "alembic", *args],
            cwd=os.getcwd(),
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"alembic {' '.join(args)} failed:\n{result.stderr}"

    async def _connect():  # noqa: ANN202
        return await asyncpg.connect(asyncpg_dsn(base_url, db=tmp_db))

    async def _table_exists(table: str) -> bool:
        conn = await _connect()
        try:
            return bool(
                await conn.fetchval(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_name = $1 AND table_schema = 'public'",
                    table,
                )
            )
        finally:
            await conn.close()

    async def _column_names(table: str) -> set[str]:
        conn = await _connect()
        try:
            rows = await conn.fetch(
                "SELECT column_name FROM information_schema.columns WHERE table_name = $1",
                table,
            )
            return {r["column_name"] for r in rows}
        finally:
            await conn.close()

    async def _index_exists(name: str) -> bool:
        conn = await _connect()
        try:
            return bool(
                await conn.fetchval("SELECT count(*) FROM pg_indexes WHERE indexname = $1", name)
            )
        finally:
            await conn.close()

    async def _fk_targets(table: str) -> set[str]:
        """Имена таблиц, на которые ссылаются FK из `table` (проверка §D7 FK)."""
        conn = await _connect()
        try:
            rows = await conn.fetch(
                """
                SELECT ccu.table_name AS target
                FROM information_schema.table_constraints tc
                JOIN information_schema.constraint_column_usage ccu
                  ON tc.constraint_name = ccu.constraint_name
                WHERE tc.table_name = $1 AND tc.constraint_type = 'FOREIGN KEY'
                """,
                table,
            )
            return {r["target"] for r in rows}
        finally:
            await conn.close()

    try:
        # До 0016 (на 0012) таблицы нет.
        _alembic("upgrade", "20260612_0001")
        assert await _table_exists("attachments") is False

        # upgrade head — ПРОД-путь (sync psycopg через env.py).
        _alembic("upgrade", "head")

        # РЕАЛЬНОЕ состояние схемы (не только alembic_version): таблица + колонки §D6.
        assert await _table_exists("attachments") is True
        cols = await _column_names("attachments")
        expected_cols = {
            "id",
            "project_id",
            "job_id",
            "s3_ref",
            "filename",
            "mime",
            "size_bytes",
            "width",
            "height",
            "sha256",
            "created_at",
        }
        assert expected_cols <= cols, f"не хватает колонок: {expected_cols - cols}"

        # Индекс ix_attachments_project_id реально создан в pg_indexes (§D4).
        assert await _index_exists("ix_attachments_project_id") is True

        # FK на projects и generation_jobs (§D7 hard-delete порядок опирается на них).
        targets = await _fk_targets("attachments")
        assert "projects" in targets
        assert "generation_jobs" in targets

        # downgrade 0016 → 0012 реверсивен (таблица и индекс сняты).
        _alembic("downgrade", "20260612_0001")
        assert await _table_exists("attachments") is False
        assert await _index_exists("ix_attachments_project_id") is False

        # повторный upgrade восстанавливает.
        _alembic("upgrade", "head")
        assert await _table_exists("attachments") is True
        assert await _index_exists("ix_attachments_project_id") is True
    finally:
        admin = await asyncpg.connect(admin_dsn)
        try:
            await admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = $1 AND pid <> pg_backend_pid()",
                tmp_db,
            )
            await admin.execute(f'DROP DATABASE IF EXISTS "{tmp_db}"')
        finally:
            await admin.close()
