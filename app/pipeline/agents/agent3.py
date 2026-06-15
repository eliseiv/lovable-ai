"""Agent 3 (Builder): спека → дерево файлов статик-сайта.

Structured-output через текстовый режим + строгий промт + extract_json + bounded retry
(ADR-020 §I, revised; общий слой structured.py): структура извлекается из block.text хелпером
extract_json, parse/schema-фейл ретраится до AGENT_OUTPUT_MAX_RETRIES. Доменная валидация
дерева (app.schemas.agent_output) применяется ПОВЕРХ извлечённой структуры (§I.2) ДО упаковки
source.tgz. На исчерпании ретраев невалидный output → AgentOutputError → встраивается в
семантику agent_output_invalid (§I.3).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import Settings
from app.pipeline.agents.base import AgentCall, build_agent_client
from app.pipeline.agents.structured import (
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
    """Один шаг Agent 3 (текстовый режим + строгий промт + extract_json + bounded retry +
    доменная валидация, ADR-020 §I).

    Хуки инъектируются task-слоем (budget/wall-clock-гард, llm_usage, диагностика §I.4).
    validate_agent_output применяется к извлечённой структуре ПОВЕРХ extract_json (§I.2):
    extract_json даёт JSON-форму, валидатор — доменную безопасность дерева
    (traversal/encoding/лимиты/allowlist).
    На исчерпании ретраев бросает AgentOutputError → task → agent_output_invalid-семантика (§I.3).
    """
    client = build_agent_client(settings)
    result = await run_structured_agent(
        settings,
        client,
        agent="agent3",
        model=settings.agent3_model,
        system_prompt=_SYSTEM_PROMPT,
        user_content=f"Specification:\n\n{spec_markdown}\n\nProduce the project file tree.",
        validate=lambda raw: validate_agent_output(raw, settings),
        before_call=before_call,
        after_call=after_call,
        on_attempt_failure=on_attempt_failure,
    )
    return Agent3Result(tree=result.value, call=result.call)
