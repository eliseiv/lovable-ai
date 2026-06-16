"""Провайдер-агностичный контракт клиента агента + фабрика выбора (ADR-032 §1).

`AgentCall` — провайдер-нейтральный результат вызова агента (текст + учёт токенов/стоимости),
единый для обоих провайдеров (Anthropic заполняет `cache_write_tokens` write-ставкой, OpenAI —
всегда `0`, ADR-032 §4/§6). Тип не меняется относительно прежнего `claude_client.AgentCall` —
он переносится сюда как единый нейтральный источник, и оба клиента (`ClaudeAgentClient`,
`OpenAIAgentClient`) и `structured.py` импортируют его отсюда.

`LLMAgentClient` — структурный протокол (`typing.Protocol`, duck-typed, без новой внешней
зависимости): единственный метод `run_agent(...) -> AgentCall`. Слой агентов типизируется на
этот протокол, а не на конкретный класс провайдера.

`build_agent_client(settings)` — фабрика: выбирает реализацию по `settings.llm_provider`
(`anthropic` → `ClaudeAgentClient`, `openai` → `OpenAIAgentClient`). Иное значение → fail-fast
`LLMProviderConfigError` на старте (не молчаливый дефолт, ADR-032 §1).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from app.core.config import Settings


@dataclass(frozen=True)
class AgentCall:
    """Результат вызова агента: текст ответа + учёт токенов/стоимости (провайдер-нейтрален)."""

    text: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: Decimal


@dataclass(frozen=True)
class ImageInput:
    """Vision-вход агента (ADR-034 §D3): нейтральный для обоих провайдеров image content-блок.

    `media_type` — image MIME, выведенный из sniff magic bytes (`image/png`/`image/jpeg`/
    `image/webp`/`image/gif`, attachments_service). Anthropic-клиент маппит в base64 image-блок
    `messages[0].content`, OpenAI — в `input_image` data-URL `input`. Единый нейтральный источник
    рядом с `AgentCall`, импортируется обоими клиентами и structured.py.
    """

    data: bytes
    media_type: str


class LLMAgentClient(Protocol):
    """Провайдер-агностичный контракт клиента агента (ADR-032 §1).

    Вход одинаков для обоих провайдеров: `agent` ∈ {agent1..agent4} (ключ per-agent
    бюджета/effort), `model` (значение env `AGENTn_MODEL` — провайдер-специфичный ID),
    `system_prompt`, `user_content`, опц. `images` (vision-вход, ADR-034 §D3). Выход —
    нейтральный `AgentCall`. `images=None` (дефолт) ⇒ текстовый путь обоих провайдеров
    байт-в-байт прежний (инвариант обратной совместимости).
    """

    async def run_agent(
        self,
        *,
        agent: str,
        model: str,
        system_prompt: str,
        user_content: str,
        images: list[ImageInput] | None = None,
    ) -> AgentCall: ...


class LLMProviderConfigError(RuntimeError):
    """Невалидное значение `LLM_PROVIDER` (ADR-032 §1): fail-fast на старте, не молчаливый дефолт.

    Фабрика поднимает его, если `settings.llm_provider` не из {`anthropic`, `openai`}. Ошибка
    конфигурации мисконфигурированного инстанса — не доменный LLM-сбой, не ретраится.
    """


def build_agent_client(settings: Settings) -> LLMAgentClient:
    """Фабрика клиента агента по `settings.llm_provider` (ADR-032 §1, нормативно).

    `anthropic` (дефолт) → `ClaudeAgentClient` (поведение байт-в-байт прежнее, инвариант
    обратной совместимости); `openai` → `OpenAIAgentClient`; иное → `LLMProviderConfigError`
    (fail-fast, не молчаливый дефолт). Импорт конкретных клиентов — внутри функции, чтобы
    избежать циклического импорта (клиенты импортируют `AgentCall` отсюда).
    """
    provider = settings.llm_provider
    if provider == "anthropic":
        from app.pipeline.agents.claude_client import ClaudeAgentClient

        return ClaudeAgentClient(settings)
    if provider == "openai":
        from app.pipeline.agents.openai_client import OpenAIAgentClient

        return OpenAIAgentClient(settings)
    raise LLMProviderConfigError(
        f"invalid LLM_PROVIDER {provider!r}: expected 'anthropic' or 'openai'"
    )
