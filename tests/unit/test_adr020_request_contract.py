"""Unit/contract: параметры запроса агента к Anthropic (ADR-020 revised §I.5, ОБЯЗАТЕЛЬНЫЙ).

Источник истины — docs/modules/pipeline/03-architecture.md §I.5 (contract на параметры запроса),
docs/06-testing-strategy.md §Unit «Structured-output агентов» (contract против регресса
HTTP-400 / 100%-отказа thinking⊥forced-tool), ADR-020 revised §Ограничение API.

КРИТИЧНО против регресса 100%-отказа: live-E2E вскрыл, что Anthropic API отвечает HTTP 400
«Thinking may not be enabled when tool_choice forces tool use» при `thinking=adaptive` +
форсирующем `tool_choice` ({"type":"tool"} / {"type":"any"}). Форсированный tool-use ОТОЗВАН.

Этот тест проверяет ФАКТИЧЕСКИ собранное тело запроса реального ClaudeAgentClient — kwargs,
передаваемые в `self._client.messages.stream(**kwargs)` — а НЕ слепой мок run_agent. Перехват:
подменяем сам `messages.stream` на сборщик kwargs (реальный HTTP к Anthropic НЕ вызывается),
инспектируем переданные аргументы. Ловит регресс, если кто-то вернёт `tools`/`tool_choice`.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.config import get_settings
from app.pipeline.agents.claude_client import ClaudeAgentClient

pytestmark = pytest.mark.asyncio


class _CapturedStreamCtx:
    """Async-context-manager, имитирующий messages.stream(): отдаёт фейковое финальное сообщение.

    Реальный HTTP к Anthropic НЕ выполняется — единственная цель перехвата собрать kwargs.
    """

    async def __aenter__(self):  # noqa: ANN202
        return self

    async def __aexit__(self, *exc):  # noqa: ANN002, ANN202
        return False

    async def get_final_message(self):  # noqa: ANN202
        # Минимальное сообщение с одним текстовым блоком + usage (как реальный SDK-ответ).
        usage = SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        )
        block = SimpleNamespace(type="text", text='{"questions": [{"text": "Q"}]}')
        return SimpleNamespace(content=[block], usage=usage)


def _capturing_client():  # noqa: ANN202
    """Реальный ClaudeAgentClient с подменённым messages.stream — захват kwargs тела запроса."""
    client = ClaudeAgentClient(get_settings())
    captured: dict = {}

    def _fake_stream(**kwargs):  # noqa: ANN003, ANN202
        captured.clear()
        captured.update(kwargs)
        return _CapturedStreamCtx()

    # Подменяем РОВНО messages.stream (точка реального SDK-вызова в _stream_final_message).
    client._client.messages.stream = _fake_stream  # type: ignore[method-assign]
    return client, captured


def _is_forcing_tool_choice(tc: object) -> bool:
    """Форсирующий tool_choice = {"type":"tool",...} или {"type":"any"} (несовместим с thinking)."""
    if not isinstance(tc, dict):
        return False
    return tc.get("type") in ("tool", "any")


async def test_request_has_no_forcing_tool_choice_with_thinking():
    """ФАКТИЧЕСКИ собранные kwargs messages.stream НЕ содержат форсирующего tool_choice
    одновременно с thinking (ADR-020 revised §I.5 — против HTTP 400 / 100%-отказа)."""
    client, captured = _capturing_client()

    await client.run_agent(
        agent="agent1", model="claude-sonnet-4-6", system_prompt="sys", user_content="user"
    )

    # thinking присутствует (adaptive у агентов 1/2/4 — ценен для качества; ADR-023).
    assert captured.get("thinking") == {"type": "adaptive"}
    # Несовместимая комбинация исключена: либо tool_choice отсутствует, либо НЕ форсирует.
    tc = captured.get("tool_choice")
    assert not _is_forcing_tool_choice(tc), (
        f"forcing tool_choice {tc!r} together with thinking → HTTP 400 (ADR-020 §Ограничение API)"
    )


async def test_request_omits_tools_and_tool_choice_entirely():
    """Текстовый режим (§I.1 revised): tools/tool_choice ВООБЩЕ не передаются в тело запроса."""
    client, captured = _capturing_client()

    await client.run_agent(
        agent="agent2", model="claude-opus-4-8", system_prompt="sys", user_content="user"
    )

    assert "tools" not in captured
    assert "tool_choice" not in captured


async def test_request_carries_thinking_and_output_config():
    """Тело запроса несёт thinking=adaptive + output_config={effort} (текстовый режим §I.1)."""
    client, captured = _capturing_client()
    settings = get_settings()

    await client.run_agent(
        agent="agent2", model="claude-opus-4-8", system_prompt="sys", user_content="user"
    )

    assert captured["thinking"] == {"type": "adaptive"}
    assert captured["output_config"] == {"effort": settings.agent_effort}
    # Системный промт кэшируется (ephemeral) — стабильная часть.
    assert captured["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert captured["system"][0]["text"] == "sys"
