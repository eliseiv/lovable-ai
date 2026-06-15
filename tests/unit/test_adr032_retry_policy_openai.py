"""Unit: классификация OpenAI-исключений в retry_policy (ADR-032 §5, docs §Unit).

Источник истины — docs/adr/ADR-032-llm-provider-abstraction-openai.md §5,
docs/06-testing-strategy.md §Unit «retry-классификация исключений OpenAI».

Оба SDK всегда установлены в образе (провайдер — рантайм); классификатор обязан распознавать
исключения ОБОИХ пакетов (anthropic.RateLimitError ≠ openai.RateLimitError — разные классы).

Покрывает сценарий 7 ТЗ:
- транзиентные OpenAI: RateLimitError(429)/APIConnectionError/APITimeoutError/InternalServerError +
  APIStatusError 429/5xx → is_transient True;
- не-ретраябельные OpenAI: AuthenticationError(401)/PermissionDeniedError(403)/BadRequestError(400)
  + LLMCredentialError → is_non_retryable_llm_failure True, is_transient False;
- APIStatusError 4xx (кроме 429) → НЕ транзиентны;
- is_llm_failure True на openai APIError-иерархии + LLMCredentialError;
- Anthropic-классификация не сломана (зеркальные проверки).
"""

from __future__ import annotations

import httpx
import pytest

# Anthropic-классы (для проверки «не сломали anthropic»).
from anthropic import RateLimitError as AnthropicRateLimitError
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    PermissionDeniedError,
    RateLimitError,
)

from app.workers.retry_policy import (
    LLMCredentialError,
    is_llm_failure,
    is_non_retryable_llm_failure,
    is_transient,
)


def _req() -> httpx.Request:
    return httpx.Request("POST", "https://api.openai.com/v1/responses")


def _status_error(status: int) -> APIStatusError:
    return APIStatusError("boom", response=httpx.Response(status, request=_req()), body=None)


def _rate_limit() -> RateLimitError:
    return RateLimitError("429", response=httpx.Response(429, request=_req()), body=None)


def _auth() -> AuthenticationError:
    return AuthenticationError("401", response=httpx.Response(401, request=_req()), body=None)


def _permission() -> PermissionDeniedError:
    return PermissionDeniedError("403", response=httpx.Response(403, request=_req()), body=None)


def _bad_request() -> BadRequestError:
    return BadRequestError("400", response=httpx.Response(400, request=_req()), body=None)


def _internal_server() -> InternalServerError:
    return InternalServerError("500", response=httpx.Response(500, request=_req()), body=None)


# --- транзиентные OpenAI → is_transient True ---


def test_openai_rate_limit_is_transient():
    assert is_transient(_rate_limit()) is True


def test_openai_connection_error_is_transient():
    assert is_transient(APIConnectionError(request=_req())) is True


def test_openai_timeout_error_is_transient():
    assert is_transient(APITimeoutError(request=_req())) is True


def test_openai_internal_server_error_is_transient():
    """InternalServerError — подкласс APIStatusError со status 500 → транзиентный."""
    assert is_transient(_internal_server()) is True


def test_openai_api_status_429_is_transient():
    assert is_transient(_status_error(429)) is True


@pytest.mark.parametrize("status", [500, 502, 503, 599])
def test_openai_api_status_5xx_is_transient(status):
    assert is_transient(_status_error(status)) is True


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_openai_api_status_4xx_except_429_not_transient(status):
    assert is_transient(_status_error(status)) is False


# --- не-ретраябельные OpenAI → is_non_retryable_llm_failure True, is_transient False ---


def test_openai_auth_error_non_retryable():
    exc = _auth()
    assert is_non_retryable_llm_failure(exc) is True
    assert is_transient(exc) is False


def test_openai_permission_denied_non_retryable():
    exc = _permission()
    assert is_non_retryable_llm_failure(exc) is True
    assert is_transient(exc) is False


def test_openai_bad_request_non_retryable():
    exc = _bad_request()
    assert is_non_retryable_llm_failure(exc) is True
    assert is_transient(exc) is False


def test_llm_credential_error_non_retryable():
    exc = LLMCredentialError("invalid openai key")
    assert is_non_retryable_llm_failure(exc) is True
    assert is_transient(exc) is False


# --- is_llm_failure: OpenAI APIError-иерархия + LLMCredentialError ---


@pytest.mark.parametrize(
    "exc_factory",
    [_rate_limit, _auth, _permission, _bad_request, _internal_server, lambda: _status_error(503)],
)
def test_openai_failures_are_llm_failures(exc_factory):
    """Все сбои OpenAI (подклассы openai.APIError) → is_llm_failure True (agent_unavailable)."""
    assert is_llm_failure(exc_factory()) is True


def test_openai_connection_error_is_llm_failure():
    assert is_llm_failure(APIConnectionError(request=_req())) is True


def test_llm_credential_error_is_llm_failure():
    assert is_llm_failure(LLMCredentialError("x")) is True


def test_non_llm_infra_not_llm_failure():
    """Не-LLM инфра (ValueError/RuntimeError) → не is_llm_failure (infra_error при исчерпании)."""
    assert is_llm_failure(ValueError("npm build failed")) is False


# --- Anthropic-классификация НЕ сломана (зеркальные проверки) ---


def test_anthropic_rate_limit_still_transient():
    exc = AnthropicRateLimitError("429", response=httpx.Response(429, request=_req()), body=None)
    assert is_transient(exc) is True
    assert is_llm_failure(exc) is True
