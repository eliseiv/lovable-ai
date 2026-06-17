"""Integration: миграция 20260617_0001 (ADR-036 projects.requested_locale) — РЕАЛЬНОЕ DDL.

Throwaway-БД (lovable_mig0017_<pid>). Прогон alembic ТЕМ ЖЕ движком/механизмом, что прод-migrate
(sync psycopg по DATABASE_URL_SYNC через migrations/env.py, ADR-031) — НЕ отдельным sync-коннектом
мимо env.py. После upgrade head проверяется ФАКТИЧЕСКОЕ состояние схемы в information_schema
(а не только exit 0 / alembic_version):
  - колонка projects.requested_locale создана, тип text, nullable (§5/§8 — без backfill);
  - down_revision = 20260616_0001 (статически) → цепочка с текущим head неразрывна;
  - downgrade реверсивен (колонка снимается), повторный upgrade восстанавливает.

Источник истины: docs/06-testing-strategy.md §Integration «Non-transactional DDL-миграции —
РЕАЛЬНОЕ применение DDL» (для DDL проверяем материализацию объекта в каталоге, не version) +
ADR-036 §8. Зеркалит test_migration_0016_attachments (тот же throwaway-pattern, alembic
subprocess через env.py / прод-путь sync psycopg).
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


async def test_migration_0017_down_revision_is_0016():
    """down_revision = 20260616_0001 (статически, без БД) — линкуется с текущим head ADR-034.

    ADR-036 §8: текущий head на момент введения миграции — 20260616_0001 (attachments);
    новая ревизия садится строго на него.
    """
    import importlib

    mod = importlib.import_module("migrations.versions.20260617_0001_requested_locale")
    assert mod.revision == "20260617_0001"
    assert mod.down_revision == "20260616_0001"


async def test_migration_0017_adds_requested_locale_column(autonomous_db):
    """upgrade head через прод-путь (sync psycopg, env.py) РЕАЛЬНО создаёт requested_locale-колонку.

    Проверяет НЕ только alembic_version, а фактическое состояние схемы в information_schema:
    колонка существует, тип text, nullable=YES (§5/§8). Затем downgrade снимает её, повторный
    upgrade восстанавливает (реверсивность + идемпотентность цепочки).
    """
    base_url = get_settings().database_url
    tmp_db = f"lovable_mig0017_{os.getpid()}"
    admin_dsn = asyncpg_dsn(base_url, db="postgres")

    admin = await asyncpg.connect(admin_dsn)
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{tmp_db}"')
        await admin.execute(f'CREATE DATABASE "{tmp_db}"')
    finally:
        await admin.close()

    # ADR-031: alembic-движок — sync psycopg по DATABASE_URL_SYNC через env.py (прод-путь).
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

    async def _column_info(table: str, column: str) -> dict | None:
        """Строка information_schema.columns (data_type/is_nullable) или None, если колонки нет."""
        conn = await _connect()
        try:
            row = await conn.fetchrow(
                "SELECT data_type, is_nullable FROM information_schema.columns "
                "WHERE table_name = $1 AND column_name = $2 AND table_schema = 'public'",
                table,
                column,
            )
            return dict(row) if row is not None else None
        finally:
            await conn.close()

    try:
        # На предыдущей ревизии (0016) колонки ещё нет — базовая точка.
        _alembic("upgrade", "20260616_0001")
        assert await _column_info("projects", "requested_locale") is None

        # upgrade head — ПРОД-путь (sync psycopg через env.py).
        _alembic("upgrade", "head")

        # РЕАЛЬНОЕ состояние схемы (не только alembic_version): колонка text NULL (§5/§8).
        info = await _column_info("projects", "requested_locale")
        assert info is not None, "колонка projects.requested_locale не материализовалась при exit 0"
        assert info["data_type"] == "text", f"ожидался text, получено {info['data_type']}"
        assert info["is_nullable"] == "YES", "поле обязано быть nullable (без backfill, §8)"

        # downgrade head → 0016 реверсивен (колонка снята).
        _alembic("downgrade", "20260616_0001")
        assert await _column_info("projects", "requested_locale") is None

        # повторный upgrade восстанавливает.
        _alembic("upgrade", "head")
        assert await _column_info("projects", "requested_locale") is not None
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
