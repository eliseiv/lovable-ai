"""Contract: пер-агентный token-бюджет + thinking-mode запроса (ADR-023, §I.5, ОБЯЗАТЕЛЬНЫЙ).

Источник истины — docs/modules/pipeline/03-architecture.md §Token-бюджет агентов (ADR-023) +
§I.5 (contract token-бюджет per-agent), docs/adr/ADR-023-agent3-token-budget-thinking-room.md
§Decision (1)/(2), docs/02-tech-stack.md (ceilings: Opus 128K / Sonnet 64K), docs/07-deployment.md
(дефолты AGENTn_MAX_TOKENS).

КРИТИЧНО против регресса прод-инцидента Agent 3 (детерминированный invalid_agent_output: все 3
попытки stop_reason=max_tokens при едином AGENT_MAX_TOKENS=16000 — усечение/обнуление вывода):

- (а) max_tokens КАЖДОГО агента == его AGENTn_MAX_TOKENS (agent1=16000, agent2=32000,
  agent3=56000, agent4=56000) И ≤ ceiling модели агента (Opus 128K / Sonnet 64K);
- (б) Agent 3 (Builder) И Agent 4 (Fixer/Editor) → thinking={"type":"disabled"} (детерминированная
  комната под вывод полного file-tree, R2 ADR-023 §Decision (4)); агенты 1/2 → adaptive;
- (в) НИГДЕ не собирается budget_tokens / {"type":"enabled",...} (HTTP 400 на Opus 4.8/4.7,
  deprecated на Sonnet 4.6).

Тест проверяет ФАКТИЧЕСКИ собранные kwargs реального ClaudeAgentClient.messages.stream — а НЕ
слепой мок run_agent. Перехват: подменяем messages.stream на сборщик kwargs (реальный HTTP к
Anthropic НЕ вызывается). Ловит регресс «max_tokens мал» / «thinking ест бюджет Builder».
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.config import get_settings
from app.pipeline.agents.claude_client import ClaudeAgentClient

pytestmark = pytest.mark.asyncio

# Ceilings моделей (docs/02-tech-stack §LLM, ADR-023 §Ограничение API → Нормативные числа моделей).
_MODEL_CEILING = {
    "claude-opus-4-8": 128_000,
    "claude-sonnet-4-6": 64_000,
}

# Нормативный маппинг агент → (env-ключ модели, ожидаемый thinking-mode).
# Модель берётся из реального Settings (AGENTn_MODEL) — НЕ хардкодим, чтобы тест следовал
# конфиг-маппингу (в т.ч. ревизии R1: agent3 → Sonnet).
# ADR-023 R2 (2026-06-12, §Decision (4)): Agent 4 (Fixer/Editor) thinking adaptive→disabled
# (как Builder) — детерминированная комната под вывод полного file-tree, против усечения дерева
# Agent 4 (прод-инцидент 31-мин правки agent_output_invalid). Agent 3 И Agent 4 → disabled,
# агенты 1/2 → adaptive (нормативный single source — docs/02-tech-stack §LLM ADR-023 R2,
# docs/modules/pipeline/03-architecture §Token-бюджет агентов, app/core/config.agent_thinking).
_AGENT_THINKING = {
    "agent1": {"type": "adaptive"},
    "agent2": {"type": "adaptive"},
    "agent3": {"type": "disabled"},
    "agent4": {"type": "disabled"},
}


def _agent_model(settings, agent: str) -> str:  # noqa: ANN001
    """Модель агента из реального Settings (AGENTn_MODEL) — следуем конфиг-маппингу, не хардкоду.

    После R1 (ADR-023 §Decision (3)): agent3 → Sonnet. agent1/3/4 → Sonnet, agent2 → Opus.
    """
    return {
        "agent1": settings.agent1_model,
        "agent2": settings.agent2_model,
        "agent3": settings.agent3_model,
        "agent4": settings.agent4_model,
    }[agent]


class _CapturedStreamCtx:
    """Async-context-manager, имитирующий messages.stream(): отдаёт фейковое финальное сообщение.

    Реальный HTTP к Anthropic НЕ выполняется — единственная цель перехвата собрать kwargs.
    """

    async def __aenter__(self):  # noqa: ANN202
        return self

    async def __aexit__(self, *exc):  # noqa: ANN002, ANN202
        return False

    async def get_final_message(self):  # noqa: ANN202
        usage = SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        )
        block = SimpleNamespace(type="text", text='{"files": []}')
        return SimpleNamespace(content=[block], usage=usage)


def _capturing_client():  # noqa: ANN202
    """Реальный ClaudeAgentClient с подменённым messages.stream — захват kwargs тела запроса."""
    client = ClaudeAgentClient(get_settings())
    captured: dict = {}

    def _fake_stream(**kwargs):  # noqa: ANN003, ANN202
        captured.clear()
        captured.update(kwargs)
        return _CapturedStreamCtx()

    client._client.messages.stream = _fake_stream  # type: ignore[method-assign]
    return client, captured


async def _capture_for(agent: str) -> dict:
    """Собрать kwargs реального messages.stream для одного агента (его модель из Settings)."""
    settings = get_settings()
    client, captured = _capturing_client()
    model = _agent_model(settings, agent)
    await client.run_agent(agent=agent, model=model, system_prompt="sys", user_content="user")
    return captured


@pytest.mark.parametrize(
    ("agent", "expected_max_tokens"),
    [
        ("agent1", 16000),
        ("agent2", 32000),
        ("agent3", 56000),
        ("agent4", 56000),
    ],
)
async def test_max_tokens_equals_per_agent_cap(agent, expected_max_tokens):
    """(а) max_tokens КАЖДОГО агента == AGENTn_MAX_TOKENS из §Token-бюджет (дефолты ADR-023)."""
    captured = await _capture_for(agent)
    assert captured["max_tokens"] == expected_max_tokens, (
        f"{agent}: max_tokens {captured['max_tokens']} != {expected_max_tokens} "
        f"(§Token-бюджет агентов / ADR-023). Регресс прод-инцидента Agent 3 (cap мал → усечение)."
    )


@pytest.mark.parametrize("agent", ["agent1", "agent2", "agent3", "agent4"])
async def test_max_tokens_within_model_ceiling(agent):
    """(а) max_tokens агента ≤ ceiling его модели (Opus 128K / Sonnet 64K).

    После R1 Builder — Sonnet (cap 56000 ≤ 64K с запасом). Запас от ceiling: ни один cap не равен
    и не превышает потолок (иначе сам запрос может 400/усечься у границы).
    """
    settings = get_settings()
    model = _agent_model(settings, agent)
    ceiling = _MODEL_CEILING[model]
    captured = await _capture_for(agent)
    assert captured["max_tokens"] <= ceiling, (
        f"{agent} ({model}): max_tokens {captured['max_tokens']} > ceiling {ceiling}"
    )


@pytest.mark.parametrize("agent", ["agent1", "agent2", "agent3", "agent4"])
async def test_thinking_mode_per_agent(agent):
    """(б) Agent 3 И Agent 4 → thinking=disabled (комната под вывод, R2); агенты 1/2 → adaptive."""
    captured = await _capture_for(agent)
    assert captured["thinking"] == _AGENT_THINKING[agent], (
        f"{agent}: thinking {captured['thinking']!r} != {_AGENT_THINKING[agent]!r} (ADR-023 §2)"
    )


async def test_agent3_thinking_disabled_gives_full_room():
    """Регресс прод-инцидента (attempt-2/3 text_len=0): Builder thinking=disabled — adaptive НЕ
    может съесть бюджет вывода. Конкретно проверяем именно Agent 3."""
    captured = await _capture_for("agent3")
    assert captured["thinking"] == {"type": "disabled"}
    assert captured["max_tokens"] == 56000


@pytest.mark.parametrize("agent", ["agent1", "agent2", "agent3", "agent4"])
async def test_no_budget_tokens_anywhere(agent):
    """(в) НИГДЕ не собирается thinking={"type":"enabled","budget_tokens":...} (HTTP 400 на
    Opus 4.8/4.7, deprecated на Sonnet 4.6 — ADR-023 §Ограничение API)."""
    captured = await _capture_for(agent)
    thinking = captured["thinking"]
    assert thinking.get("type") != "enabled", (
        f"{agent}: thinking type 'enabled' → HTTP 400 (ADR-023 §Ограничение API)"
    )
    assert "budget_tokens" not in thinking, (
        f"{agent}: budget_tokens собран в thinking → HTTP 400/deprecated (ADR-023)"
    )
