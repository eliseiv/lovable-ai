"""Integration: миграция 20260602_0003 (auth & multi-user) up/down (ADR-007/008, TD-004).

Реальный Postgres на throwaway-БД (lovable_mig0003_<pid>), чтобы не ломать основную
тест-схему. Проверяет фактическую схему после upgrade/downgrade:
- api_tokens создаётся (с UNIQUE key_id);
- users.api_key_hash становится nullable;
- users.apple_sub существует и UNIQUE;
- downgrade реверсивен (api_tokens удалён, api_key_hash снова NOT NULL, apple_sub снят).
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


async def test_migration_0003_up_down_schema(autonomous_db):
    base_url = get_settings().database_url
    tmp_db = f"lovable_mig0003_{os.getpid()}"
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
    # ADR-031: alembic-движок — sync psycopg по DATABASE_URL_SYNC (env.py читает его, НЕ
    # DATABASE_URL). Без ключа на throwaway-БД env.py мигрировал бы не ту БД / падал
    # RuntimeError. Указываем psycopg-DSN на ту же throwaway-БД (прод-путь миграций).
    env["DATABASE_URL_SYNC"] = _asyncpg_dsn(base_url, db=tmp_db).replace(
        "postgresql://", "postgresql+psycopg://"
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

    async def _table_exists(table: str) -> bool:
        conn = await _connect()
        try:
            return bool(
                await conn.fetchval(
                    "SELECT count(*) FROM information_schema.tables WHERE table_name = $1",
                    table,
                )
            )
        finally:
            await conn.close()

    async def _column_is_nullable(table: str, column: str) -> str | None:
        conn = await _connect()
        try:
            return await conn.fetchval(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = $1 AND column_name = $2",
                table,
                column,
            )
        finally:
            await conn.close()

    async def _unique_exists(index_or_constraint: str) -> bool:
        """UNIQUE по имени индекса (pg_indexes) — покрывает и unique-constraint, и unique-index."""
        conn = await _connect()
        try:
            cnt = await conn.fetchval(
                "SELECT count(*) FROM pg_indexes WHERE indexname = $1", index_or_constraint
            )
            return bool(cnt)
        finally:
            await conn.close()

    try:
        # --- до 0003: на 0002 ещё нет api_tokens / apple_sub, api_key_hash NOT NULL ---
        _alembic("upgrade", "20260602_0002")
        assert await _table_exists("api_tokens") is False
        assert await _column_is_nullable("users", "apple_sub") is None  # колонки нет
        assert await _column_is_nullable("users", "api_key_hash") == "NO"  # NOT NULL до 0003

        # --- upgrade 0003 ---
        _alembic("upgrade", "20260602_0003")
        assert await _table_exists("api_tokens") is True
        # users.api_key_hash → nullable (legacy fallback, ADR-008).
        assert await _column_is_nullable("users", "api_key_hash") == "YES"
        # users.apple_sub есть и nullable (NULL допустим для legacy seeded-юзера).
        assert await _column_is_nullable("users", "apple_sub") == "YES"
        # apple_sub UNIQUE.
        assert await _unique_exists("uq_users_apple_sub") is True
        # api_tokens.key_id UNIQUE (единственная точка O(1) lookup, ADR-008).
        assert await _unique_exists("uq_api_tokens_key_id") is True

        # --- downgrade 0003 → 0002 реверсивен ---
        _alembic("downgrade", "20260602_0002")
        assert await _table_exists("api_tokens") is False
        assert await _column_is_nullable("users", "api_key_hash") == "NO"  # снова NOT NULL
        assert await _column_is_nullable("users", "apple_sub") is None  # колонка снята

        # --- повторный upgrade восстанавливает ---
        _alembic("upgrade", "20260602_0003")
        assert await _table_exists("api_tokens") is True
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
