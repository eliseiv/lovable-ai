"""Integration: миграция 20260604_0001 (ADR-021 бонус-кредиты) up/down + CHECK-инвариант.

Throwaway-БД (lovable_mig0604_<pid>). Зеркалит test_migration_0006 (alembic subprocess).
Проверяет (docs/modules/admin/03 §3, 03-data-model credit_grants):
  - down_revision == 20260603_0007 (цепочка неразрывна);
  - upgrade добавляет users.bonus_generations_balance (NOT NULL DEFAULT 0) + CHECK
    ck_users_bonus_generations_balance_nonneg (>= 0); создаёт credit_grants + партиальный
    UNIQUE (user_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
  - CHECK реально отвергает баланс < 0 (defense-in-depth);
  - партиальный UNIQUE дедупит (user_id, idempotency_key), но допускает несколько NULL-ключей;
  - downgrade реверсивен (колонка/таблица/CHECK сняты); повторный upgrade восстанавливает.
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

_REV = "20260604_0001"
_PREV = "20260603_0007"


def _asyncpg_dsn(sqlalchemy_url: str, db: str | None = None) -> str:
    parts = urlsplit(sqlalchemy_url.replace("postgresql+asyncpg://", "postgresql://"))
    path = f"/{db}" if db is not None else parts.path
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


async def test_migration_admin_bonus_down_revision():
    import importlib

    mod = importlib.import_module("migrations.versions.20260604_0001_admin_bonus_credits")
    assert mod.revision == _REV
    assert mod.down_revision == _PREV


async def test_migration_admin_bonus_up_down_and_check(autonomous_db):
    base_url = get_settings().database_url
    tmp_db = f"lovable_mig0604_{os.getpid()}"
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

    async def _constraint_exists(name: str) -> bool:
        conn = await _connect()
        try:
            return bool(
                await conn.fetchval("SELECT count(*) FROM pg_constraint WHERE conname = $1", name)
            )
        finally:
            await conn.close()

    try:
        # --- на 0007 ни колонки, ни таблицы, ни CHECK нет ---
        _alembic("upgrade", _PREV)
        assert await _column_exists("users", "bonus_generations_balance") is False
        assert await _table_exists("credit_grants") is False
        assert await _constraint_exists("ck_users_bonus_generations_balance_nonneg") is False

        # --- upgrade 20260604_0001 ---
        _alembic("upgrade", _REV)
        assert await _column_exists("users", "bonus_generations_balance") is True
        assert await _table_exists("credit_grants") is True
        assert await _constraint_exists("ck_users_bonus_generations_balance_nonneg") is True

        conn = await _connect()
        try:
            # NOT NULL DEFAULT 0 на колонке баланса.
            col = await conn.fetchrow(
                "SELECT is_nullable, column_default FROM information_schema.columns "
                "WHERE table_name = 'users' AND column_name = 'bonus_generations_balance'"
            )
            assert col["is_nullable"] == "NO"
            assert "0" in (col["column_default"] or "")

            # Партиальный UNIQUE (user_id, idempotency_key) WHERE idempotency_key IS NOT NULL.
            uq = await conn.fetchval(
                "SELECT count(*) FROM pg_indexes WHERE indexname = $1",
                "uq_credit_grants_user_idempotency",
            )
            assert uq == 1

            # CHECK реально отвергает баланс < 0 (вставляем юзера и пытаемся уйти в минус).
            await conn.execute(
                "INSERT INTO users (id, monthly_budget_usd, bonus_generations_balance, status) "
                "VALUES ('u_mig_chk0001', 50.0, 0, 'active')"
            )
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    "UPDATE users SET bonus_generations_balance = -1 WHERE id = 'u_mig_chk0001'"
                )

            # Партиальный UNIQUE: два NULL-ключа допустимы, дубль непустого ключа — нет.
            await conn.execute(
                "INSERT INTO credit_grants (id, user_id, amount, idempotency_key, created_by) "
                "VALUES ('cg_a', 'u_mig_chk0001', 1, NULL, 'admin'), "
                "('cg_b', 'u_mig_chk0001', 1, NULL, 'admin')"
            )  # два NULL — ок
            await conn.execute(
                "INSERT INTO credit_grants (id, user_id, amount, idempotency_key, created_by) "
                "VALUES ('cg_c', 'u_mig_chk0001', 1, 'idem-x', 'admin')"
            )
            with pytest.raises(asyncpg.UniqueViolationError):
                await conn.execute(
                    "INSERT INTO credit_grants (id, user_id, amount, idempotency_key, created_by) "
                    "VALUES ('cg_d', 'u_mig_chk0001', 1, 'idem-x', 'admin')"
                )
        finally:
            await conn.close()

        # --- downgrade реверсивен ---
        _alembic("downgrade", _PREV)
        assert await _column_exists("users", "bonus_generations_balance") is False
        assert await _table_exists("credit_grants") is False
        assert await _constraint_exists("ck_users_bonus_generations_balance_nonneg") is False

        # --- повторный upgrade восстанавливает ---
        _alembic("upgrade", _REV)
        assert await _column_exists("users", "bonus_generations_balance") is True
        assert await _table_exists("credit_grants") is True
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
