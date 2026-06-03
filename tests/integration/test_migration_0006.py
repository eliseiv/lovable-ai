"""Integration: миграция 20260602_0006 (Sprint 5, ADR-013/014) up/down + сидинг monthly_edits.

Throwaway-БД (lovable_mig0006_<pid>). Зеркалит test_migration_0005 (alembic subprocess).
Проверяет:
  - down_revision == 0005 (цепочка 0001→…→0006 неразрывна);
  - upgrade создаёт device_tokens (+UNIQUE user_id,apns_token, индекс user_id),
    edit_usage_counters (PK user_id,period), plan_quotas.monthly_edits (nullable);
  - сидинг monthly_edits: Free=5, Pro=NULL (data-migration);
  - downgrade реверсивен (таблицы/колонка сняты, схема возвращается к 0005);
  - повторный upgrade восстанавливает.
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


async def test_migration_0006_down_revision_is_0005():
    import importlib

    mod = importlib.import_module("migrations.versions.20260602_0006_realtime_edits")
    assert mod.revision == "20260602_0006"
    assert mod.down_revision == "20260602_0005"


async def test_migration_0006_up_down_and_seed(autonomous_db):
    base_url = get_settings().database_url
    tmp_db = f"lovable_mig0006_{os.getpid()}"
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

    async def _table_exists(table: str) -> bool:
        conn = await _connect()
        try:
            return bool(
                await conn.fetchval(
                    "SELECT count(*) FROM information_schema.tables WHERE table_name = $1", table
                )
            )
        finally:
            await conn.close()

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

    async def _monthly_edits(access_level: str):  # noqa: ANN202
        conn = await _connect()
        try:
            return await conn.fetchval(
                "SELECT monthly_edits FROM plan_quotas WHERE access_level = $1", access_level
            )
        finally:
            await conn.close()

    try:
        # --- на 0005 новых таблиц/колонки нет ---
        _alembic("upgrade", "20260602_0005")
        assert await _table_exists("device_tokens") is False
        assert await _table_exists("edit_usage_counters") is False
        assert await _column_exists("plan_quotas", "monthly_edits") is False

        # --- upgrade 0006 ---
        _alembic("upgrade", "20260602_0006")
        assert await _table_exists("device_tokens") is True
        assert await _table_exists("edit_usage_counters") is True
        assert await _column_exists("plan_quotas", "monthly_edits") is True

        # UNIQUE (user_id, apns_token) + индекс по user_id.
        conn = await _connect()
        try:
            uq = await conn.fetchval(
                "SELECT count(*) FROM pg_constraint WHERE conname = $1",
                "uq_device_tokens_user_token",
            )
            ix = await conn.fetchval(
                "SELECT count(*) FROM pg_indexes WHERE indexname = $1", "ix_device_tokens_user_id"
            )
            # edit_usage_counters PK (user_id, period).
            pk = await conn.fetchval(
                "SELECT count(*) FROM pg_constraint WHERE conname = $1", "pk_edit_usage_counters"
            )
        finally:
            await conn.close()
        assert uq == 1
        assert ix == 1
        assert pk == 1

        # --- сидинг monthly_edits: Free=5, Pro=NULL ---
        assert await _monthly_edits("free") == 5
        assert await _monthly_edits("pro") is None

        # monthly_edits nullable (NULL = безлимит).
        conn = await _connect()
        try:
            is_nullable = await conn.fetchval(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = 'plan_quotas' AND column_name = 'monthly_edits'"
            )
        finally:
            await conn.close()
        assert is_nullable == "YES"

        # --- downgrade 0006 → 0005 реверсивен ---
        _alembic("downgrade", "20260602_0005")
        assert await _table_exists("device_tokens") is False
        assert await _table_exists("edit_usage_counters") is False
        assert await _column_exists("plan_quotas", "monthly_edits") is False

        # --- повторный upgrade восстанавливает + пересидивает ---
        _alembic("upgrade", "20260602_0006")
        assert await _table_exists("device_tokens") is True
        assert await _monthly_edits("free") == 5
        assert await _monthly_edits("pro") is None
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
