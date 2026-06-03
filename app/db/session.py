"""Async SQLAlchemy engine + sessionmaker.

API использует FastAPI-зависимость get_session; воркеры — контекст-менеджер
session_scope (Celery-таски синхронны снаружи, но БД-доступ async внутри).

observability §7 (ADR-019, прод-фикс `RuntimeError: Future attached to a different loop`):
синхронная Celery-задача исполняет async-код через `asyncio.run` (новый loop на каждый
вызов), а asyncpg привязывает Future/соединение к конкретному loop. Глобальный модуль-
уровневый async-engine (создан при импорте на ASGI-loop FastAPI / прежнем loop) НЕЛЬЗЯ
переиспользовать из Celery-задачи — соединения «прилипают» к чужому loop. Поэтому каждая
синхронная Celery-задача с async-DB оборачивает тело в `worker_engine_scope()`: per-task
engine создаётся ВНУТРИ её asyncio.run-loop, биндится в ContextVar и dispose()-ится на
выходе. `session_scope()` (его зовут тела задач и events.py) при активном ContextVar берёт
этот per-task sessionmaker; вне задачи (FastAPI-путь, скрипты, тесты) — глобальный engine.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None

# Per-task sessionmaker активной синхронной Celery-задачи (worker_engine_scope). При не-None
# `session_scope()` биндит сессии к per-task engine (созданному внутри asyncio.run-loop задачи),
# а не к глобальному (привязан к чужому loop → `Future attached to a different loop`, §7).
_task_sessionmaker: ContextVar[async_sessionmaker[AsyncSession] | None] = ContextVar(
    "task_sessionmaker", default=None
)


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url,
            pool_pre_ping=True,
            future=True,
        )
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
            autoflush=False,
        )
    return _sessionmaker


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI-зависимость: одна сессия на запрос."""
    async with get_sessionmaker()() as session:
        yield session


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Контекст-менеджер сессии для воркеров/скриптов.

    observability §7: если активна синхронная Celery-задача под `worker_engine_scope()`
    (per-task engine в её asyncio.run-loop), сессия биндится к per-task sessionmaker из
    ContextVar — иначе глобальный engine (привязанный к чужому loop) дал бы asyncpg
    `RuntimeError: Future attached to a different loop`. Вне задачи (FastAPI-зависимости
    тут не используют session_scope; скрипты/тесты) — глобальный sessionmaker.
    """
    sm = _task_sessionmaker.get() or get_sessionmaker()
    async with sm() as session:
        yield session


@asynccontextmanager
async def worker_engine_scope() -> AsyncIterator[None]:
    """Единая обёртка тела синхронной Celery-задачи с async-DB (observability §7, ADR-019).

    Создаёт per-task async-engine ВНУТРИ текущего asyncio.run-loop задачи, биндит его
    sessionmaker в ContextVar (`session_scope()` его подхватывает) и `engine.dispose()`-ит
    в finally той же корутины — соединения asyncpg не переживают loop и не «прилипают» к
    чужому. Обязательна для ВСЕХ Celery-задач, исполняющих async-DB синхронно (§7 п.4):
    единый вход исключает регресс «новая задача забыла per-task engine».

    Тела задач продолжают звать `session_scope()` без аргументов — биндинг прозрачен через
    ContextVar. Токен сбрасывается на выходе (вложенность/повторный asyncio.run безопасны).
    """
    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True, future=True)
    sm = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    token = _task_sessionmaker.set(sm)
    try:
        yield
    finally:
        _task_sessionmaker.reset(token)
        await engine.dispose()


@asynccontextmanager
async def task_engine_scope() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Per-task async-engine для синхронной Celery-задачи (observability §7, ADR-019).

    Нормативный паттерн §7: async-engine/asyncpg-пул создаётся ВНУТРИ asyncio.run-loop
    задачи и dispose()-ится в finally той же корутины — НЕ модуль-уровневый глобал
    (привязан к ASGI-loop FastAPI / прежнему loop, которого уже нет к моменту запуска
    задачи). asyncpg привязывает Future/соединение к конкретному loop; переиспользование
    глобального engine между asyncio.run-loop'ами → `RuntimeError: Future attached to a
    different loop`. Здесь движок живёт строго в пределах одного asyncio.run, соединения не
    переживают loop и не «прилипают» к чужому.

    Возвращает sessionmaker, привязанный к свежему engine; вызывающий открывает сессии из
    него. engine закрывается (dispose) на выходе — все соединения отпускаются в том же loop.
    """
    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True, future=True)
    try:
        yield async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    finally:
        await engine.dispose()
