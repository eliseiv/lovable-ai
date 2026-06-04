"""Надёжный structured-output всех 4 агентов (ADR-020, revised; docs pipeline §I).

ЕДИНЫЙ слой для Agent 1/2/3/4 — НЕ дублируется в каждом агенте. Три части:

(1) Текстовый режим + строгий системный промт — основной механизм (§I.1, revised). Вызов
    `ClaudeAgentClient.run_agent` (`thinking=adaptive` + `output_config={effort}`, БЕЗ
    `tools`/`tool_choice`); формат выхода форсируется строгим суффиксом системного промта
    (`STRICT_JSON_SUFFIX` ниже — нормативный общий шаблон). Форсированный tool-use ОТОЗВАН:
    несовместим с thinking → HTTP 400 (ADR-020 §Ограничение API, 100% отказ).
(2) Толерантный парсинг (§I.2) — `extract_json` на текстовый ответ модели (`block.text`):
    снятие ```json/``` -фенсов + извлечение первого сбалансированного JSON перед json.loads.
    Это ОСНОВНОЙ путь получения структуры (устраняет markdown-фенсы как класс — прод-баг §I:
    ~40% ответов модели приходили в ```json…``` → строгий json.loads → ValueError → FAILED).
(3) Bounded retry на parse/schema-фейл (§I.3) — `run_structured_agent` ретраит НОВЫЙ LLM-вызов
    того же агента до AGENT_OUTPUT_MAX_RETRIES раз ВНУТРИ шага агента (НЕ Celery-retry, НЕ
    FIXING). Перед КАЖДЫМ вызовом — guard-хук (budget/wall-clock §C(b)/(c) считают retry-вызовы);
    после КАЖДОГО вызова — usage-хук (запись llm_usage + spend). При каждом фейле — diag-хук
    (имя агента/attempt/класс/текст ошибки/scrubbed усечённый raw в job_events.payload, §I.4).

Доменная валидация (особенно дерево Agent 3) — поверх извлечённой структуры, НЕ заменяется
парсером (§I.2): вызывающий передаёт `validate`-колбэк, применяющий прежний валидатор.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.core.config import Settings
from app.observability.sentry import scrub_text
from app.observability.timing import timed_agent_call
from app.pipeline.agents.claude_client import AgentCall, ClaudeAgentClient

# Классы фейла structured-output (§I.4): parse — структура не извлеклась; schema — извлеклась,
# но не прошла доменную валидацию.
FAIL_CLASS_PARSE = "parse_error"
FAIL_CLASS_SCHEMA = "schema_error"

# Строгая нормативная инструкция формата (ADR-020 §I.1, revised; docs pipeline §I.1).
# ОБЯЗАТЕЛЬНА в системном промте КАЖДОГО из 4 агентов — единый общий шаблон, добавляется
# через append_strict_json (не дублируется в каждом промт-файле). Текстовый режим без
# форсирующего tool_choice → формат держится этим промтом + extract_json (§I.2).
STRICT_JSON_SUFFIX = (
    "\n\n"
    "Return STRICTLY raw JSON of the required structure. NO markdown fences "
    "(``` or ```json), NO explanations/prefixes/prose before or after the JSON. "
    "The first character of your response must be { or [, and the last must be } or ]."
)


def append_strict_json(system_prompt: str) -> str:
    """Добавляет нормативную строгую JSON-инструкцию (§I.1) к системному промту агента.

    Единый источник формулировки — STRICT_JSON_SUFFIX. Применяется ко ВСЕМ 4 агентам
    единообразно через общий слой (не дублируется в промт-файлах).
    """
    return system_prompt + STRICT_JSON_SUFFIX


def extract_json(text: str) -> Any:
    """Толерантный парсинг текстового ответа модели (ADR-020 §I.2, основной путь).

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


def _extract_structure(call: AgentCall) -> Any:
    """Извлекает структуру из текстового ответа модели толерантным парсером (§I.2).

    Бросает StructuredOutputError(parse_error), если валидный JSON извлечь не удалось.
    """
    try:
        return extract_json(call.text)
    except (ValueError, json.JSONDecodeError) as exc:
        raise StructuredOutputError(
            f"model text is not valid JSON: {exc}",
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
    validate: Callable[[Any], T],
    before_call: GuardHook,
    after_call: UsageHook,
    on_attempt_failure: DiagnosticsHook,
    retry_nudge: str = (
        "\n\nReturn the result STRICTLY as raw JSON — no markdown fences, no prose."
    ),
) -> StructuredResult[T]:
    """Текстовый режим + толерантный парсинг + bounded retry (ADR-020 §I, revised; единый слой).

    Системный промт обязан нести строгую JSON-инструкцию — она добавляется здесь через
    append_strict_json (§I.1, единый шаблон STRICT_JSON_SUFFIX). Вызов — ТЕКСТОВЫЙ
    (`run_agent`, без `tools`/`tool_choice`): несовместимость форсированного tool_choice с
    thinking (HTTP 400) исключена конструктивно. Цикл (до AGENT_OUTPUT_MAX_RETRIES доп. попыток
    = до N+1 LLM-вызовов, §I.3):
      1. `before_call()` — budget/wall-clock-гард ПЕРЕД вызовом (считает каждый retry; бросок
         гарда прерывает шаг штатным FAILED(budget/wall_clock) — НЕ ловится здесь);
      2. текстовый вызов агента (§I.1);
      3. `after_call(call)` — запись llm_usage + spend (ВСЕГДА, вызов оплачен — даже при фейле);
      4. извлечь структуру из `call.text` толерантным парсером `extract_json` (§I.2);
      5. `validate(structure)` — доменная валидация (§I.2, поверх извлечённой структуры);
      6. успех → StructuredResult; parse/schema-фейл → диагностика (§I.4) + retry (re-sample);
         на исчерпании ретраев — бросить StructuredOutputError (вызывающий терминализует §I.3).

    Доменная валидация может бросить ЛЮБОЕ доменное исключение (например AgentOutputError для
    Agent 3/4) — оно трактуется как schema-фейл (re-семплируемый), ретраится, а на исчерпании
    ретраев пробрасывается вызывающему для встраивания в семантику agent_output_invalid.
    """
    max_retries = settings.agent_output_max_retries
    strict_system_prompt = append_strict_json(system_prompt)
    last_error: StructuredOutputError | None = None

    for attempt in range(max_retries + 1):
        await before_call()
        content = user_content if attempt == 0 else user_content + retry_nudge
        with timed_agent_call(agent, model):
            call = await client.run_agent(
                agent=agent,
                model=model,
                system_prompt=strict_system_prompt,
                user_content=content,
            )
        # Вызов оплачен — учитываем usage ВСЕГДА (включая последующий parse/schema-фейл, §I.3).
        await after_call(call)

        try:
            structure = _extract_structure(call)
        except StructuredOutputError as exc:
            last_error = exc
            await _report(
                on_attempt_failure,
                agent,
                attempt,
                max_retries,
                exc,
                exc.fail_class,
                call.text,
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
                call.text,
                settings,
            )
            continue
        except ValueError as exc:
            # Доменная валидация (AgentOutputError и пр.) — schema-фейл (§I.2 «поверх структуры»).
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
                call.text,
                settings,
            )
            # Сохраняем исходное доменное исключение для пробрасывания на исчерпании ретраев.
            last_error.__dict__["domain_exc"] = exc
            continue

        return StructuredResult(value=value, call=call)

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
