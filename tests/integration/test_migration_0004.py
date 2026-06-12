"""Integration: миграция 20260602_0004 (billing) up/down + сидинг plan_quotas.

Throwaway-БД (lovable_mig0004_<pid>). После upgrade: subscriptions/plan_quotas/
usage_counters/billing_events созданы; billing_events.adapty_event_id UNIQUE; plan_quotas
засидирован Free=3/1/1, Pro=100/null/3. downgrade реверсивен (таблицы сняты).
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


async def test_migration_0004_up_down_and_seed(autonomous_db):
    base_url = get_settings().database_url
    tmp_db = f"lovable_mig0004_{os.getpid()}"
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
                    "SELECT count(*) FROM information_schema.tables WHERE table_name = $1", table
                )
            )
        finally:
            await conn.close()

    async def _unique_exists(name: str) -> bool:
        conn = await _connect()
        try:
            return bool(
                await conn.fetchval("SELECT count(*) FROM pg_indexes WHERE indexname = $1", name)
            )
        finally:
            await conn.close()

    try:
        # --- до 0004 (на 0003) биллинговых таблиц нет ---
        _alembic("upgrade", "20260602_0003")
        assert await _table_exists("subscriptions") is False
        assert await _table_exists("plan_quotas") is False

        # --- upgrade 0004 ---
        _alembic("upgrade", "20260602_0004")
        for t in ("subscriptions", "plan_quotas", "usage_counters", "billing_events"):
            assert await _table_exists(t) is True, t
        # billing_events.adapty_event_id UNIQUE (идемпотентность вебхука).
        assert await _unique_exists("uq_billing_events_adapty_event_id") is True

        # --- сидинг plan_quotas: Free=3/1/1, Pro=100/null/3 ---
        conn = await _connect()
        try:
            free = await conn.fetchrow(
                "SELECT monthly_generations, max_concurrent_jobs, max_projects "
                "FROM plan_quotas WHERE access_level = 'free'"
            )
            pro = await conn.fetchrow(
                "SELECT monthly_generations, max_concurrent_jobs, max_projects "
                "FROM plan_quotas WHERE access_level = 'pro'"
            )
        finally:
            await conn.close()
        assert (free["monthly_generations"], free["max_concurrent_jobs"], free["max_projects"]) == (
            3,
            1,
            1,
        )
        assert pro["monthly_generations"] == 100
        assert pro["max_concurrent_jobs"] == 3
        assert pro["max_projects"] is None  # безлимит проектов (Pro)

        # --- downgrade 0004 → 0003 реверсивен ---
        _alembic("downgrade", "20260602_0003")
        for t in ("subscriptions", "plan_quotas", "usage_counters", "billing_events"):
            assert await _table_exists(t) is False, t

        # --- повторный upgrade восстанавливает (+ сидинг повторно идемпотентен) ---
        _alembic("upgrade", "20260602_0004")
        assert await _table_exists("plan_quotas") is True
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
