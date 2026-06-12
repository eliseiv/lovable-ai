"""Alembic env: SYNC-движок psycopg, metadata из ORM, URL из env DATABASE_URL_SYNC.

Нормативный движок миграций — sync psycopg (ADR-031), НЕ async asyncpg. Причина:
стандартный alembic-механизм non-transactional DDL `op.get_context().autocommit_block()`
(нужен для `ALTER TYPE ... ADD VALUE`, которое PostgreSQL запрещает в транзакции) реально
переводит соединение в AUTOCOMMIT только на синхронном DBAPI-движке. Поверх asyncpg+`run_sync`
он соединение в AUTOCOMMIT не переводил → DDL «терялся», а `alembic_version` коммитился
(прод-инцидент 2026-06-12, миграция 20260612_0001). См. ADR-031, docs/03-data-model.md
(Migration-guidance), docs/07-deployment.md (канонический список ключей).

URL берётся из env-ключа DATABASE_URL_SYNC (`postgresql+psycopg://`) напрямую — НЕ из
Settings (`extra=ignore`, поле под него не заводится; ADR-031 §A) и НЕ из DATABASE_URL
(asyncpg, рантайм приложения). Тот же Postgres, иной драйвер.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config
from sqlalchemy.pool import NullPool

from app.db import models  # noqa: F401 - регистрирует модели в Base.metadata
from app.db.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _sync_database_url() -> str:
    """SYNC-DSN Alembic из env DATABASE_URL_SYNC (psycopg, ADR-031).

    Читается из окружения напрямую (а не из Settings): миграции — оффлайн-операция
    migrate-сервиса, ключ DATABASE_URL_SYNC не является полем Settings (extra=ignore).
    Отсутствие ключа — мисконфигурация migrate-сервиса (devops обязан его предоставить,
    docs/07-deployment.md), поэтому явный отказ, а не тихий дефолт.
    """
    url = os.environ.get("DATABASE_URL_SYNC")
    if not url:
        raise RuntimeError(
            "DATABASE_URL_SYNC не задан: Alembic-движок — sync psycopg "
            "(postgresql+psycopg://), ключ обязателен в окружении migrate-сервиса "
            "(ADR-031, docs/07-deployment.md)."
        )
    return url


# URL — из env (секреты не в alembic.ini); sync psycopg-DSN, не asyncpg.
config.set_main_option("sqlalchemy.url", _sync_database_url())

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
