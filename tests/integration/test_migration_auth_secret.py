"""Integration: миграция 20260608_0001 (ADR-024 auth_secret_hash) up/down + аддитивность.

Throwaway-БД (lovable_mig0608_<pid>). Зеркалит test_migration_admin_bonus (alembic subprocess).
Проверяет (docs/03-data-model.md users.auth_secret_hash, ADR-024 §3):
  - down_revision == 20260604_0001 (текущий head — цепочка неразрывна);
  - upgrade add_column users.auth_secret_hash (Text, NULLABLE, БЕЗ UNIQUE);
  - существующие Apple/admin-юзеры остаются с NULL (аддитивная миграция без backfill);
  - downgrade drop_column реверсивен; повторный upgrade восстанавливает.
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

_REV = "20260608_0001"
_PREV = "20260604_0001"


def _asyncpg_dsn(sqlalchemy_url: str, db: str | None = None) -> str:
    parts = urlsplit(sqlalchemy_url.replace("postgresql+asyncpg://", "postgresql://"))
    path = f"/{db}" if db is not None else parts.path
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


async def test_migration_auth_secret_down_revision_is_current_head():
    """down_revision указывает на текущий head 20260604_0001 (ADR-024 §3)."""
    import importlib

    mod = importlib.import_module("migrations.versions.20260608_0001_auth_secret")
    assert mod.revision == _REV
    assert mod.down_revision == _PREV


async def test_migration_auth_secret_up_down_additive(autonomous_db):
    base_url = get_settings().database_url
    tmp_db = f"lovable_mig0608_{os.getpid()}"
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

    try:
        # --- на 20260604_0001 колонки ещё нет ---
        _alembic("upgrade", _PREV)
        assert await _column_exists("users", "auth_secret_hash") is False

        # Существующий Apple/admin-юзер ДО миграции (apple_sub задан / NULL).
        conn = await _connect()
        try:
            await conn.execute(
                "INSERT INTO users (id, apple_sub, monthly_budget_usd, status) "
                "VALUES ('u_mig_apple_0001', 'apple-sub-existing', 50.0, 'active')"
            )
            await conn.execute(
                "INSERT INTO users (id, apple_sub, monthly_budget_usd, status) "
                "VALUES ('u_mig_admin_0001', NULL, 50.0, 'active')"
            )
        finally:
            await conn.close()

        # --- upgrade 20260608_0001 ---
        _alembic("upgrade", _REV)
        assert await _column_exists("users", "auth_secret_hash") is True

        conn = await _connect()
        try:
            # Колонка Text, NULLABLE.
            col = await conn.fetchrow(
                "SELECT is_nullable, data_type FROM information_schema.columns "
                "WHERE table_name = 'users' AND column_name = 'auth_secret_hash'"
            )
            assert col["is_nullable"] == "YES"
            assert col["data_type"] == "text"

            # БЕЗ UNIQUE-индекса/ограничения на auth_secret_hash (не identity-якорь).
            uniq = await conn.fetchval(
                "SELECT count(*) FROM pg_indexes "
                "WHERE tablename = 'users' AND indexdef ILIKE '%auth_secret_hash%' "
                "AND indexdef ILIKE '%unique%'"
            )
            assert uniq == 0

            # Существующие Apple/admin-юзеры остаются с NULL (без backfill).
            apple_secret = await conn.fetchval(
                "SELECT auth_secret_hash FROM users WHERE id = 'u_mig_apple_0001'"
            )
            admin_secret = await conn.fetchval(
                "SELECT auth_secret_hash FROM users WHERE id = 'u_mig_admin_0001'"
            )
            assert apple_secret is None
            assert admin_secret is None
        finally:
            await conn.close()

        # --- downgrade drop_column реверсивен ---
        _alembic("downgrade", _PREV)
        assert await _column_exists("users", "auth_secret_hash") is False

        # --- повторный upgrade восстанавливает ---
        _alembic("upgrade", _REV)
        assert await _column_exists("users", "auth_secret_hash") is True
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
