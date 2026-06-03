"""Классификация исключений: транзиентный инфра-сбой vs доменный build-fail (ADR-006).

Единственная точка решения «ретраить Celery или уводить в FIXING» (ADR-006, инвариант):
- транзиентные инфра-исключения (Docker/сеть/S3/Anthropic 429/5xx, временные БД/Redis)
  → Celery autoretry с exponential backoff (`max_retries=5`), `FAILED(infra_error)` при
  исчерпании;
- доменные build/health/validation-fail → НЕ Celery-retry, а состояние FIXING (доменный
  цикл с гардами, `retry_count`/no-progress). Так один и тот же фейл не учитывается
  обоими механизмами.

`TRANSIENT_EXCEPTIONS` подаётся в `autoretry_for` Celery-таски, что инкапсулирует
build/deploy-логику (task_build_request / task_deploy / task_fix). Доменные фейлы НЕ
поднимаются как исключения этих типов — они ловятся внутри таски и переводят джобу в
FIXING явным вызовом state-machine.
"""

from __future__ import annotations

import asyncio
import socket

import httpx
from anthropic import APIConnectionError, APIStatusError, APITimeoutError, RateLimitError
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError

# Конфиг Celery-ретраев для тасок, делающих инфра-IO (Docker/S3/Anthropic/БД/Redis).
# Применяется как `**RETRY_KWARGS` в декораторе @celery_app.task (ADR-006).
MAX_RETRIES = 5
RETRY_BACKOFF_MAX_S = 600


class TransientInfraError(RuntimeError):
    """Транзиентный инфра-сбой, явно поднимаемый из кода (например, Docker daemon/CLI).

    Docker CLI вызывается через subprocess и не бросает типизированных исключений
    SDK — обёртка над его транспортными ошибками поднимает это исключение, чтобы
    оно попало в autoretry_for (ADR-006: ошибки Docker daemon/CLI транспорта —
    транзиентные).
    """


# Множество ретраябельных (транзиентных инфра) исключений — единственный источник
# истины (ADR-006). Доменные исключения сюда НЕ входят сознательно.
TRANSIENT_EXCEPTIONS: tuple[type[BaseException], ...] = (
    # Сеть/транспорт к S3/Anthropic/Docker.
    httpx.TransportError,
    APIConnectionError,
    APITimeoutError,
    ConnectionError,
    TimeoutError,
    socket.timeout,
    asyncio.TimeoutError,
    # Anthropic rate-limit / временные server-ошибки.
    RateLimitError,
    # Redis-брокер/счётчики.
    RedisConnectionError,
    RedisTimeoutError,
    # Временные ошибки БД (потеря соединения с Postgres).
    OperationalError,
    InterfaceError,
    DBAPIError,
    # Явный инфра-сбой из subprocess-обёртки Docker.
    TransientInfraError,
)


def is_transient(exc: BaseException) -> bool:
    """True, если исключение — транзиентный инфра-сбой (ретраить Celery), иначе доменное.

    `APIStatusError` ретраябелен только на 429/5xx (server-side/rate-limit); 4xx (кроме
    429) — детерминированная ошибка запроса, не ретраится (ADR-006).
    """
    if isinstance(exc, APIStatusError) and not isinstance(exc, RateLimitError):
        status = getattr(exc, "status_code", None)
        return status == 429 or (isinstance(status, int) and 500 <= status < 600)
    return isinstance(exc, TRANSIENT_EXCEPTIONS)
