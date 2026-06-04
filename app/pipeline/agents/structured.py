"""Надёжный structured-output всех 4 агентов (ADR-020, docs pipeline §I).

ЕДИНЫЙ слой для Agent 1/2/3/4 — НЕ дублируется в каждом агенте. Три части:

(1) Форсированный tool-use — основной механизм (детерминизм, §I.1). Вызов
    `ClaudeAgentClient.run_agent_tool` с tool_choice (forced) + input_schema на агента;
    структура читается из `tool_use.input`, НЕ из текстового блока. Устраняет markdown-фенсы
    как класс (прод-баг §I: ~40% ответов модели приходили в ```json…``` → строгий
    json.loads(call.text) → ValueError → немедленный FAILED без ретрая).
(2) Толерантный парсинг (§I.2, defence-in-depth) — `extract_json` на текстовый ответ, если
    структура всё же пришла текстом (граничный случай / отказ tool-use / будущие версии SDK):
    снятие ```json/``` -фенсов + извлечение первого сбалансированного JSON перед json.loads.
(3) Bounded retry на parse/schema-фейл (§I.3) — `run_structured_agent` ретраит НОВЫЙ LLM-вызов
    того же агента до AGENT_OUTPUT_MAX_RETRIES раз ВНУТРИ шага агента (НЕ Celery-retry, НЕ
    FIXING). Перед КАЖДЫМ вызовом — guard-хук (budget/wall-clock §C(b)/(c) считают retry-вызовы);
    после КАЖДОГО вызова — usage-хук (запись llm_usage + spend). При каждом фейле — diag-хук
    (имя агента/attempt/класс/текст ошибки/scrubbed усечённый raw в job_events.payload, §I.4).

Доменная валидация (особенно дерево Agent 3) — поверх tool-use, НЕ заменяется им (§I.1):
вызывающий передаёт `validate`-колбэк, который применяет прежний валидатор к tool_input.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.core.config import Settings
from app.observability.sentry import scrub_text
from app.observability.timing import timed_agent_call
from app.pipeline.agents.claude_client import AgentCall, AgentToolCall, ClaudeAgentClient

# Классы фейла structured-output (§I.4): parse — структура не извлеклась; schema — извлеклась,
# но не прошла JSON-схему инструмента/доменную валидацию.
FAIL_CLASS_PARSE = "parse_error"
FAIL_CLASS_SCHEMA = "schema_error"


@dataclass(frozen=True)
class AgentToolSpec:
    """Спецификация инструмента structured-output одного агента (ADR-020 §I.1).

    `tool_name` — имя форсируемого инструмента; `input_schema` — JSON-схема выхода агента
    (транспорт структуры; полная доменная валидация — отдельным validate-колбэком).
    """

    tool_name: str
    input_schema: dict[str, Any]


# --- Tool-схемы на агента (§I.1). Транспорт структуры; доменная валидация — поверх. ---

# Agent 1 (Interviewer) — submit_questions. questions[]: каждый — объект с обязательным text;
# опц. position/kind(free_text|choice)/options[]. Доп. валидация контракта — в agent1._parse.
AGENT1_TOOL = AgentToolSpec(
    tool_name="submit_questions",
    input_schema={
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "position": {"type": "integer"},
                        "kind": {"type": "string", "enum": ["free_text", "choice"]},
                        "options": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["text"],
                },
            }
        },
        "required": ["questions"],
    },
)

# Agent 2 (Spec writer) — submit_spec. spec_markdown: финальная спека (spec_tz-форма, Markdown).
AGENT2_TOOL = AgentToolSpec(
    tool_name="submit_spec",
    input_schema={
        "type": "object",
        "properties": {"spec_markdown": {"type": "string"}},
        "required": ["spec_markdown"],
    },
)

# Схема файла дерева (Agent 3/4): path/encoding/content. Доменные правила (traversal/allowlist/
# лимиты/dotfiles) — НЕ выражаются JSON-схемой, проверяются validate_agent_output поверх (§I.1).
_TREE_FILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "encoding": {"type": "string", "enum": ["utf8", "base64"]},
        "content": {"type": "string"},
    },
    "required": ["path", "encoding", "content"],
}
_BUILD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tool": {"type": "string"},
        "command": {"type": "string"},
        "output_dir": {"type": "string"},
    },
    "required": ["command"],
}

# Agent 3 (Builder) — submit_project: дерево agent_output (files[]/entry/build).
AGENT3_TOOL = AgentToolSpec(
    tool_name="submit_project",
    input_schema={
        "type": "object",
        "properties": {
            "files": {"type": "array", "items": _TREE_FILE_SCHEMA},
            "entry": {"type": "string"},
            "build": _BUILD_SCHEMA,
        },
        "required": ["files", "entry", "build"],
    },
)

# Agent 4 (Fixer/editor) — submit_project: та же схема agent_output ПЛЮС ветка unrecoverable
# (§A) — «неисправимо» выражается полями ОДНОГО инструмента, tool_choice форсирует tool,
# ветка выбирается полями (не отсутствием вызова). files/entry/build тут НЕ required: при
# unrecoverable=true их нет; доменная валидация дерева применяется только когда не-unrecoverable.
AGENT4_TOOL = AgentToolSpec(
    tool_name="submit_project",
    input_schema={
        "type": "object",
        "properties": {
            "files": {"type": "array", "items": _TREE_FILE_SCHEMA},
            "entry": {"type": "string"},
            "build": _BUILD_SCHEMA,
            "unrecoverable": {"type": "boolean"},
            "reason": {"type": "string"},
            "explanation": {"type": "string"},
        },
    },
)


def extract_json(text: str) -> Any:
    """Толерантный парсинг текстового ответа модели (ADR-020 §I.2, defence-in-depth).

    Снимает обёртку ```json … ``` / ``` … ``` (любой/без language-tag) + извлекает ПЕРВЫЙ
    сбалансированный JSON-объект/массив (срезает ведущую/хвостовую прозу), затем json.loads.
    Минимально, версионно-устойчиво, без regex-парсинга всего тела. Бросает ValueError, если
    валидный JSON извлечь не удалось.
    """
    stripped = _strip_code_fence(text.strip())
    candidate = _first_balanced_json(stripped)
    if candidate is None:
        raise ValueError("no balanced JSON object/array found in model text")
    return json.loads(candidate)


def _strip_code_fence(text: str) -> str:
    """Снимает обрамляющую markdown-fence ```json … ``` / ``` … ``` (если есть)."""
    if not text.startswith("```"):
        return text
    # Отрезаем открывающую строку фенса (```json / ``` + опц. language-tag до перевода строки).
    newline = text.find("\n")
    if newline == -1:
        return text
    inner = text[newline + 1 :]
    close = inner.rfind("```")
    if close == -1:
        return inner.strip()
    return inner[:close].strip()


def _first_balanced_json(text: str) -> str | None:
    """Извлекает первый сбалансированный JSON-объект `{...}` или массив `[...]` из текста.

    Учитывает строковые литералы и экранирование, чтобы скобки внутри строк не сбивали баланс.
    Возвращает подстроку-кандидат или None, если сбалансированной структуры нет.
    """
    start = _first_index_of_any(text, "{[")
    if start is None:
        return None
    opener = text[start]
    closer = "}" if opener == "{" else "]"
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _first_index_of_any(text: str, chars: str) -> int | None:
    indices = [text.find(c) for c in chars]
    found = [i for i in indices if i != -1]
    return min(found) if found else None


class StructuredOutputError(ValueError):
    """Parse/schema-фейл structured-output (ADR-020 §I.3): РЕ-СЕМПЛИРУЕМЫЙ сбой формата.

    `fail_class` ∈ {parse_error, schema_error}. Не терминал сам по себе — bounded retry
    (`run_structured_agent`) ретраит до исчерпания; на исчерпании вызывающий терминализует по
    §I.3 (Agent 1/2 → invalid_agent_output; Agent 3/4 → agent_output_invalid-виток).
    """

    def __init__(self, message: str, *, fail_class: str) -> None:
        super().__init__(message)
        self.fail_class = fail_class


# Колбэки, инъектируемые вызывающим (task-слой) — держат DB/гарды/cost-ledger вне structured.py.
# before_call: проверка budget/wall-clock §C(b)/(c) ПЕРЕД каждым LLM-вызовом (включая retry);
#   бросает доменное исключение (budget/wall-clock-гард) → loop его НЕ глотает, прерывает шаг.
# after_call: запись llm_usage + инкремент spend ПОСЛЕ каждого вызова (включая retry, §I.3).
# on_attempt_failure: диагностика parse/schema-фейла (§I.4) — лог + job_events.payload.
GuardHook = Callable[[], Awaitable[None]]
UsageHook = Callable[[AgentCall], Awaitable[None]]
DiagnosticsHook = Callable[..., Awaitable[None]]


class _ToolUseUnavailable(StructuredOutputError):
    """Внутренний маркер: модель не вернула tool_use-блок (отказ tool-use, граничный случай)."""


def _extract_structure(tool_call: AgentToolCall) -> Any:
    """Извлекает структуру: основной путь tool_input (§I.1), fallback — толерантный парсинг
    текста (§I.2). Бросает StructuredOutputError(parse_error), если оба не дали структуры."""
    if tool_call.tool_input is not None:
        return tool_call.tool_input
    # Tool-use не сработал — defence-in-depth на текстовый ответ.
    try:
        return extract_json(tool_call.text)
    except (ValueError, json.JSONDecodeError) as exc:
        raise StructuredOutputError(
            f"tool_use absent and text is not valid JSON: {exc}",
            fail_class=FAIL_CLASS_PARSE,
        ) from exc


@dataclass(frozen=True)
class StructuredResult[T]:
    """Итог structured-агента: доменно-валидированное значение + оплаченный вызов."""

    value: T
    call: AgentCall


async def run_structured_agent[T](
    settings: Settings,
    client: ClaudeAgentClient,
    *,
    agent: str,
    model: str,
    system_prompt: str,
    user_content: str,
    tool: AgentToolSpec,
    validate: Callable[[Any], T],
    before_call: GuardHook,
    after_call: UsageHook,
    on_attempt_failure: DiagnosticsHook,
    retry_nudge: str = (
        "\n\nReturn the result STRICTLY by calling the provided tool with valid arguments."
    ),
) -> StructuredResult[T]:
    """Форсированный tool-use + толерантный парсинг + bounded retry (ADR-020 §I, единый слой).

    Цикл (до AGENT_OUTPUT_MAX_RETRIES доп. попыток = до N+1 LLM-вызовов, §I.3):
      1. `before_call()` — budget/wall-clock-гард ПЕРЕД вызовом (считает каждый retry; бросок
         гарда прерывает шаг штатным FAILED(budget/wall_clock) — НЕ ловится здесь);
      2. форсированный tool-use вызов (§I.1);
      3. `after_call(call)` — запись llm_usage + spend (ВСЕГДА, вызов оплачен — даже при фейле);
      4. извлечь структуру: tool_input → fallback толерантный парсинг текста (§I.2);
      5. `validate(structure)` — доменная валидация (§I.1, поверх tool-use);
      6. успех → StructuredResult; parse/schema-фейл → диагностика (§I.4) + retry (re-sample);
         на исчерпании ретраев — бросить StructuredOutputError (вызывающий терминализует §I.3).

    Доменная валидация может бросить ЛЮБОЕ доменное исключение (например AgentOutputError для
    Agent 3/4) — оно трактуется как schema-фейл (re-семплируемый), ретраится, а на исчерпании
    ретраев пробрасывается вызывающему для встраивания в семантику agent_output_invalid.
    """
    max_retries = settings.agent_output_max_retries
    last_error: StructuredOutputError | None = None

    for attempt in range(max_retries + 1):
        await before_call()
        content = user_content if attempt == 0 else user_content + retry_nudge
        with timed_agent_call(agent, model):
            tool_call = await client.run_agent_tool(
                model=model,
                system_prompt=system_prompt,
                user_content=content,
                tool_name=tool.tool_name,
                input_schema=tool.input_schema,
            )
        # Вызов оплачен — учитываем usage ВСЕГДА (включая последующий parse/schema-фейл, §I.3).
        await after_call(tool_call.call)

        try:
            structure = _extract_structure(tool_call)
        except StructuredOutputError as exc:
            last_error = exc
            await _report(
                on_attempt_failure,
                agent,
                attempt,
                max_retries,
                exc,
                exc.fail_class,
                tool_call.text,
                settings,
            )
            continue

        try:
            value = validate(structure)
        except StructuredOutputError as exc:
            last_error = exc
            await _report(
                on_attempt_failure,
                agent,
                attempt,
                max_retries,
                exc,
                exc.fail_class,
                tool_call.text,
                settings,
            )
            continue
        except ValueError as exc:
            # Доменная валидация (AgentOutputError и пр.) — schema-фейл (§I.1 «поверх tool-use»).
            wrapped = StructuredOutputError(str(exc), fail_class=FAIL_CLASS_SCHEMA)
            wrapped.__cause__ = exc
            last_error = wrapped
            await _report(
                on_attempt_failure,
                agent,
                attempt,
                max_retries,
                exc,
                FAIL_CLASS_SCHEMA,
                tool_call.text,
                settings,
            )
            # Сохраняем исходное доменное исключение для пробрасывания на исчерпании ретраев.
            last_error.__dict__["domain_exc"] = exc
            continue

        return StructuredResult(value=value, call=tool_call.call)

    # Ретраи исчерпаны: пробрасываем исходное доменное исключение, если было (для §I.3 Agent 3/4
    # — встраивание в agent_output_invalid), иначе StructuredOutputError (Agent 1/2 → §I.3).
    assert last_error is not None
    domain_exc = last_error.__dict__.get("domain_exc")
    if domain_exc is not None:
        raise domain_exc
    raise last_error


async def _report(
    hook: DiagnosticsHook,
    agent: str,
    attempt: int,
    max_retries: int,
    error: Exception,
    fail_class: str,
    raw_text: str,
    settings: Settings,
) -> None:
    """Диагностика одной неудачной попытки (§I.4): scrubbed усечённый raw + текст ошибки."""
    truncated = scrub_text(raw_text[: settings.agent_raw_output_log_bytes])
    await hook(
        agent=agent,
        attempt=attempt + 1,
        max_attempts=max_retries + 1,
        error_text=str(error),
        fail_class=fail_class,
        raw_tail=truncated,
    )
