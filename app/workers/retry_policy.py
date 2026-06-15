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
from anthropic import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    PermissionDeniedError,
    RateLimitError,
)

# ADR-032 §5: оба SDK всегда установлены в образе (провайдер выбирается рантаймом, НЕ условным
# импортом по LLM_PROVIDER). Имена классов openai совпадают с anthropic, но это РАЗНЫЕ классы из
# разных пакетов (anthropic.RateLimitError ≠ openai.RateLimitError) — классификатор обязан
# распознавать исключения ОБОИХ SDK независимо от выбранного провайдера. Импорт под алиасами.
from openai import APIConnectionError as OpenAIAPIConnectionError
from openai import APIError as OpenAIAPIError
from openai import APIStatusError as OpenAIAPIStatusError
from openai import APITimeoutError as OpenAIAPITimeoutError
from openai import AuthenticationError as OpenAIAuthenticationError
from openai import BadRequestError as OpenAIBadRequestError
from openai import PermissionDeniedError as OpenAIPermissionDeniedError
from openai import RateLimitError as OpenAIRateLimitError
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


class LLMCredentialError(RuntimeError):
    """Client-side auth-resolution сбой Anthropic SDK ДО HTTP (ADR-019 §Fix round 3, §G).

    При пустом/невалидном ANTHROPIC_API_KEY Anthropic SDK бросает ВСТРОЕННЫЙ stdlib-`TypeError`
    («Could not resolve authentication method...») на этапе сборки заголовков
    (`_validate_headers`) — ДО HTTP-запроса. Это НЕ подкласс `anthropic.APIError`, поэтому
    классификатор LLM-сбоев его бы не распознал → джоба ушла бы в ветку «unexpected»
    (autoretry/re-raise) и зависла бы до reconciler-TTL.

    Чтобы матч был узким и version-agnostic (а НЕ по подстроке сообщения через весь стек),
    `ClaudeAgentClient.run_agent` перехватывает `TypeError` вокруг первого вызова SDK
    (`messages.stream` / `get_final_message`) и поднимает это исключение. SDK валидирует
    auth ЛЕНИВО — на первом запросе, а НЕ в конструкторе клиента, поэтому точка перехвата —
    `run_agent` (первый вызов SDK), не `__init__`. Классификатор трактует его как
    не-транзиентный LLM-сбой → немедленный FAILED(agent_unavailable) без ретраев (§G,
    подстраховка-слой; основной путь для ПУСТОГО ключа — preflight в run_agent_task).
    """


# Множество ретраябельных (транзиентных инфра) исключений — единственный источник
# истины (ADR-006). Доменные исключения сюда НЕ входят сознательно.
TRANSIENT_EXCEPTIONS: tuple[type[BaseException], ...] = (
    # Сеть/транспорт к S3/LLM/Docker.
    httpx.TransportError,
    APIConnectionError,
    APITimeoutError,
    # OpenAI SDK-эквиваленты (ADR-032 §5): оба провайдера в одном образе.
    OpenAIAPIConnectionError,
    OpenAIAPITimeoutError,
    ConnectionError,
    TimeoutError,
    socket.timeout,
    asyncio.TimeoutError,
    # Anthropic + OpenAI rate-limit / временные server-ошибки.
    RateLimitError,
    OpenAIRateLimitError,
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
    429) — детерминированная ошибка запроса, не ретраится (ADR-006). Проверка покрывает
    APIStatusError/RateLimitError ОБОИХ SDK (anthropic + openai, ADR-032 §5).
    """
    if isinstance(exc, (APIStatusError, OpenAIAPIStatusError)) and not isinstance(
        exc, (RateLimitError, OpenAIRateLimitError)
    ):
        status = getattr(exc, "status_code", None)
        return status == 429 or (isinstance(status, int) and 500 <= status < 600)
    return isinstance(exc, TRANSIENT_EXCEPTIONS)


# --- ADR-019 §G: классификация недоступности LLM (graceful-fail шага агента) ---

# Не-транзиентные сбои Claude — детерминированно-падающие вызовы (ключ отсутствует/невалиден,
# нет прав, bad request). НЕ ретраятся (ретрай бессмыслен): таска немедленно делает
# graceful-переход FAILED(agent_unavailable) без сжигания max_retries (ADR-019 §G).
NON_RETRYABLE_LLM_EXCEPTIONS: tuple[type[BaseException], ...] = (
    AuthenticationError,  # 401 — ANTHROPIC_API_KEY отсутствует/невалиден
    PermissionDeniedError,  # 403
    BadRequestError,  # 400, не зависящий от входных данных
    # OpenAI SDK-эквиваленты (ADR-032 §5): те же 401/403/400 активного провайдера в одном образе.
    OpenAIAuthenticationError,
    OpenAIPermissionDeniedError,
    OpenAIBadRequestError,
    # ADR-019 §Fix round 3 (подстраховка): client-side auth-resolution-сбой Anthropic SDK
    # (поднят ClaudeAgentClient как LLMCredentialError из встроенного stdlib-TypeError) —
    # невалидный credential, детерминированно-падающий ДО HTTP. Не ретраить → agent_unavailable.
    # OpenAI-клиент (ADR-032 §5) сюда НЕ маппит: невалидный ключ у openai SDK не валидируется
    # client-side, а даёт request-time OpenAIAuthenticationError(401) — уже покрыта выше.
    LLMCredentialError,
)


def is_non_retryable_llm_failure(exc: BaseException) -> bool:
    """True для не-транзиентного сбоя Claude (401/403/400) — graceful-fail без ретраев (§G).

    Эти исключения детерминированно повторяются (ключ невалиден / нет прав / bad request) —
    ретрай только сожжёт max_retries без терминализации. Таска обязана сразу перевести джобу
    в FAILED(agent_unavailable), а не висеть в активном state (ADR-019 §G). Классификация —
    та же единственная точка решения, что is_transient (docs §D/§G).
    """
    return isinstance(exc, NON_RETRYABLE_LLM_EXCEPTIONS)


def is_llm_failure(exc: BaseException) -> bool:
    """True, если исключение относится к недоступности LLM (Anthropic), иначе не-LLM инфра.

    Разграничивает reason-код при исчерпании Celery max_retries (ADR-019 §G / docs §D):
    исчерпание на сбое Claude (429/5xx/timeout/connection или auth/permission/bad-request) →
    FAILED(agent_unavailable); исчерпание на не-LLM инфра (Docker/S3/БД/Redis) →
    FAILED(infra_error). Anthropic SDK поднимает подклассы APIError на все сбои Claude
    (включая APIConnectionError/APITimeoutError/RateLimitError/APIStatusError).
    `LLMCredentialError` (client-side auth-resolution-сбой SDK, ADR-019 §Fix round 3) —
    тоже LLM-недоступность, хотя и вне иерархии APIError. OpenAI SDK (ADR-032 §5) тоже поднимает
    подклассы своего `APIError` на все сбои LLM — классификатор покрывает ОБА SDK.
    """
    return isinstance(exc, (APIError, OpenAIAPIError, LLMCredentialError))


# --- ADR-019 §Fix round 3: per-job fail-fast preflight LLM-credential (основной путь) ---


def llm_credential_present(api_key: str | None) -> bool:
    """True, если LLM-credential АКТИВНОГО провайдера пригоден для SDK-вызова (ADR-019 §G).

    Непустой = не `None` и не whitespace-only строка. Принимает уже-распакованное значение
    credential активного провайдера (`Settings.active_llm_api_key()` — ANTHROPIC_API_KEY либо
    OPENAI_API_KEY по LLM_PROVIDER, ADR-032 §5), не `SecretStr`, чтобы не логировать секрет и не
    тащить зависимость от типа конфига в классификатор. При `False` агент-таска делает fail-fast
    graceful-переход FAILED(agent_unavailable) ДО первого обращения к LLM SDK — version-agnostic,
    детерминированно ловит самый частый прод-кейс (пустой ключ), не завися от типа/текста
    встроенного auth-resolution-сбоя SDK (§Fix round 3, п.1).
    """
    return bool(api_key and api_key.strip())
