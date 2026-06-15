"""Unit: ветвление active_llm_api_key() по LLM_PROVIDER (ADR-032 §5, config.py).

Источник истины — docs/adr/ADR-032-llm-provider-abstraction-openai.md §5 (preflight по credential
АКТИВНОГО провайдера), app/core/config.py Settings.active_llm_api_key().

Покрывает часть сценария 5 ТЗ (чистый предикат, без I/O):
- LLM_PROVIDER=openai → возвращает OPENAI_API_KEY (распакованный);
- LLM_PROVIDER=anthropic (и дефолт) → возвращает ANTHROPIC_API_KEY;
- пустой ключ активного провайдера → пустая строка (preflight отсечёт через llm_credential_present).
"""

from __future__ import annotations

from app.core.config import Settings
from app.workers.retry_policy import llm_credential_present


def test_active_key_openai_returns_openai_key():
    settings = Settings(
        llm_provider="openai",
        openai_api_key="sk-openai-xyz",
        anthropic_api_key="sk-ant-should-not-be-used",
    )
    assert settings.active_llm_api_key() == "sk-openai-xyz"


def test_active_key_anthropic_returns_anthropic_key():
    settings = Settings(
        llm_provider="anthropic",
        openai_api_key="sk-openai-should-not-be-used",
        anthropic_api_key="sk-ant-abc",
    )
    assert settings.active_llm_api_key() == "sk-ant-abc"


def test_active_key_default_provider_is_anthropic_key():
    settings = Settings(anthropic_api_key="sk-ant-default")
    assert settings.active_llm_api_key() == "sk-ant-default"


def test_active_key_openai_empty_when_unset():
    """LLM_PROVIDER=openai + пустой OPENAI_API_KEY → пустая строка → preflight-fail."""
    settings = Settings(llm_provider="openai", anthropic_api_key="sk-ant-nonempty")
    assert settings.active_llm_api_key() == ""
    assert llm_credential_present(settings.active_llm_api_key()) is False


def test_active_key_openai_present_passes_preflight():
    settings = Settings(llm_provider="openai", openai_api_key="sk-openai-valid")
    assert llm_credential_present(settings.active_llm_api_key()) is True


def test_active_key_anthropic_empty_when_unset_under_openai_irrelevant():
    """Пустой ANTHROPIC_API_KEY НЕ блокирует, если активный провайдер openai с валидным ключом."""
    settings = Settings(
        llm_provider="openai", openai_api_key="sk-openai-valid", anthropic_api_key=""
    )
    assert llm_credential_present(settings.active_llm_api_key()) is True
