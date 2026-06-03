"""Unit: подстраховка-классификация client-side auth-resolution SDK (ADR-019 §Fix round 3 п.5).

Нормативный источник — docs/modules/pipeline/03-architecture.md §G «Client-side auth-resolution
ошибка SDK (подстраховка)», §D, docs/06-testing-strategy.md (критерий round 3, unit подстраховка),
app/pipeline/agents/claude_client.py (ClaudeAgentClient.run_agent), app/workers/retry_policy.py.

Покрывает критерий round 3 (unit подстраховка-классификация):
- ClaudeAgentClient.run_agent при невалидном ключе (SDK бросает встроенный TypeError на
  client-side auth-resolution ДО HTTP) → доменный LLMCredentialError (узкий version-agnostic
  перехват в точке SDK-вызова, не по подстроке сообщения);
- is_non_retryable_llm_failure(LLMCredentialError) = True (немедленный agent_unavailable без
  ретраев), is_llm_failure = True (reason agent_unavailable, не infra_error), is_transient = False
  (НЕ Celery-retry);
- посторонний TypeError из ДРУГОГО места (вне точки SDK-вызова в run_agent) НЕ конвертируется в
  LLMCredentialError и НЕ классифицируется как LLM-сбой — узость перехвата (не широкий матч через
  весь стек таски).

Внешняя граница (Anthropic SDK) изолирована: реальный HTTP к Anthropic НЕ вызывается —
self._client.messages.stream подменяется на бросок TypeError (точная модель SDK auth-resolution).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.config import get_settings
from app.pipeline.agents.claude_client import ClaudeAgentClient
from app.workers.retry_policy import (
    LLMCredentialError,
    is_llm_failure,
    is_non_retryable_llm_failure,
    is_transient,
)

# Текст встроенного TypeError Anthropic SDK при невозможности разрешить auth (ДО HTTP).
_SDK_AUTH_MSG = (
    "Could not resolve authentication method. Expected either api_key or auth_token to be set."
)


def _client() -> ClaudeAgentClient:
    """ClaudeAgentClient на cached Settings (конструктор создаёт AsyncAnthropic — без сети)."""
    return ClaudeAgentClient(get_settings())


# --- run_agent: TypeError SDK на auth-resolution → LLMCredentialError ---


@pytest.mark.asyncio
async def test_run_agent_typeerror_on_stream_becomes_llm_credential_error(monkeypatch):
    """SDK бросает TypeError при сборке заголовков (messages.stream) → LLMCredentialError.

    Модель прод-инцидента: при невалидном credential Anthropic SDK поднимает встроенный
    stdlib-TypeError на client-side auth-resolution ДО HTTP. run_agent перехватывает РОВНО в
    точке SDK-вызова и поднимает доменный LLMCredentialError (узкий version-agnostic матч).
    """
    client = _client()

    def _raise_typeerror(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        raise TypeError(_SDK_AUTH_MSG)

    # messages.stream вызывается синхронно в run_agent (до async with) — бросаем TypeError здесь.
    monkeypatch.setattr(client._client.messages, "stream", _raise_typeerror)

    with pytest.raises(LLMCredentialError):
        await client.run_agent(model="claude-opus-4-8", system_prompt="sys", user_content="u")


@pytest.mark.asyncio
async def test_run_agent_typeerror_from_get_final_message_becomes_llm_credential_error(monkeypatch):
    """TypeError из get_final_message (внутри async with stream) тоже → LLMCredentialError.

    Перехват охватывает весь блок первого SDK-вызова (stream + get_final_message), т.к. SDK
    валидирует auth ЛЕНИВО на первом запросе. Узкий матч по классу TypeError в этой точке.
    """
    client = _client()

    class _FakeStreamCtx:
        async def __aenter__(self):  # noqa: ANN202
            return self

        async def __aexit__(self, *exc):  # noqa: ANN002, ANN202
            return False

        async def get_final_message(self):  # noqa: ANN202
            raise TypeError(_SDK_AUTH_MSG)

    monkeypatch.setattr(client._client.messages, "stream", lambda *a, **k: _FakeStreamCtx())

    with pytest.raises(LLMCredentialError):
        await client.run_agent(model="claude-opus-4-8", system_prompt="sys", user_content="u")


@pytest.mark.asyncio
async def test_run_agent_llm_credential_error_chains_original_typeerror(monkeypatch):
    """LLMCredentialError сохраняет исходный TypeError в __cause__ (raise ... from exc)."""
    client = _client()

    original = TypeError(_SDK_AUTH_MSG)

    def _raise(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        raise original

    monkeypatch.setattr(client._client.messages, "stream", _raise)

    with pytest.raises(LLMCredentialError) as ei:
        await client.run_agent(model="claude-opus-4-8", system_prompt="s", user_content="u")
    assert ei.value.__cause__ is original


# --- классификация LLMCredentialError: non-retryable LLM, не transient ---


def test_llm_credential_error_is_non_retryable_llm_failure():
    """is_non_retryable_llm_failure(LLMCredentialError) = True → немедленный agent_unavailable."""
    assert is_non_retryable_llm_failure(LLMCredentialError("bad cred")) is True


def test_llm_credential_error_is_llm_failure():
    """is_llm_failure(LLMCredentialError) = True → reason agent_unavailable (не infra_error)."""
    assert is_llm_failure(LLMCredentialError("bad cred")) is True


def test_llm_credential_error_not_transient():
    """is_transient(LLMCredentialError) = False → НЕ Celery-retry (детерминированно падает)."""
    assert is_transient(LLMCredentialError("bad cred")) is False


# --- узость перехвата: посторонний TypeError НЕ становится LLM-сбоем ---


def test_unrelated_typeerror_not_classified_as_llm_failure():
    """Посторонний TypeError (баг в другом месте) НЕ классифицируется как LLM-сбой.

    Перехват в run_agent узкий — он оборачивает РОВНО точку SDK-вызова. Любой TypeError,
    возникший вне этой точки (в теле таски), остаётся обычным TypeError и НЕ трактуется
    классификатором §D как LLM-недоступность (не agent_unavailable, не «проглочен»).
    """
    foreign = TypeError("unrelated bug: NoneType is not callable")
    assert is_non_retryable_llm_failure(foreign) is False
    assert is_llm_failure(foreign) is False
    assert is_transient(foreign) is False


@pytest.mark.asyncio
async def test_run_agent_does_not_swallow_unrelated_typeerror_outside_sdk_call(monkeypatch):
    """TypeError, поднятый ВНЕ блока SDK-вызова, НЕ конвертируется в LLMCredentialError.

    Подстраховка ловит TypeError только вокруг первого SDK-обращения (stream/get_final_message).
    Если TypeError рождается ПОСЛЕ успешного получения message (в обработке usage/контента —
    напр. кривой mock без .usage), это НЕ auth-resolution и НЕ должен маскироваться под
    LLMCredentialError — узость перехвата по позиции в коде, не по типу через весь метод.
    """
    client = _client()

    class _OkStreamCtx:
        async def __aenter__(self):  # noqa: ANN202
            return self

        async def __aexit__(self, *exc):  # noqa: ANN002, ANN202
            return False

        async def get_final_message(self):  # noqa: ANN202
            # message без .content/.usage — пост-обработка в run_agent (НЕ в try-блоке SDK)
            # упадёт обычным TypeError/AttributeError, который НЕ конвертируется.
            return SimpleNamespace()

    monkeypatch.setattr(client._client.messages, "stream", lambda *a, **k: _OkStreamCtx())

    # Пост-обработка (доступ к message.content) падает вне SDK-try → НЕ LLMCredentialError.
    with pytest.raises((TypeError, AttributeError)):
        await client.run_agent(model="claude-opus-4-8", system_prompt="s", user_content="u")
