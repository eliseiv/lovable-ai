"""Unit: read/idle-таймаут LLM-клиентов (ADR-035) — httpx.Timeout на обоих клиентах + retry.

Источник истины:
- docs/adr/ADR-035-llm-stream-read-timeout.md §Decision (1)/(2) (форма httpx.Timeout:
  read/connect/write/pool; поля Settings llm_read_timeout_s=180.0, llm_connect_timeout_s=10.0;
  §Нормативный факт: APITimeoutError обоих SDK уже в TRANSIENT_EXCEPTIONS).
- docs/06-testing-strategy.md §Unit «Read/idle-таймаут LLM-клиента (ADR-035)» — сценарии (1)-(4).

Изоляция (правило qa.md): реальный сетевой вызов к LLM НЕ выполняется. Конструируем РЕАЛЬНЫЕ
AsyncAnthropic/AsyncOpenAI внутри клиентов (валидный замоканный ключ) и инспектируем
client._client.timeout — это httpx.Timeout-объект, который оба SDK хранят как есть (а не
скалярный/total timeout). Симметрия (ADR-032): оба клиента строятся одним кодом — таблица
сценариев параметризована по обоим.

Сценарии ТЗ/06-testing-strategy:
- (1)/(2) httpx.Timeout собран на клиенте с read=llm_read_timeout_s, connect=llm_connect_timeout_s,
  write==read, pool==read; именно httpx.Timeout-объект с раздельными полями, НЕ скаляр/total.
- (3) дефолты Settings: llm_read_timeout_s==180.0, llm_connect_timeout_s==10.0.
- (4) retry_policy: is_transient(anthropic.APITimeoutError(...)) и openai.APITimeoutError(...) ==
  True; оба класса присутствуют в TRANSIENT_EXCEPTIONS (против регресса удаления).
- (5) Anthropic-дефолт без регрессий: конструктор при валидном замоканном ключе не падает.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from anthropic import APITimeoutError as AnthropicAPITimeoutError
from openai import APITimeoutError as OpenAIAPITimeoutError

from app.core.config import Settings
from app.pipeline.agents.base import AgentCall
from app.pipeline.agents.claude_client import ClaudeAgentClient
from app.pipeline.agents.openai_client import OpenAIAgentClient
from app.workers.retry_policy import (
    TRANSIENT_EXCEPTIONS,
    is_non_retryable_llm_failure,
    is_transient,
)


def _anthropic_settings(**overrides) -> Settings:  # noqa: ANN003
    """Settings для Anthropic-пути (валидный непустой ключ — конструктор клиента не падает)."""
    base: dict = {"llm_provider": "anthropic", "anthropic_api_key": "sk-anthropic-test"}
    base.update(overrides)
    return Settings(**base)


def _openai_settings(**overrides) -> Settings:  # noqa: ANN003
    """Settings для OpenAI-пути (валидный непустой ключ)."""
    base: dict = {"llm_provider": "openai", "openai_api_key": "sk-openai-test"}
    base.update(overrides)
    return Settings(**base)


def _client_timeout(client) -> httpx.Timeout:  # noqa: ANN001
    """httpx.Timeout, с которым сконструирован SDK-клиент (оба SDK хранят его в .timeout)."""
    return client._client.timeout


# ---------------------------------------------------------------------------
# (1)/(2) httpx.Timeout собран на клиенте — read/connect/write/pool из Settings.
# Симметрия обоих SDK: одна параметризованная таблица (ADR-032, сценарий (4) docs).
# ---------------------------------------------------------------------------

_CLIENT_BUILDERS = [
    pytest.param(ClaudeAgentClient, _anthropic_settings, id="anthropic"),
    pytest.param(OpenAIAgentClient, _openai_settings, id="openai"),
]


@pytest.mark.parametrize("client_cls,settings_factory", _CLIENT_BUILDERS)
def test_client_timeout_is_httpx_timeout_object_not_scalar(client_cls, settings_factory):
    """timeout — httpx.Timeout-объект (раздельные поля), а НЕ скалярный/total timeout (§1).

    Скаляр (uniform/total) хранился бы клиентом как float; httpx.Timeout-объект подтверждает
    read/idle-семантику (раздельный read-компонент), не total-таймаут.
    """
    client = client_cls(settings_factory())
    timeout = _client_timeout(client)
    assert isinstance(timeout, httpx.Timeout)
    assert not isinstance(timeout, (int, float))


@pytest.mark.parametrize("client_cls,settings_factory", _CLIENT_BUILDERS)
def test_read_component_equals_llm_read_timeout_s(client_cls, settings_factory):
    """read == settings.llm_read_timeout_s (не хардкод; берётся из LLM_READ_TIMEOUT_S, §1/§2)."""
    settings = settings_factory(llm_read_timeout_s=200.0)
    client = client_cls(settings)
    timeout = _client_timeout(client)
    assert timeout.read == 200.0
    assert timeout.read == settings.llm_read_timeout_s


@pytest.mark.parametrize("client_cls,settings_factory", _CLIENT_BUILDERS)
def test_connect_component_equals_llm_connect_timeout_s(client_cls, settings_factory):
    """connect == settings.llm_connect_timeout_s (отдельный короткий таймаут, §1/§2)."""
    settings = settings_factory(llm_connect_timeout_s=7.0)
    client = client_cls(settings)
    timeout = _client_timeout(client)
    assert timeout.connect == 7.0
    assert timeout.connect == settings.llm_connect_timeout_s


@pytest.mark.parametrize("client_cls,settings_factory", _CLIENT_BUILDERS)
def test_write_and_pool_inherit_read(client_cls, settings_factory):
    """write/pool наследуют read (write==pool==read; короткий запрос, §1)."""
    settings = settings_factory(llm_read_timeout_s=150.0, llm_connect_timeout_s=9.0)
    client = client_cls(settings)
    timeout = _client_timeout(client)
    assert timeout.write == 150.0
    assert timeout.pool == 150.0
    assert timeout.write == settings.llm_read_timeout_s
    assert timeout.pool == settings.llm_read_timeout_s


@pytest.mark.parametrize("client_cls,settings_factory", _CLIENT_BUILDERS)
def test_read_distinct_from_connect_not_uniform_timeout(client_cls, settings_factory):
    """read != connect при разных дефолтах → раздельные компоненты, не uniform-таймаут (§1)."""
    settings = settings_factory(llm_read_timeout_s=180.0, llm_connect_timeout_s=10.0)
    client = client_cls(settings)
    timeout = _client_timeout(client)
    assert timeout.read == 180.0
    assert timeout.connect == 10.0
    assert timeout.read != timeout.connect


@pytest.mark.parametrize("client_cls,settings_factory", _CLIENT_BUILDERS)
def test_full_timeout_matches_explicit_httpx_timeout(client_cls, settings_factory):
    """Полная сборка timeout идентична httpx.Timeout(read=…, connect=…, write=read, pool=read)."""
    settings = settings_factory(llm_read_timeout_s=180.0, llm_connect_timeout_s=10.0)
    client = client_cls(settings)
    expected = httpx.Timeout(read=180.0, connect=10.0, write=180.0, pool=180.0)
    timeout = _client_timeout(client)
    assert timeout.read == expected.read
    assert timeout.connect == expected.connect
    assert timeout.write == expected.write
    assert timeout.pool == expected.pool


# ---------------------------------------------------------------------------
# (3) Дефолты Settings (env-контракт docs/07-deployment.md / ADR-035 §2).
# ---------------------------------------------------------------------------


def test_settings_default_read_timeout_is_180():
    """llm_read_timeout_s дефолт == 180.0 (float), env LLM_READ_TIMEOUT_S (§2)."""
    settings = Settings(anthropic_api_key="x")
    assert settings.llm_read_timeout_s == 180.0
    assert isinstance(settings.llm_read_timeout_s, float)


def test_settings_default_connect_timeout_is_10():
    """llm_connect_timeout_s дефолт == 10.0 (float), env LLM_CONNECT_TIMEOUT_S (§2)."""
    settings = Settings(anthropic_api_key="x")
    assert settings.llm_connect_timeout_s == 10.0
    assert isinstance(settings.llm_connect_timeout_s, float)


def test_default_clients_use_180_10_timeout():
    """С дефолтными Settings оба клиента собирают read=180.0 / connect=10.0 (§1/§2)."""
    anthropic_timeout = _client_timeout(ClaudeAgentClient(_anthropic_settings()))
    openai_timeout = _client_timeout(OpenAIAgentClient(_openai_settings()))
    for timeout in (anthropic_timeout, openai_timeout):
        assert timeout.read == 180.0
        assert timeout.connect == 10.0
        assert timeout.write == 180.0
        assert timeout.pool == 180.0


# ---------------------------------------------------------------------------
# (4) retry_policy: APITimeoutError обоих SDK уже транзиентны (§Нормативный факт).
# Повисший stream → SDK поднимает APITimeoutError → is_transient True → Celery-ретрай.
# ---------------------------------------------------------------------------


def _req() -> httpx.Request:
    return httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def test_anthropic_api_timeout_is_transient():
    """anthropic.APITimeoutError (повисший stream) → is_transient True → Celery-ретрай (§3)."""
    exc = AnthropicAPITimeoutError(request=_req())
    assert is_transient(exc) is True


def test_openai_api_timeout_is_transient():
    """openai.APITimeoutError (повисший stream) → is_transient True → Celery-ретрай (§3)."""
    exc = OpenAIAPITimeoutError(request=_req())
    assert is_transient(exc) is True


def test_both_api_timeout_classes_in_transient_exceptions():
    """Оба класса в TRANSIENT_EXCEPTIONS — против регресса удаления (чек-лист 06-testing)."""
    assert AnthropicAPITimeoutError in TRANSIENT_EXCEPTIONS
    assert OpenAIAPITimeoutError in TRANSIENT_EXCEPTIONS
    # Разные классы из разных пакетов (anthropic ≠ openai) — оба обязаны присутствовать.
    assert AnthropicAPITimeoutError is not OpenAIAPITimeoutError


# ---------------------------------------------------------------------------
# (5) Anthropic-дефолт без регрессий: конструктор при валидном замоканном ключе не падает,
# добавлен только timeout (никакие иные параметры конструктора не сломаны).
# ---------------------------------------------------------------------------


def test_anthropic_client_constructs_without_error_with_valid_key():
    """ClaudeAgentClient.__init__ при валидном (замоканном) ключе не падает (только timeout, §5)."""
    client = ClaudeAgentClient(_anthropic_settings())
    assert client._client is not None
    assert isinstance(_client_timeout(client), httpx.Timeout)


def test_openai_client_constructs_without_error_with_valid_key():
    """OpenAIAgentClient.__init__ при валидном (замоканном) ключе не падает (симметрия, §5)."""
    client = OpenAIAgentClient(_openai_settings())
    assert client._client is not None
    assert isinstance(_client_timeout(client), httpx.Timeout)


# ---------------------------------------------------------------------------
# (3) Межчанковый легитимный стрим НЕ рвётся (docs/06-testing-strategy.md строка 40, обязательна).
# Read/idle-таймаут ограничивает ТОЛЬКО паузу между чанками — не суммарную длительность стрима.
# Мок-стрим, прогрессирующий чанками (каждый сбрасывает read-таймер) суммарно дольше
# llm_read_timeout_s, завершается УСПЕШНО (get_final_*) без APITimeoutError — против регресса в
# total-таймаут (§1/§4). Реального sleep НЕТ (правило qa.md): семантика «таймер сбрасывается на
# каждом чанке, total не ограничен» проверяется детерминированно через мок прогрессирующего
# стрима, который штатно отдаёт финал (httpx не поднимает ReadTimeout, т.к. данные капали).
# ---------------------------------------------------------------------------


class _ProgressingAnthropicStream:
    """messages.stream(), который «капал» чанками и штатно отдал финальное сообщение.

    Имитирует длинный thinking-стрим: чанки приходили с интервалом < read-таймаут (read-таймер
    сбрасывался на каждом), суммарная длительность могла превысить read-таймаут — но т.к. данные
    прогрессировали, httpx НЕ поднял ReadTimeout, и get_final_message() вернул результат.
    """

    def __init__(self, message, chunk_count: int) -> None:  # noqa: ANN001
        self._message = message
        self._chunk_count = chunk_count

    async def __aenter__(self):  # noqa: ANN202
        return self

    async def __aexit__(self, *exc):  # noqa: ANN002, ANN202
        return False

    async def __aiter__(self):  # noqa: ANN202
        # Прогрессирующие чанки: каждый = «данные пришли» → read-таймер сброшен.
        for _ in range(self._chunk_count):
            yield SimpleNamespace(type="content_block_delta")

    async def get_final_message(self):  # noqa: ANN202
        return self._message


class _ProgressingOpenAIStream:
    """responses.stream(), который «капал» чанками и штатно отдал финальный response (симметрия)."""

    def __init__(self, response, chunk_count: int) -> None:  # noqa: ANN001
        self._response = response
        self._chunk_count = chunk_count

    async def __aenter__(self):  # noqa: ANN202
        return self

    async def __aexit__(self, *exc):  # noqa: ANN002, ANN202
        return False

    async def __aiter__(self):  # noqa: ANN202
        for _ in range(self._chunk_count):
            yield SimpleNamespace(type="response.output_text.delta")

    async def get_final_response(self):  # noqa: ANN202
        return self._response


def _anthropic_message():  # noqa: ANN202
    usage = SimpleNamespace(
        input_tokens=100,
        output_tokens=50,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    text_block = SimpleNamespace(type="text", text='{"questions": [{"text": "Q"}]}')
    return SimpleNamespace(content=[text_block], usage=usage)


def _openai_response():  # noqa: ANN202
    usage = SimpleNamespace(
        input_tokens=100,
        output_tokens=50,
        input_tokens_details=SimpleNamespace(cached_tokens=0),
    )
    return SimpleNamespace(output_text='{"questions": [{"text": "Q"}]}', usage=usage)


@pytest.mark.asyncio
async def test_anthropic_progressing_stream_not_aborted_returns_agent_call():
    """Длинный прогрессирующий Anthropic-стрим завершается успешно без APITimeoutError (§3)."""
    client = ClaudeAgentClient(_anthropic_settings(llm_read_timeout_s=180.0))
    msg = _anthropic_message()

    def _fake_stream(**kwargs):  # noqa: ANN003, ANN202
        # Много чанков (суммарно «дольше» read-таймаута) — но данные капают, таймер сброшен.
        return _ProgressingAnthropicStream(msg, chunk_count=50)

    client._client.messages.stream = _fake_stream  # type: ignore[method-assign]
    call = await client.run_agent(
        agent="agent2", model="claude-opus-4-8", system_prompt="sys", user_content="user"
    )
    # Стрим НЕ оборван read-таймаутом — вернулся валидный AgentCall (текст из block.text).
    assert isinstance(call, AgentCall)
    assert call.text == '{"questions": [{"text": "Q"}]}'


@pytest.mark.asyncio
async def test_openai_progressing_stream_not_aborted_returns_agent_call():
    """Длинный прогрессирующий OpenAI-стрим завершается без APITimeoutError (§3, симметрия)."""
    client = OpenAIAgentClient(_openai_settings(llm_read_timeout_s=180.0))
    resp = _openai_response()

    def _fake_stream(**kwargs):  # noqa: ANN003, ANN202
        return _ProgressingOpenAIStream(resp, chunk_count=50)

    client._client.responses.stream = _fake_stream  # type: ignore[method-assign]
    call = await client.run_agent(
        agent="agent2", model="gpt-5.5", system_prompt="sys", user_content="user"
    )
    assert isinstance(call, AgentCall)
    assert call.text == '{"questions": [{"text": "Q"}]}'


def test_timeout_has_no_finite_total_bound_capping_stream():
    """read-, НЕ total-таймаут: httpx.Timeout не несёт отдельного конечного total, ограничивающего
    суммарную длительность стрима — только покомпонентные read/connect/write/pool (§1/§4).

    httpx.Timeout не имеет компонента «total»; ограничение длительности задавалось бы общим
    скаляром (uniform) — но клиент хранит раздельные компоненты (test выше), значит суммарная
    длительность прогрессирующего стрима не ограничена. Здесь фиксируем, что все компоненты —
    из read/connect (а не искусственно занижены), т.е. конструктивно total-кэпа нет.
    """
    for client in (
        ClaudeAgentClient(
            _anthropic_settings(llm_read_timeout_s=180.0, llm_connect_timeout_s=10.0)
        ),
        OpenAIAgentClient(_openai_settings(llm_read_timeout_s=180.0, llm_connect_timeout_s=10.0)),
    ):
        timeout = _client_timeout(client)
        # read покрывает межчанковую паузу; нет компонента, ограничивающего ОБЩУЮ длительность.
        assert timeout.read == 180.0
        # connect — отдельный короткий; не путается с read (иначе был бы uniform/total-эффект).
        assert timeout.connect == 10.0


# ---------------------------------------------------------------------------
# (1) Повисший («молчащий») стрим → SDK поднимает APITimeoutError → проброс через run_agent
# реального клиента → is_transient True (Celery-ретрай, НЕ stuck/немедленный FAILED), для ОБОИХ
# SDK (docs/06-testing-strategy.md строка 38). Поведенческий end-to-end через РЕАЛЬНЫЙ клиент
# (не только проверка членства класса в TRANSIENT_EXCEPTIONS выше).
# ---------------------------------------------------------------------------


def _raising_anthropic_stream(exc: BaseException):  # noqa: ANN202
    def _factory(**kwargs):  # noqa: ANN003, ANN202
        raise exc

    return _factory


def _raising_openai_stream(exc: BaseException):  # noqa: ANN202
    def _factory(**kwargs):  # noqa: ANN003, ANN202
        raise exc

    return _factory


@pytest.mark.asyncio
async def test_anthropic_silent_stream_timeout_propagates_transient():
    """Молчащий Anthropic-стрим → APITimeoutError из run_agent → is_transient True (§1/§3)."""
    client = ClaudeAgentClient(_anthropic_settings())
    exc = AnthropicAPITimeoutError(request=_req())
    client._client.messages.stream = _raising_anthropic_stream(exc)  # type: ignore[method-assign]
    with pytest.raises(AnthropicAPITimeoutError) as ei:
        await client.run_agent(
            agent="agent2", model="claude-opus-4-8", system_prompt="sys", user_content="user"
        )
    assert is_transient(ei.value) is True
    assert is_non_retryable_llm_failure(ei.value) is False


@pytest.mark.asyncio
async def test_openai_silent_stream_timeout_propagates_transient():
    """Молчащий OpenAI-стрим → APITimeoutError из run_agent → is_transient True (§1/§4)."""
    client = OpenAIAgentClient(_openai_settings())
    exc = OpenAIAPITimeoutError(request=_req())
    client._client.responses.stream = _raising_openai_stream(exc)  # type: ignore[method-assign]
    with pytest.raises(OpenAIAPITimeoutError) as ei:
        await client.run_agent(
            agent="agent2", model="gpt-5.5", system_prompt="sys", user_content="user"
        )
    assert is_transient(ei.value) is True
    assert is_non_retryable_llm_failure(ei.value) is False
