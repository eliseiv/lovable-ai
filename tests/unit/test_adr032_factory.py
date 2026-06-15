"""Unit: фабрика выбора LLM-провайдера build_agent_client (ADR-032 §1, docs §Unit ADR-032).

Источник истины — docs/adr/ADR-032-llm-provider-abstraction-openai.md §1 (контракт провайдера +
фабрика), docs/06-testing-strategy.md §Unit «LLM-провайдер: абстракция + фабрика».

Покрывает сценарий 1 ТЗ:
- LLM_PROVIDER=anthropic (и дефолт, ключ не задан) → ClaudeAgentClient;
- LLM_PROVIDER=openai → OpenAIAgentClient;
- иное (мусорное) значение → fail-fast LLMProviderConfigError (НЕ молчаливый дефолт).

Settings конструируется напрямую (без env-наследования) — провайдер задаётся аргументом
llm_provider, чтобы проверка была детерминирована и не зависела от окружения прогона.
"""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.pipeline.agents.base import LLMProviderConfigError, build_agent_client
from app.pipeline.agents.claude_client import ClaudeAgentClient
from app.pipeline.agents.openai_client import OpenAIAgentClient


def test_factory_anthropic_returns_claude_client():
    settings = Settings(llm_provider="anthropic")
    client = build_agent_client(settings)
    assert isinstance(client, ClaudeAgentClient)


def test_factory_default_provider_is_anthropic():
    """Дефолт (ключ не задан) → anthropic → ClaudeAgentClient (backward-compat, §1)."""
    settings = Settings()
    assert settings.llm_provider == "anthropic"
    client = build_agent_client(settings)
    assert isinstance(client, ClaudeAgentClient)


def test_factory_openai_returns_openai_client():
    settings = Settings(llm_provider="openai", openai_api_key="sk-openai-test")
    client = build_agent_client(settings)
    assert isinstance(client, OpenAIAgentClient)


@pytest.mark.parametrize("garbage", ["gpt", "OpenAI", "claude", "azure", "", "  ", "anthropic "])
def test_factory_invalid_provider_fail_fast(garbage):
    """Иное значение LLM_PROVIDER → LLMProviderConfigError (fail-fast, НЕ молчаливый дефолт)."""
    settings = Settings(llm_provider=garbage)
    with pytest.raises(LLMProviderConfigError):
        build_agent_client(settings)


def test_invalid_provider_error_message_names_value():
    """Сообщение об ошибке несёт невалидное значение (диагностируемость мисконфига)."""
    settings = Settings(llm_provider="bogus-provider")
    with pytest.raises(LLMProviderConfigError, match="bogus-provider"):
        build_agent_client(settings)
