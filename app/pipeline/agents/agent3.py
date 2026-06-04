"""Agent 3 (Builder): спека → дерево файлов статик-сайта.

Structured-output через форсированный tool-use + bounded retry (ADR-020 §I, общий слой
structured.py): структура читается из tool_use.input (не из текста), parse/schema-фейл
ретраится до AGENT_OUTPUT_MAX_RETRIES. Доменная валидация дерева (app.schemas.agent_output)
применяется ПОВЕРХ tool-use (§I.1) ДО упаковки source.tgz. На исчерпании ретраев невалидный
output → AgentOutputError → встраивается в семантику agent_output_invalid (§I.3).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import Settings
from app.pipeline.agents.claude_client import AgentCall, ClaudeAgentClient
from app.pipeline.agents.structured import (
    AGENT3_TOOL,
    DiagnosticsHook,
    GuardHook,
    UsageHook,
    run_structured_agent,
)
from app.pipeline.prompts import load_prompt
from app.schemas.agent_output import ValidatedTree, validate_agent_output

_SYSTEM_PROMPT = load_prompt("agent3_builder")


@dataclass(frozen=True)
class Agent3Result:
    tree: ValidatedTree
    call: AgentCall


async def run_agent3(
    settings: Settings,
    spec_markdown: str,
    *,
    before_call: GuardHook,
    after_call: UsageHook,
    on_attempt_failure: DiagnosticsHook,
) -> Agent3Result:
    """Один шаг Agent 3 (форсированный tool-use + bounded retry + доменная валидация, ADR-020 §I).

    Хуки инъектируются task-слоем (budget/wall-clock-гард, llm_usage, диагностика §I.4).
    validate_agent_output применяется к tool_use.input ПОВЕРХ tool-use (§I.1): tool-схема даёт
    JSON-форму, валидатор — доменную безопасность дерева (traversal/encoding/лимиты/allowlist).
    На исчерпании ретраев бросает AgentOutputError → task → agent_output_invalid-семантика (§I.3).
    """
    client = ClaudeAgentClient(settings)
    result = await run_structured_agent(
        settings,
        client,
        agent="agent3",
        model=settings.agent3_model,
        system_prompt=_SYSTEM_PROMPT,
        user_content=f"Specification:\n\n{spec_markdown}\n\nProduce the project file tree.",
        tool=AGENT3_TOOL,
        validate=lambda raw: validate_agent_output(raw, settings),
        before_call=before_call,
        after_call=after_call,
        on_attempt_failure=on_attempt_failure,
    )
    return Agent3Result(tree=result.value, call=result.call)
