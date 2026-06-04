"""Agent 2 (Spec Writer): промт + ответы → финальная спека (ТЗ, Markdown).

Output → generation_jobs.spec_tz (inline ≤ 16 KB) или spec_ref в S3. Structured-output
через форсированный tool-use + bounded retry (ADR-020 §I, общий слой structured.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.config import Settings
from app.pipeline.agents.claude_client import AgentCall, ClaudeAgentClient
from app.pipeline.agents.structured import (
    AGENT2_TOOL,
    FAIL_CLASS_SCHEMA,
    DiagnosticsHook,
    GuardHook,
    StructuredOutputError,
    UsageHook,
    run_structured_agent,
)
from app.pipeline.prompts import load_prompt

_SYSTEM_PROMPT = load_prompt("agent2_spec_writer")


@dataclass(frozen=True)
class Agent2Result:
    spec_markdown: str
    call: AgentCall


def _build_user_content(prompt: str, qa_pairs: list[tuple[str, str]]) -> str:
    lines = [f"Original user prompt:\n{prompt}\n", "Clarifying questions and answers:"]
    for idx, (question, answer) in enumerate(qa_pairs, start=1):
        lines.append(f"{idx}. Q: {question}")
        lines.append(f"   A: {answer}")
    lines.append("\nWrite the specification now.")
    return "\n".join(lines)


def _validate_spec(data: Any) -> str:
    """Доменная валидация структуры Agent 2 поверх tool-use (ADR-020 §I.1): непустая спека.

    Нарушение → schema-фейл (re-семплируемый; на исчерпании — FAILED(invalid_agent_output)).
    """
    spec = data.get("spec_markdown") if isinstance(data, dict) else None
    if not isinstance(spec, str) or not spec.strip():
        raise StructuredOutputError(
            "agent2 produced an empty specification", fail_class=FAIL_CLASS_SCHEMA
        )
    return spec.strip()


async def run_agent2(
    settings: Settings,
    prompt: str,
    qa_pairs: list[tuple[str, str]],
    *,
    before_call: GuardHook,
    after_call: UsageHook,
    on_attempt_failure: DiagnosticsHook,
) -> Agent2Result:
    """Один шаг Agent 2 (форсированный tool-use + bounded retry, ADR-020 §I).

    Хуки инъектируются task-слоем (budget/wall-clock-гард, llm_usage, диагностика §I.4).
    На исчерпании ретраев бросает StructuredOutputError → task → FAILED(invalid_agent_output).
    """
    client = ClaudeAgentClient(settings)
    result = await run_structured_agent(
        settings,
        client,
        agent="agent2",
        model=settings.agent2_model,
        system_prompt=_SYSTEM_PROMPT,
        user_content=_build_user_content(prompt, qa_pairs),
        tool=AGENT2_TOOL,
        validate=_validate_spec,
        before_call=before_call,
        after_call=after_call,
        on_attempt_failure=on_attempt_failure,
    )
    return Agent2Result(spec_markdown=result.value, call=result.call)
