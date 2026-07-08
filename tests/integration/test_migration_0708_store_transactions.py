"""Integration: миграция 20260708_0001 (ADR-039 store_transactions) — РЕАЛЬНОЕ DDL.

Throwaway-БД (lovable_mig0708_<pid>). Прогон alembic ТЕМ ЖЕ движком/механизмом, что прод-migrate
(sync psycopg по DATABASE_URL_SYNC через migrations/env.py, ADR-031/§D) — НЕ отдельным
sync-коннектом мимо env.py. После upgrade head проверяется ФАКТИЧЕСКОЕ состояние схемы в Postgres
(information_schema / pg_constraint / pg_index), а не только exit 0 / alembic_version:
  - таблица store_transactions создана;
  - PRIMARY KEY на (transaction_id);
  - FOREIGN KEY (user_id) → users(id);
  - индекс ix_store_transactions_user_id по (user_id);
  - downgrade реверсивен (таблица снята), повторный upgrade восстанавливает.

Источник истины: docs/06-testing-strategy.md §Integration «Non-transactional DDL-миграции —
РЕАЛЬНОЕ применение DDL» + ADR-039 §D. Зеркалит test_migration_0017_requested_locale (тот же
throwaway-pattern, alembic subprocess через env.py / прод-путь sync psycopg).
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys

import asyncpg
import pytest

from app.core.config import get_settings
from tests.integration._migration_env import alembic_env, asyncpg_dsn

pytestmark = pytest.mark.asyncio


async def test_migration_0708_down_revision_is_0617():
    """down_revision = 20260617_0001 (статически) → цепочка с head неразрывна (ADR-039 §D)."""
    mod = importlib.import_module("migrations.versions.20260708_0001_store_transactions")
    assert mod.revision == "20260708_0001"
    assert mod.down_revision == "20260617_0001"


async def test_migration_0708_creates_store_transactions(autonomous_db):
    """upgrade head через прод-путь (sync psycopg, env.py) РЕАЛЬНО создаёт store_transactions.

    Проверяет НЕ alembic_version, а фактические каталоги: таблица, PK(transaction_id),
    FK(user_id→users), индекс(user_id). Затем downgrade снимает, повторный upgrade восстанавливает.
    """
    base_url = get_settings().database_url
    tmp_db = f"lovable_mig0708_{os.getpid()}"
    admin_dsn = asyncpg_dsn(base_url, db="postgres")

    admin = await asyncpg.connect(admin_dsn)
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{tmp_db}"')
        await admin.execute(f'CREATE DATABASE "{tmp_db}"')
    finally:
        await admin.close()

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

    async def _table_exists() -> bool:
        conn = await _connect()
        try:
            return bool(
                await conn.fetchval("SELECT to_regclass('public.store_transactions') IS NOT NULL")
            )
        finally:
            await conn.close()

    async def _pk_columns() -> list[str]:
        conn = await _connect()
        try:
            rows = await conn.fetch(
                """
                SELECT a.attname
                FROM pg_index i
                JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
                WHERE i.indrelid = 'public.store_transactions'::regclass AND i.indisprimary
                """
            )
            return sorted(r["attname"] for r in rows)
        finally:
            await conn.close()

    async def _fk_target() -> tuple[str, str] | None:
        conn = await _connect()
        try:
            row = await conn.fetchrow(
                """
                SELECT confrelid::regclass::text AS ref_table,
                       (SELECT attname FROM pg_attribute
                        WHERE attrelid = c.conrelid AND attnum = c.conkey[1]) AS col
                FROM pg_constraint c
                WHERE c.conrelid = 'public.store_transactions'::regclass AND c.contype = 'f'
                """
            )
            return (row["col"], row["ref_table"]) if row is not None else None
        finally:
            await conn.close()

    async def _index_exists(name: str) -> bool:
        conn = await _connect()
        try:
            return bool(await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", f"public.{name}"))
        finally:
            await conn.close()

    try:
        # База (0617) — таблицы ещё нет.
        _alembic("upgrade", "20260617_0001")
        assert await _table_exists() is False

        # upgrade head — прод-путь (sync psycopg через env.py).
        _alembic("upgrade", "head")

        # РЕАЛЬНОЕ состояние схемы.
        assert await _table_exists() is True, "store_transactions не материализовалась при exit 0"
        assert await _pk_columns() == ["transaction_id"], "PK должен быть на (transaction_id)"

        fk = await _fk_target()
        assert fk is not None, "FK на users отсутствует"
        col, ref_table = fk
        assert col == "user_id"
        assert ref_table.endswith("users")

        assert await _index_exists("ix_store_transactions_user_id") is True

        # downgrade реверсивен.
        _alembic("downgrade", "20260617_0001")
        assert await _table_exists() is False

        # повторный upgrade восстанавливает.
        _alembic("upgrade", "head")
        assert await _table_exists() is True
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
