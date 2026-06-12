"""Хелпер для migration-тестов: окружение alembic-subprocess под прод-путь (ADR-031).

Прод-migrate гонит alembic СИНХРОННЫМ движком psycopg по env-ключу DATABASE_URL_SYNC
(`postgresql+psycopg://`), а НЕ asyncpg/DATABASE_URL (migrations/env.py, ADR-031 §A).
`migrations/env.py` читает DATABASE_URL_SYNC напрямую и при его отсутствии бросает
RuntimeError — поэтому subprocess alembic в тестах ОБЯЗАН получить DATABASE_URL_SYNC,
указывающий на throwaway-БД теста, иначе он мигрирует не ту БД (унаследованный из
окружения DATABASE_URL_SYNC) или падает RuntimeError.

`alembic_env(base_url, tmp_db)` собирает окружение subprocess с ОБОИМИ DSN на throwaway-БД:
  - DATABASE_URL      — asyncpg (`postgresql+asyncpg://…/tmp_db`), для кода, читающего Settings;
  - DATABASE_URL_SYNC — psycopg  (`postgresql+psycopg://…/tmp_db`), ПРОД-путь alembic-движка.
Так migration-тест воспроизводит реальный прод-путь применения DDL (ADR-031 §D), а не
отдельный sync-коннект мимо env.py.

`base_url` — async DSN из get_settings().database_url (тест-стек, asyncpg). `_dsn` строит
из него драйвер-специфичный DSN на нужную БД (тот же host/креды, иной драйвер/база).
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit, urlunsplit


def _bare_pg_netloc_path(sqlalchemy_url: str, db: str | None) -> tuple[str, str]:
    """netloc + path из async SQLAlchemy-URL (срезает драйвер-суффикс +asyncpg)."""
    parts = urlsplit(sqlalchemy_url.replace("postgresql+asyncpg://", "postgresql://"))
    path = f"/{db}" if db is not None else parts.path
    return parts.netloc, path


def asyncpg_dsn(sqlalchemy_url: str, db: str | None = None) -> str:
    """`postgresql://…` (driver-neutral) — для прямых asyncpg.connect в тестах."""
    netloc, path = _bare_pg_netloc_path(sqlalchemy_url, db)
    return urlunsplit(("postgresql", netloc, path, "", ""))


def async_sqlalchemy_dsn(sqlalchemy_url: str, db: str | None = None) -> str:
    """`postgresql+asyncpg://…/db` — DATABASE_URL рантайма приложения."""
    netloc, path = _bare_pg_netloc_path(sqlalchemy_url, db)
    return urlunsplit(("postgresql+asyncpg", netloc, path, "", ""))


def sync_sqlalchemy_dsn(sqlalchemy_url: str, db: str | None = None) -> str:
    """`postgresql+psycopg://…/db` — DATABASE_URL_SYNC, ПРОД-путь alembic-движка (ADR-031)."""
    netloc, path = _bare_pg_netloc_path(sqlalchemy_url, db)
    return urlunsplit(("postgresql+psycopg", netloc, path, "", ""))


def alembic_env(base_url: str, tmp_db: str) -> dict[str, str]:
    """Окружение subprocess `python -m alembic` на throwaway-БД tmp_db (прод-путь sync).

    Кладёт ОБА DSN на tmp_db: DATABASE_URL (asyncpg, Settings) и DATABASE_URL_SYNC
    (psycopg, прод-путь alembic). Без DATABASE_URL_SYNC env.py упал бы RuntimeError
    или (при унаследованном из окружения ключе) мигрировал бы не ту БД — оба случая
    делают migration-тест ложным. ADR-031 §A/§D.
    """
    env = dict(os.environ)
    env["DATABASE_URL"] = async_sqlalchemy_dsn(base_url, db=tmp_db)
    env["DATABASE_URL_SYNC"] = sync_sqlalchemy_dsn(base_url, db=tmp_db)
    return env
