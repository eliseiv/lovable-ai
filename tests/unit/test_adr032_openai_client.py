"""Unit/contract: OpenAIAgentClient — usage-маппинг, kwargs Responses API, error-проброс (ADR-032).

Источник истины — docs/adr/ADR-032-llm-provider-abstraction-openai.md §2/§4/§5/§6,
docs/06-testing-strategy.md §Unit «OpenAI usage/cost-маппинг» + «OpenAI параметры запроса
(contract на реальном теле, не слепой мок)» + «retry-классификация исключений OpenAI».

Реального сетевого вызова к OpenAI НЕТ: подменяем РОВНО self._client.responses.stream на
сборщик kwargs, возвращающий фейковый async-context с get_final_response() (как реальный SDK).
Так тест проверяет ФАКТИЧЕСКИ собранное тело запроса (kwargs) + маппинг usage финального
response в AgentCall — а не слепой мок run_agent.

Покрывает сценарии 2, 3, 4, 6 ТЗ:
- 3/4: usage→AgentCall (input/output/cache_read=cached_tokens/cache_write=0); kwargs
  responses.stream (max_output_tokens, reasoning.effort per-agent, instructions БЕЗ
  cache_control, input=user_content);
- 2 (транзиентный сбой): openai.RateLimitError(429)/APITimeoutError/APIConnectionError/
  APIStatusError(5xx), поднятые из responses.stream/get_final_response, пробрасываются из
  run_agent БЕЗ обёртки и классифицируются is_transient()==True (ведут к Celery-ретраю);
- 3/6 (невалидный непустой OPENAI_API_KEY): request-time openai.AuthenticationError(401)
  пробрасывается из run_agent БЕЗ обёртки (НЕ LLMCredentialError на openai-пути) и
  классифицируется is_non_retryable_llm_failure()==True → FAILED(agent_unavailable) без ретраев.

⚠️ НОВЫЙ контракт (ADR-032 §5, прод-фикс широкого `except OpenAIError`): OpenAI-клиент НЕ
оборачивает SDK-исключения — все они пробрасываются «as-is» в retry_policy (единственная точка
решения retry vs graceful-fail). Прежнее баговое поведение (run_agent оборачивал
AuthenticationError в LLMCredentialError, перемаппивая транзиентные 429/5xx/timeout в
non-retryable) УДАЛЕНО — тесты ниже фиксируют именно проброс без обёртки.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    RateLimitError,
)

from app.core.config import Settings
from app.pipeline.agents.base import AgentCall
from app.pipeline.agents.openai_client import OpenAIAgentClient
from app.workers.retry_policy import (
    LLMCredentialError,
    is_non_retryable_llm_failure,
    is_transient,
)

pytestmark = pytest.mark.asyncio


def _openai_settings(**overrides) -> Settings:  # noqa: ANN003
    base = {"llm_provider": "openai", "openai_api_key": "sk-openai-test"}
    base.update(overrides)
    return Settings(**base)


def _fake_usage(
    *, input_tokens: int, output_tokens: int, cached_tokens: int | None
) -> SimpleNamespace:
    """usage финального response Responses API: input/output + cached_tokens (§4)."""
    details = SimpleNamespace(cached_tokens=cached_tokens) if cached_tokens is not None else None
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        input_tokens_details=details,
    )


class _CapturedStreamCtx:
    """Async-context-manager, имитирующий responses.stream(): отдаёт фейковый финальный response.

    Реальный HTTP к OpenAI НЕ выполняется — цель: собрать kwargs + вернуть запрограммированный
    response с output_text/usage (как SDK get_final_response()).
    """

    def __init__(self, response) -> None:  # noqa: ANN001
        self._response = response

    async def __aenter__(self):  # noqa: ANN202
        return self

    async def __aexit__(self, *exc):  # noqa: ANN002, ANN202
        return False

    async def get_final_response(self):  # noqa: ANN202
        return self._response


def _capturing_client(
    *,
    settings: Settings | None = None,
    text: str = '{"questions": [{"text": "Q"}]}',
    usage: SimpleNamespace | None = None,
):  # noqa: ANN202
    """Реальный OpenAIAgentClient с подменённым responses.stream — захват kwargs тела запроса."""
    client = OpenAIAgentClient(settings or _openai_settings())
    captured: dict = {}
    if usage is None:
        usage = _fake_usage(input_tokens=100, output_tokens=50, cached_tokens=20)
    response = SimpleNamespace(output_text=text, usage=usage)

    def _fake_stream(**kwargs):  # noqa: ANN003, ANN202
        captured.clear()
        captured.update(kwargs)
        return _CapturedStreamCtx(response)

    # Подменяем РОВНО responses.stream (точка реального SDK-вызова в _stream_final_response).
    client._client.responses.stream = _fake_stream  # type: ignore[method-assign]
    return client, captured


# --- usage → AgentCall маппинг (§4) ---


async def test_usage_maps_to_agent_call_fields():
    """input/output/cache_read=cached_tokens/cache_write=0 (§4); cost по §2.2A."""
    usage = _fake_usage(input_tokens=1_000_000, output_tokens=0, cached_tokens=0)
    client, _ = _capturing_client(usage=usage)

    call = await client.run_agent(
        agent="agent1", model="gpt-5.4-mini", system_prompt="sys", user_content="user"
    )

    assert isinstance(call, AgentCall)
    assert call.input_tokens == 1_000_000
    assert call.output_tokens == 0
    assert call.cache_read_tokens == 0
    assert call.cache_write_tokens == 0  # OpenAI: всегда 0 (§4/§6)
    assert call.model == "gpt-5.4-mini"
    # 1M input gpt-5.4-mini = 0.75 (§2.2A).
    assert call.cost_usd == Decimal("0.7500")


async def test_cache_read_from_cached_tokens():
    """cache_read_tokens = usage.input_tokens_details.cached_tokens (§4)."""
    usage = _fake_usage(input_tokens=500, output_tokens=200, cached_tokens=123)
    client, _ = _capturing_client(usage=usage)

    call = await client.run_agent(
        agent="agent2", model="gpt-5.5", system_prompt="sys", user_content="user"
    )
    assert call.cache_read_tokens == 123
    assert call.cache_write_tokens == 0


async def test_cache_read_zero_when_details_missing():
    """Нет input_tokens_details (или cached_tokens) → cache_read=0 (защитный getattr, §4)."""
    usage = _fake_usage(input_tokens=10, output_tokens=5, cached_tokens=None)
    client, _ = _capturing_client(usage=usage)

    call = await client.run_agent(
        agent="agent3", model="gpt-5.4-mini", system_prompt="sys", user_content="user"
    )
    assert call.cache_read_tokens == 0


async def test_text_taken_from_output_text():
    client, _ = _capturing_client(text='{"spec_markdown": "# x"}')
    call = await client.run_agent(
        agent="agent2", model="gpt-5.5", system_prompt="sys", user_content="user"
    )
    assert call.text == '{"spec_markdown": "# x"}'


# --- kwargs Responses API: reasoning.effort per-agent (§2) ---


@pytest.mark.parametrize("agent", ["agent3", "agent4"])
async def test_reasoning_effort_none_for_agent3_4(agent):
    """Agent 3/4 → reasoning.effort=none (весь max_output_tokens под вывод, §2)."""
    client, captured = _capturing_client()
    await client.run_agent(
        agent=agent, model="gpt-5.4-mini", system_prompt="sys", user_content="user"
    )
    assert captured["reasoning"] == {"effort": "none"}


@pytest.mark.parametrize("agent", ["agent1", "agent2"])
async def test_reasoning_effort_config_for_agent1_2(agent):
    """Agent 1/2 → reasoning.effort = settings.openai_agent_effort (§2)."""
    settings = _openai_settings(openai_agent_effort="xhigh")
    client, captured = _capturing_client(settings=settings)
    await client.run_agent(agent=agent, model="gpt-5.5", system_prompt="sys", user_content="user")
    assert captured["reasoning"] == {"effort": "xhigh"}


async def test_reasoning_effort_default_high_for_agent1_2():
    """Дефолт openai_agent_effort = high (§2)."""
    client, captured = _capturing_client()  # default openai_agent_effort
    await client.run_agent(
        agent="agent1", model="gpt-5.4-mini", system_prompt="sys", user_content="user"
    )
    assert captured["reasoning"] == {"effort": "high"}


# --- kwargs Responses API: max_output_tokens / instructions / input (§2/§6) ---


@pytest.mark.parametrize(
    "agent,expected_cap",
    [("agent1", 16000), ("agent2", 32000), ("agent3", 56000), ("agent4", 56000)],
)
async def test_max_output_tokens_per_agent_cap(agent, expected_cap):
    """max_output_tokens = AGENTn_MAX_TOKENS (settings.agent_max_tokens(agent), §2)."""
    client, captured = _capturing_client()
    await client.run_agent(
        agent=agent, model="gpt-5.4-mini", system_prompt="sys", user_content="user"
    )
    assert captured["max_output_tokens"] == expected_cap


async def test_instructions_without_cache_control_and_input_is_user_content():
    """instructions = system_prompt БЕЗ cache_control; input = user_content (§6)."""
    client, captured = _capturing_client()
    await client.run_agent(
        agent="agent1",
        model="gpt-5.4-mini",
        system_prompt="SYSTEM-PROMPT",
        user_content="USER-CONTENT",
    )
    # instructions — плоская строка system-промта (НЕ список блоков, НЕ dict с cache_control).
    assert captured["instructions"] == "SYSTEM-PROMPT"
    assert not isinstance(captured["instructions"], (list, dict))
    assert captured["input"] == "USER-CONTENT"
    assert captured["model"] == "gpt-5.4-mini"


async def test_request_omits_anthropic_and_native_schema_keys():
    """Текстовый режим (§3): нет native text.format/json_schema, system/messages, cache_control."""
    client, captured = _capturing_client()
    await client.run_agent(
        agent="agent2", model="gpt-5.5", system_prompt="sys", user_content="user"
    )
    for forbidden in ("text", "system", "messages", "tools", "tool_choice", "cache_control"):
        assert forbidden not in captured, f"unexpected key {forbidden!r} in responses.stream kwargs"


def _http_response(status: int):  # noqa: ANN202
    return httpx.Response(
        status, request=httpx.Request("POST", "https://api.openai.com/v1/responses")
    )


def _stream_raising(exc: BaseException):  # noqa: ANN202
    """Подмена responses.stream, бросающая exc синхронно на вызове stream(...)."""

    def _raising(**kwargs):  # noqa: ANN003, ANN202
        raise exc

    return _raising


def _stream_raising_in_final(exc: BaseException):  # noqa: ANN202
    """Подмена responses.stream, чей get_final_response() бросает exc (внутри async with)."""

    class _Ctx:
        async def __aenter__(self):  # noqa: ANN202
            return self

        async def __aexit__(self, *e):  # noqa: ANN002, ANN202
            return False

        async def get_final_response(self):  # noqa: ANN202
            raise exc

    def _factory(**kwargs):  # noqa: ANN003, ANN202
        return _Ctx()

    return _factory


# --- credential (§5, НОВЫЙ контракт): невалидный непустой OPENAI_API_KEY → request-time
# AuthenticationError(401) ПРОБРАСЫВАЕТСЯ run_agent БЕЗ обёртки (НЕ LLMCredentialError) и
# классифицируется non-retryable → FAILED(agent_unavailable) без ретраев (сценарий 3 ТЗ). ---


async def test_invalid_credential_raises_authentication_error_unwrapped():
    """Невалидный непустой ключ → openai.AuthenticationError(401) проброшена БЕЗ обёртки (§5).

    НОВЫЙ контракт (прод-фикс широкого except OpenAIError): openai SDK при невалидном (непустом)
    ключе НЕ валидирует client-side — запрос уходит и возвращает request-time 401. run_agent
    НЕ оборачивает её в LLMCredentialError (в отличие от anthropic-пути client-side TypeError).
    """
    client = OpenAIAgentClient(_openai_settings())
    exc = AuthenticationError("invalid api key", response=_http_response(401), body=None)
    client._client.responses.stream = _stream_raising(exc)  # type: ignore[method-assign]

    with pytest.raises(AuthenticationError):
        await client.run_agent(
            agent="agent1", model="gpt-5.4-mini", system_prompt="sys", user_content="user"
        )


async def test_invalid_credential_not_wrapped_in_llm_credential_error():
    """run_agent НЕ поднимает LLMCredentialError на openai-пути (контраст с anthropic, §5)."""
    client = OpenAIAgentClient(_openai_settings())
    exc = AuthenticationError("invalid api key", response=_http_response(401), body=None)
    client._client.responses.stream = _stream_raising(exc)  # type: ignore[method-assign]

    # Именно AuthenticationError (а НЕ её обёртка LLMCredentialError) — иначе перемаппивание
    # исказило бы транзиентную классификацию (баг прод-инцидента, удалён).
    with pytest.raises(AuthenticationError) as ei:
        await client.run_agent(
            agent="agent1", model="gpt-5.4-mini", system_prompt="sys", user_content="user"
        )
    assert not isinstance(ei.value, LLMCredentialError)


async def test_invalid_credential_propagated_is_non_retryable():
    """Проброшенная AuthenticationError(401) → non-retryable True (FAILED без ретраев, §5)."""
    client = OpenAIAgentClient(_openai_settings())
    exc = AuthenticationError("401", response=_http_response(401), body=None)
    client._client.responses.stream = _stream_raising(exc)  # type: ignore[method-assign]

    with pytest.raises(AuthenticationError) as ei:
        await client.run_agent(
            agent="agent1", model="gpt-5.4-mini", system_prompt="sys", user_content="user"
        )
    assert is_non_retryable_llm_failure(ei.value) is True
    assert is_transient(ei.value) is False


# --- транзиентный OpenAI-сбой (сценарий 2 ТЗ): RateLimitError/APITimeoutError/
# APIConnectionError/APIStatusError(5xx), поднятые из stream/get_final_response,
# ПРОБРАСЫВАЮТСЯ run_agent БЕЗ обёртки и классифицируются is_transient True (Celery-ретрай,
# НЕ немедленный agent_unavailable). ---


def _req() -> httpx.Request:
    return httpx.Request("POST", "https://api.openai.com/v1/responses")


def _transient_excs() -> list[BaseException]:
    return [
        RateLimitError("429", response=_http_response(429), body=None),
        APITimeoutError(request=_req()),
        APIConnectionError(request=_req()),
        APIStatusError("503", response=_http_response(503), body=None),
        APIStatusError("500", response=_http_response(500), body=None),
    ]


@pytest.mark.parametrize("exc", _transient_excs(), ids=lambda e: type(e).__name__)
async def test_transient_from_stream_propagates_and_is_transient(exc):
    """Транзиентный сбой из responses.stream() → проброшен из run_agent, is_transient True (§5)."""
    client = OpenAIAgentClient(_openai_settings())
    client._client.responses.stream = _stream_raising(exc)  # type: ignore[method-assign]

    with pytest.raises(type(exc)) as ei:
        await client.run_agent(
            agent="agent3", model="gpt-5.4-mini", system_prompt="sys", user_content="user"
        )
    # Проброшено БЕЗ обёртки (тот же класс, не LLMCredentialError) → retry_policy ретраит.
    assert not isinstance(ei.value, LLMCredentialError)
    assert is_transient(ei.value) is True
    assert is_non_retryable_llm_failure(ei.value) is False


@pytest.mark.parametrize("exc", _transient_excs(), ids=lambda e: type(e).__name__)
async def test_transient_from_get_final_response_propagates_and_is_transient(exc):
    """Транзиентный сбой из get_final_response() (внутри async with) → проброшен, transient (§5)."""
    client = OpenAIAgentClient(_openai_settings())
    client._client.responses.stream = _stream_raising_in_final(exc)  # type: ignore[method-assign]

    with pytest.raises(type(exc)) as ei:
        await client.run_agent(
            agent="agent3", model="gpt-5.4-mini", system_prompt="sys", user_content="user"
        )
    assert not isinstance(ei.value, LLMCredentialError)
    assert is_transient(ei.value) is True
