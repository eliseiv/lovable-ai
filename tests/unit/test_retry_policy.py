"""Unit: классификация исключений transient vs domain (ADR-006, docs §D).

Единственная точка решения (app/workers/retry_policy.py): is_transient(exc).
- транзиентные инфра (Docker/network/S3/Anthropic 429/5xx/timeout/Redis/DB) → True
  (Celery autoretry с backoff);
- APIStatusError 4xx (кроме 429) и доменный build-fail → False (НЕ Celery-retry,
  доменный фейл идёт в FIXING).

Доменный build/health/validation-fail в коде НЕ поднимается как исключение из
TRANSIENT_EXCEPTIONS — он ловится внутри таски и уводит в FIXING (проверяется в
integration); здесь — что обычные доменные исключения классифицируются как НЕ-transient.
"""

from __future__ import annotations

import httpx
import pytest
from anthropic import APIConnectionError, APIStatusError, APITimeoutError, RateLimitError
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.exc import OperationalError

from app.workers.retry_policy import TransientInfraError, is_transient


def _make_request() -> httpx.Request:
    return httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def _api_status_error(status: int) -> APIStatusError:
    response = httpx.Response(status, request=_make_request())
    return APIStatusError("boom", response=response, body=None)


# --- транзиентные → True ---


def test_docker_transient_infra_error_is_transient():
    assert is_transient(TransientInfraError("docker daemon unreachable")) is True


def test_httpx_transport_error_is_transient():
    assert is_transient(httpx.ConnectError("conn refused", request=_make_request())) is True


def test_anthropic_connection_error_is_transient():
    assert is_transient(APIConnectionError(request=_make_request())) is True


def test_anthropic_timeout_error_is_transient():
    assert is_transient(APITimeoutError(request=_make_request())) is True


def test_socket_timeout_is_transient():
    assert is_transient(TimeoutError("timed out")) is True


def test_asyncio_timeout_is_transient():
    assert is_transient(TimeoutError()) is True


def test_builtin_connection_error_is_transient():
    assert is_transient(ConnectionError("s3 reset")) is True


def test_builtin_timeout_error_is_transient():
    assert is_transient(TimeoutError("s3 timeout")) is True


def test_redis_connection_error_is_transient():
    assert is_transient(RedisConnectionError("broker down")) is True


def test_db_operational_error_is_transient():
    assert is_transient(OperationalError("SELECT 1", {}, Exception("conn lost"))) is True


def test_anthropic_rate_limit_error_is_transient():
    response = httpx.Response(429, request=_make_request())
    assert is_transient(RateLimitError("429", response=response, body=None)) is True


# --- APIStatusError по коду ---


def test_api_status_429_is_transient():
    assert is_transient(_api_status_error(429)) is True


@pytest.mark.parametrize("status", [500, 502, 503, 599])
def test_api_status_5xx_is_transient(status: int):
    assert is_transient(_api_status_error(status)) is True


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_api_status_4xx_except_429_not_transient(status: int):
    assert is_transient(_api_status_error(status)) is False


# --- доменные → False ---


def test_plain_value_error_not_transient():
    """Доменный build/validation-fail (ValueError/AgentOutputError) — НЕ Celery-retry."""
    assert is_transient(ValueError("npm build failed")) is False


def test_runtime_error_not_transient():
    """Generic RuntimeError (не TransientInfraError) — доменный, НЕ transient."""
    assert is_transient(RuntimeError("vite build exit 1")) is False
