"""Integration: миграция 20260612_0001 — ALTER TYPE job_state ADD VALUE 'EDITING' (ADR-030 §E).

Throwaway-БД (lovable_migedit_<pid>). Зеркалит test_migration_0006 (alembic subprocess).
Критичный сценарий ADR-030 §E: ADD VALUE НЕЛЬЗЯ выполнять внутри транзакционного блока
(autocommit_block в миграции). Проверяет:
  - down_revision цепочки (20260612_0001 revises 20260611_0001);
  - `alembic upgrade head` РЕАЛЬНО проходит на чистом Postgres БЕЗ ошибки
    «ALTER TYPE ... ADD VALUE cannot run inside a transaction block»;
  - после upgrade enum job_state содержит значение 'EDITING';
  - повторный `upgrade` идемпотентен (IF NOT EXISTS), downgrade — no-op (значение остаётся).
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


async def test_migration_editing_down_revision_chain():
    import importlib

    mod = importlib.import_module("migrations.versions.20260612_0001_editing_state")
    assert mod.revision == "20260612_0001"
    assert mod.down_revision == "20260611_0001"


async def test_migration_editing_add_value_applies_outside_transaction(autonomous_db):
    """upgrade head на чистой БД реально применяет ADD VALUE 'EDITING' без transaction-block ошибки.

    Если бы autocommit_block был забыт, alembic упал бы с
    'ALTER TYPE ... ADD VALUE cannot run inside a transaction block' (PG) и returncode != 0 —
    assert на returncode==0 это ловит. Затем сверяем фактическое наличие enum-значения.
    """
    base_url = get_settings().database_url
    tmp_db = f"lovable_migedit_{os.getpid()}"
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

    def _alembic(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(  # noqa: S603
            [sys.executable, "-m", "alembic", *args],
            cwd=os.getcwd(),
            env=env,
            capture_output=True,
            text=True,
        )

    async def _enum_labels() -> list[str]:
        conn = await asyncpg.connect(_asyncpg_dsn(base_url, db=tmp_db))
        try:
            rows = await conn.fetch(
                "SELECT enumlabel FROM pg_enum e "
                "JOIN pg_type t ON e.enumtypid = t.oid "
                "WHERE t.typname = 'job_state' ORDER BY e.enumsortorder"
            )
            return [r["enumlabel"] for r in rows]
        finally:
            await conn.close()

    try:
        # --- на 20260611_0001 (до EDITING) значения EDITING ещё нет ---
        r0 = _alembic("upgrade", "20260611_0001")
        assert r0.returncode == 0, f"alembic upgrade 0611 failed:\n{r0.stderr}"
        assert "EDITING" not in await _enum_labels()

        # --- upgrade head: ADD VALUE 'EDITING' (autocommit_block) без transaction-block ошибки ---
        r1 = _alembic("upgrade", "head")
        assert r1.returncode == 0, (
            "alembic upgrade head failed — вероятно ADD VALUE внутри транзакции "
            f"(autocommit_block):\n{r1.stderr}"
        )
        # Защитный assert на конкретный класс ошибки PG (ненулевой код в иной формулировке).
        assert "cannot run inside a transaction block" not in r1.stderr
        labels = await _enum_labels()
        assert "EDITING" in labels, labels

        # --- повторный upgrade head идемпотентен (IF NOT EXISTS) ---
        r2 = _alembic("upgrade", "head")
        assert r2.returncode == 0, f"repeat upgrade failed:\n{r2.stderr}"

        # --- downgrade на 20260611_0001 — no-op для enum (значение необратимо остаётся) ---
        r3 = _alembic("downgrade", "20260611_0001")
        assert r3.returncode == 0, f"downgrade failed:\n{r3.stderr}"
        # ADD VALUE необратим: EDITING остаётся в enum даже после downgrade (downgrade = no-op).
        assert "EDITING" in await _enum_labels()
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
