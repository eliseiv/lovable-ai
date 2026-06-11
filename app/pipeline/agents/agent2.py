"""Agent 2 (Spec Writer): промт + ответы → финальная спека (ТЗ, Markdown).

Output → generation_jobs.spec_tz (inline ≤ 16 KB) или spec_ref в S3. Structured-output
через текстовый режим + строгий промт + extract_json + bounded retry (ADR-020 §I, revised;
общий слой structured.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.config import Settings
from app.pipeline.agents.claude_client import AgentCall, ClaudeAgentClient
from app.pipeline.agents.structured import (
    FAIL_CLASS_SCHEMA,
    DiagnosticsHook,
    GuardHook,
    StructuredOutputError,
    UsageHook,
    run_structured_agent,
)
from app.pipeline.language import DetectedLanguage
from app.pipeline.prompts import load_prompt

_SYSTEM_PROMPT = load_prompt("agent2_spec_writer")

# Маркер языка контента, которым ОБЯЗАН начинаться spec_markdown (ADR-025, §Язык/локализация,
# §I.1a). Прокидывает detected content language downstream через уже передаваемый spec_markdown
# (Agent 3 извлекает из него BCP-47 для <html lang>); новых колонок/полей/параметров НЕ вводится.
_CONTENT_LANGUAGE_MARKER = "**Content language:**"


@dataclass(frozen=True)
class Agent2Result:
    spec_markdown: str
    call: AgentCall


def _build_user_content(
    prompt: str, qa_pairs: list[tuple[str, str]], language: DetectedLanguage
) -> str:
    # Серверная language-директива (ADR-028 §3): язык — детерминированный детект из исходного
    # промпта (content_language), НЕ само-детект модели. Маркер `**Content language:**` в начале
    # spec_markdown обязан нести именно это значение (ADR-028 §3/§5).
    directive = (
        f"Generate all user-facing content in {language.marker_value}. "
        f"Begin the spec_markdown value with the exact marker line "
        f"`{_CONTENT_LANGUAGE_MARKER} {language.marker_value}`."
    )
    lines = [
        directive,
        f"\nOriginal user prompt:\n{prompt}\n",
        "Clarifying questions and answers:",
    ]
    for idx, (question, answer) in enumerate(qa_pairs, start=1):
        lines.append(f"{idx}. Q: {question}")
        lines.append(f"   A: {answer}")
    lines.append("\nWrite the specification now.")
    return "\n".join(lines)


def _validate_spec(data: Any) -> str:
    """Доменная валидация структуры Agent 2 поверх извлечённого JSON (ADR-020 §I.2).

    Проверяет: (1) непустую спеку под ключом `spec_markdown` (§I.1a); (2) что спека
    НАЧИНАЕТСЯ маркером языка контента `**Content language:**` (ADR-025, §Язык/локализация,
    §I.5 — минимальная валидная форма теперь С маркером). Маркер живёт внутри значения
    `spec_markdown`; top-level ключ output не меняется.

    Любое нарушение → тот же schema-фейл, что и для прочих нарушений контракта Agent 2
    (re-семплируемый; на исчерпании — FAILED(invalid_agent_output)).
    """
    spec = data.get("spec_markdown") if isinstance(data, dict) else None
    if not isinstance(spec, str) or not spec.strip():
        raise StructuredOutputError(
            "agent2 produced an empty specification", fail_class=FAIL_CLASS_SCHEMA
        )
    spec = spec.strip()
    if not spec.startswith(_CONTENT_LANGUAGE_MARKER):
        raise StructuredOutputError(
            "agent2 spec_markdown must begin with the "
            f"'{_CONTENT_LANGUAGE_MARKER}' content-language marker (ADR-025)",
            fail_class=FAIL_CLASS_SCHEMA,
        )
    return spec


async def run_agent2(
    settings: Settings,
    prompt: str,
    qa_pairs: list[tuple[str, str]],
    language: DetectedLanguage,
    *,
    before_call: GuardHook,
    after_call: UsageHook,
    on_attempt_failure: DiagnosticsHook,
) -> Agent2Result:
    """Один шаг Agent 2 (текстовый режим + строгий промт + extract_json + bounded retry, §I).

    `language` — серверный детерминированный детект языка из исходного промпта (ADR-028);
    инжектируется явной директивой; значение маркера `**Content language:**` приходит отсюда,
    НЕ из само-детекта модели.
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
        user_content=_build_user_content(prompt, qa_pairs, language),
        validate=_validate_spec,
        before_call=before_call,
        after_call=after_call,
        on_attempt_failure=on_attempt_failure,
    )
    return Agent2Result(spec_markdown=result.value, call=result.call)
