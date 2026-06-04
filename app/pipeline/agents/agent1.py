"""Agent 1 (Interviewer): промт → список уточняющих вопросов.

Output → таблица questions (docs/modules/pipeline/03-architecture.md). Structured-output
через форсированный tool-use + bounded retry (ADR-020 §I, общий слой structured.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.config import Settings
from app.pipeline.agents.claude_client import AgentCall, ClaudeAgentClient
from app.pipeline.agents.structured import (
    AGENT1_TOOL,
    FAIL_CLASS_SCHEMA,
    DiagnosticsHook,
    GuardHook,
    StructuredOutputError,
    UsageHook,
    run_structured_agent,
)
from app.pipeline.prompts import load_prompt

_SYSTEM_PROMPT = load_prompt("agent1_interviewer")


@dataclass(frozen=True)
class ParsedQuestion:
    position: int
    text: str
    kind: str | None
    options: list[Any] | None


@dataclass(frozen=True)
class Agent1Result:
    questions: list[ParsedQuestion]
    call: AgentCall


def _validate_questions(data: Any) -> list[ParsedQuestion]:
    """Доменная валидация структуры Agent 1 поверх tool-use (ADR-020 §I.1).

    tool-use гарантирует JSON-форму; здесь — доменные правила контракта questions (непустой
    список, обязательный непустой text, корректный kind/options). Нарушение → schema-фейл
    (re-семплируемый, ретраится; на исчерпании — FAILED(invalid_agent_output), §I.3).
    """
    raw = data.get("questions") if isinstance(data, dict) else None
    if not isinstance(raw, list) or not raw:
        raise StructuredOutputError(
            "agent1 output missing non-empty 'questions'", fail_class=FAIL_CLASS_SCHEMA
        )

    questions: list[ParsedQuestion] = []
    for idx, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise StructuredOutputError(
                "agent1 question must be an object", fail_class=FAIL_CLASS_SCHEMA
            )
        q_text = item.get("text")
        if not isinstance(q_text, str) or not q_text.strip():
            raise StructuredOutputError(
                "agent1 question text must be a non-empty string", fail_class=FAIL_CLASS_SCHEMA
            )
        position = item.get("position")
        if not isinstance(position, int):
            position = idx
        kind = item.get("kind")
        kind = kind if kind in ("free_text", "choice") else None
        options = item.get("options")
        if kind == "choice":
            if not isinstance(options, list) or not options:
                raise StructuredOutputError(
                    "agent1 choice question must have non-empty options",
                    fail_class=FAIL_CLASS_SCHEMA,
                )
        else:
            options = None
        questions.append(ParsedQuestion(position=position, text=q_text, kind=kind, options=options))
    return questions


async def run_agent1(
    settings: Settings,
    prompt: str,
    *,
    before_call: GuardHook,
    after_call: UsageHook,
    on_attempt_failure: DiagnosticsHook,
) -> Agent1Result:
    """Один шаг Agent 1 (форсированный tool-use + bounded retry, ADR-020 §I).

    before_call/after_call/on_attempt_failure инъектируются task-слоем (budget/wall-clock-гард
    перед каждым вызовом; запись llm_usage после каждого; диагностика parse/schema-фейла §I.4).
    На исчерпании ретраев бросает StructuredOutputError → task → FAILED(invalid_agent_output).
    """
    client = ClaudeAgentClient(settings)
    result = await run_structured_agent(
        settings,
        client,
        agent="agent1",
        model=settings.agent1_model,
        system_prompt=_SYSTEM_PROMPT,
        user_content=f"User website idea:\n\n{prompt}",
        tool=AGENT1_TOOL,
        validate=_validate_questions,
        before_call=before_call,
        after_call=after_call,
        on_attempt_failure=on_attempt_failure,
    )
    return Agent1Result(questions=result.value, call=result.call)
