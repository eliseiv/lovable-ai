"""Integration: миграция 20260602_0005 (project soft-delete) up/down (S4, ADR-011).

Throwaway-БД (lovable_mig0005_<pid>). Проверяется:
  - down_revision == 0004 (цепочка 0001→…→0005 неразрывна);
  - upgrade добавляет projects.deleted_at (nullable timestamptz) + частичный индекс
    ix_projects_user_active (postgresql_where deleted_at IS NULL);
  - downgrade реверсивен (колонка и индекс сняты, БД возвращается к схеме 0004);
  - повторный upgrade восстанавливает (идемпотентность цепочки).

Зеркалит структуру test_migration_0004 (тот же throwaway-pattern, alembic subprocess).
"""

from __future__ import annotations

import os
import subprocess
import sys
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest

from app.core.config import get_settings

pytestmark = pytest.mark.asyncio


def _asyncpg_dsn(sqlalchemy_url: str, db: str | None = None) -> str:
    parts = urlsplit(sqlalchemy_url.replace("postgresql+asyncpg://", "postgresql://"))
    path = f"/{db}" if db is not None else parts.path
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


async def test_migration_0005_down_revision_is_0004():
    """down_revision цепочки = 0004 (статически, без БД) — цепочка 0001→…→0005 неразрывна."""
    import importlib

    mod = importlib.import_module("migrations.versions.20260602_0005_project_soft_delete")
    assert mod.revision == "20260602_0005"
    assert mod.down_revision == "20260602_0004"


async def test_migration_0005_up_down(autonomous_db):
    base_url = get_settings().database_url
    tmp_db = f"lovable_mig0005_{os.getpid()}"
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

    async def _connect():  # noqa: ANN202
        return await asyncpg.connect(_asyncpg_dsn(base_url, db=tmp_db))

    async def _column_exists(table: str, column: str) -> bool:
        conn = await _connect()
        try:
            return bool(
                await conn.fetchval(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name = $1 AND column_name = $2",
                    table,
                    column,
                )
            )
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

    async def _index_predicate(name: str) -> str | None:
        """Частичный предикат индекса (pg_get_expr) — проверка postgresql_where."""
        conn = await _connect()
        try:
            return await conn.fetchval(
                "SELECT pg_get_expr(ix.indpred, ix.indrelid) "
                "FROM pg_index ix JOIN pg_class c ON c.oid = ix.indexrelid "
                "WHERE c.relname = $1",
                name,
            )
        finally:
            await conn.close()

    try:
        # --- до 0005 (на 0004) колонки/индекса нет ---
        _alembic("upgrade", "20260602_0004")
        assert await _column_exists("projects", "deleted_at") is False
        assert await _index_exists("ix_projects_user_active") is False

        # --- upgrade 0005 ---
        _alembic("upgrade", "20260602_0005")
        assert await _column_exists("projects", "deleted_at") is True
        assert await _index_exists("ix_projects_user_active") is True
        # Частичный индекс именно по deleted_at IS NULL (горячий фильтр активных проектов).
        predicate = await _index_predicate("ix_projects_user_active")
        assert predicate is not None
        assert "deleted_at" in predicate.lower()
        assert "null" in predicate.lower()

        # deleted_at — nullable (soft-delete-маркер, NULL = активен).
        conn = await _connect()
        try:
            is_nullable = await conn.fetchval(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = 'projects' AND column_name = 'deleted_at'"
            )
        finally:
            await conn.close()
        assert is_nullable == "YES"

        # --- downgrade 0005 → 0004 реверсивен ---
        _alembic("downgrade", "20260602_0004")
        assert await _column_exists("projects", "deleted_at") is False
        assert await _index_exists("ix_projects_user_active") is False

        # --- повторный upgrade восстанавливает ---
        _alembic("upgrade", "20260602_0005")
        assert await _column_exists("projects", "deleted_at") is True
        assert await _index_exists("ix_projects_user_active") is True
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
