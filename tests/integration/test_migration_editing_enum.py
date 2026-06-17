"""Integration: миграция 20260612_0001 — ALTER TYPE job_state ADD VALUE 'EDITING'.

Нормативный контракт — ADR-031 (sync psycopg-движок + non-transactional DDL) §D и
docs/06-testing-strategy.md §Integration «Non-transactional DDL-миграции — РЕАЛЬНОЕ
применение DDL». Этот тест воспроизводит ПРОД-инцидент 2026-06-12: миграция «прошла»
(alembic_version встал на 20260612_0001), но ADD VALUE НЕ применился под async-движком
(asyncpg+run_sync), и `pg_enum` остался без 'EDITING'.

Поэтому проверки одного `alembic_version` НЕДОСТАТОЧНО — тест ОБЯЗАН после
`alembic upgrade head` проверить РЕАЛЬНОЕ состояние схемы:
    SELECT 1 FROM pg_enum e JOIN pg_type t ON e.enumtypid = t.oid
    WHERE t.typname = 'job_state' AND e.enumlabel = 'EDITING';

Прогон идёт ТЕМ ЖЕ движком/механизмом, что прод-migrate — sync psycopg по env-ключу
DATABASE_URL_SYNC через migrations/env.py (хелпер alembic_env), а НЕ отдельным
sync-коннектом мимо env.py. Сценарии (ADR-031 §A/§C/§D):
  1. pg_enum после upgrade head (главный): свежая БД → EDITING реально в pg_enum.
  2. Идемпотентность (§C): повторный прогон на БД с уже существующим EDITING — no-op
     (ADD VALUE IF NOT EXISTS), значение не дублируется (эмуляция 4 ручных прод-фиксов).
  3. Негатив (§A): env.py без DATABASE_URL_SYNC → явный RuntimeError (не тихий дефолт).
  4. Полная цепочка: upgrade head проходит ВСЕ ревизии на свежей БД через sync psycopg.
"""

from __future__ import annotations

import os
import subprocess
import sys

import asyncpg
from _migration_env import alembic_env, asyncpg_dsn

from app.core.config import get_settings

# asyncio_mode = "auto" (pyproject) сам подхватывает async-тесты; sync-тесты
# (down-revision, негатив RuntimeError) остаются синхронными без asyncio-маркера.


def test_migration_editing_down_revision_chain():
    import importlib

    mod = importlib.import_module("migrations.versions.20260612_0001_editing_state")
    assert mod.revision == "20260612_0001"
    assert mod.down_revision == "20260611_0001"


def _alembic(env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "alembic", *args],
        cwd=os.getcwd(),
        env=env,
        capture_output=True,
        text=True,
    )


async def _enum_label_count(base_url: str, tmp_db: str, label: str) -> int:
    """Точное число вхождений `label` в enum job_state (через pg_enum) — реальная схема.

    Прямой коннект ТОЛЬКО для ПРОВЕРКИ состояния (не для применения миграции — её
    применяет прод-путь env.py). Возвращает count: 0 = нет, 1 = есть, >1 = дубль.
    """
    conn = await asyncpg.connect(asyncpg_dsn(base_url, db=tmp_db))
    try:
        return int(
            await conn.fetchval(
                "SELECT count(*) FROM pg_enum e JOIN pg_type t ON e.enumtypid = t.oid "
                "WHERE t.typname = 'job_state' AND e.enumlabel = $1",
                label,
            )
        )
    finally:
        await conn.close()


async def _alembic_version(base_url: str, tmp_db: str) -> str | None:
    conn = await asyncpg.connect(asyncpg_dsn(base_url, db=tmp_db))
    try:
        return await conn.fetchval("SELECT version_num FROM alembic_version")
    finally:
        await conn.close()


async def _create_db(admin_dsn: str, tmp_db: str) -> None:
    admin = await asyncpg.connect(admin_dsn)
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{tmp_db}"')
        await admin.execute(f'CREATE DATABASE "{tmp_db}"')
    finally:
        await admin.close()


async def _drop_db(admin_dsn: str, tmp_db: str) -> None:
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


async def test_editing_enum_real_ddl_applied_via_prod_sync_path(autonomous_db):
    """ГЛАВНЫЙ (ADR-031 §D): upgrade head через прод-путь sync psycopg РЕАЛЬНО создаёт EDITING.

    Ловит инцидент 2026-06-12: проверяет НЕ только alembic_version, но фактическое
    наличие значения в pg_enum. Под старым async-движком version коммитился, а enum-
    значение не создавалось → этот assert упал бы (count == 0). Прогон — через env.py
    (DATABASE_URL_SYNC, psycopg), как прод-migrate.
    """
    base_url = get_settings().database_url
    tmp_db = f"lovable_migedit_main_{os.getpid()}"
    admin_dsn = asyncpg_dsn(base_url, db="postgres")
    env = alembic_env(base_url, tmp_db)

    await _create_db(admin_dsn, tmp_db)
    try:
        # До EDITING-ревизии значения нет — базовая точка.
        r0 = _alembic(env, "upgrade", "20260611_0001")
        assert r0.returncode == 0, f"alembic upgrade 0611 failed:\n{r0.stderr}"
        assert await _enum_label_count(base_url, tmp_db, "EDITING") == 0

        # upgrade до EDITING-ревизии через прод-путь (sync psycopg, env.py, DATABASE_URL_SYNC).
        # Пин на 20260612_0001 (НЕ head): head сместился на 20260616_0001 (ADR-034 attachments),
        # а этот тест валидирует именно EDITING-enum DDL ревизии 0012.
        r1 = _alembic(env, "upgrade", "20260612_0001")
        assert r1.returncode == 0, (
            "alembic upgrade 0612 (sync psycopg) failed — ADD VALUE внутри транзакции?\n"
            f"{r1.stderr}"
        )
        assert "cannot run inside a transaction block" not in r1.stderr

        # КРИТИЧНО: alembic_version продвинулся И DDL РЕАЛЬНО применён (pg_enum).
        assert await _alembic_version(base_url, tmp_db) == "20260612_0001"
        cnt = await _enum_label_count(base_url, tmp_db, "EDITING")
        assert cnt == 1, (
            f"EDITING не материализовался в pg_enum (count={cnt}) при "
            "alembic_version=20260612_0001 — это и есть прод-инцидент 2026-06-12 "
            "(DDL потерян под async-движком, ADR-031)."
        )
    finally:
        await _drop_db(admin_dsn, tmp_db)


async def test_editing_enum_idempotent_on_db_where_value_exists(autonomous_db):
    """Идемпотентность (ADR-031 §C): повтор миграции на БД с уже существующим EDITING — no-op.

    Эмуляция 4 прод-БД, исправленных ВРУЧНУЮ (EDITING досоздан, version уже 20260612_0001):
    откатываем alembic_version на предыдущую ревизию, сохраняя EDITING в enum, и повторно
    прогоняем head. ADD VALUE IF NOT EXISTS → upgrade проходит без ошибки и НЕ дублирует
    значение (count остаётся 1).
    """
    base_url = get_settings().database_url
    tmp_db = f"lovable_migedit_idem_{os.getpid()}"
    admin_dsn = asyncpg_dsn(base_url, db="postgres")
    env = alembic_env(base_url, tmp_db)

    await _create_db(admin_dsn, tmp_db)
    try:
        r1 = _alembic(env, "upgrade", "head")
        assert r1.returncode == 0, f"upgrade head failed:\n{r1.stderr}"
        assert await _enum_label_count(base_url, tmp_db, "EDITING") == 1

        # Эмуляция «вручную исправленной» прод-БД: enum уже содержит EDITING, но
        # version откатываем на предыдущую → повторный upgrade head снова дойдёт до
        # 20260612_0001 и выполнит ADD VALUE IF NOT EXISTS на существующем значении.
        conn = await asyncpg.connect(asyncpg_dsn(base_url, db=tmp_db))
        try:
            await conn.execute("UPDATE alembic_version SET version_num = '20260611_0001'")
        finally:
            await conn.close()
        assert await _enum_label_count(base_url, tmp_db, "EDITING") == 1

        # Повтор ровно EDITING-ревизии (НЕ head): тест валидирует идемпотентность ADD VALUE
        # IF NOT EXISTS ревизии 0012. Гнать до head нельзя — схема БД уже на head (0016),
        # повторный CREATE TABLE attachments (0016) упал бы DuplicateTable не по теме теста.
        r2 = _alembic(env, "upgrade", "20260612_0001")
        assert r2.returncode == 0, (
            "повторный upgrade 0612 на БД с уже существующим EDITING упал — "
            f"ADD VALUE без IF NOT EXISTS?\n{r2.stderr}"
        )
        # Не дублируется и не теряется: ровно одно вхождение.
        assert await _enum_label_count(base_url, tmp_db, "EDITING") == 1
        assert await _alembic_version(base_url, tmp_db) == "20260612_0001"
    finally:
        await _drop_db(admin_dsn, tmp_db)


def test_editing_enum_missing_sync_url_raises_runtimeerror():
    """Негатив (ADR-031 §A): env.py без DATABASE_URL_SYNC → явный RuntimeError, не тихий дефолт.

    Прод-путь обязан ОТКАЗАТЬ при отсутствии DATABASE_URL_SYNC (мисконфигурация migrate-
    сервиса), а не молча упасть на asyncpg/DATABASE_URL. Subprocess alembic без ключа →
    returncode != 0 + сообщение RuntimeError про DATABASE_URL_SYNC в stderr.
    """
    env = dict(os.environ)
    env.pop("DATABASE_URL_SYNC", None)
    # DATABASE_URL (asyncpg) специально оставлен — проверяем, что env.py НЕ откатится
    # на него молча, а потребует именно DATABASE_URL_SYNC.
    env.setdefault("DATABASE_URL", get_settings().database_url)

    result = _alembic(env, "upgrade", "head")
    assert result.returncode != 0, (
        "alembic без DATABASE_URL_SYNC должен падать (RuntimeError), а не тихо мигрировать "
        f"через asyncpg/DATABASE_URL. stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    combined = result.stderr + result.stdout
    assert "DATABASE_URL_SYNC" in combined, combined
    assert "RuntimeError" in combined, combined


async def test_full_chain_upgrade_head_via_sync_psycopg(autonomous_db):
    """Полная цепочка: upgrade head проходит ВСЕ ревизии на свежей БД через sync psycopg.

    Подтверждает, что перевод движка на psycopg не сломал прочие (transactional) миграции —
    весь набор ревизий применяется от пустой БД до head без ошибок на sync-движке, head
    встаёт на последнюю ревизию.
    """
    base_url = get_settings().database_url
    tmp_db = f"lovable_migedit_chain_{os.getpid()}"
    admin_dsn = asyncpg_dsn(base_url, db="postgres")
    env = alembic_env(base_url, tmp_db)

    await _create_db(admin_dsn, tmp_db)
    try:
        r = _alembic(env, "upgrade", "head")
        assert r.returncode == 0, f"full-chain upgrade head (sync psycopg) failed:\n{r.stderr}"
        # head = текущая последняя ревизия (ADR-036 requested_locale сместил head 0016→0017).
        assert await _alembic_version(base_url, tmp_db) == "20260617_0001"
        # Несколько контрольных объектов из ранних ревизий существуют (цепочка реально
        # применилась, а не только последняя ревизия).
        conn = await asyncpg.connect(asyncpg_dsn(base_url, db=tmp_db))
        try:
            jobs_tbl = await conn.fetchval(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_name = 'generation_jobs'"
            )
            device_tbl = await conn.fetchval(
                "SELECT count(*) FROM information_schema.tables WHERE table_name = 'device_tokens'"
            )
        finally:
            await conn.close()
        assert jobs_tbl == 1
        assert device_tbl == 1
    finally:
        await _drop_db(admin_dsn, tmp_db)
