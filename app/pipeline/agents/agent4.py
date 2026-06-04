"""Agent 4 (Fixer): спека + упавшее дерево + failure_log → исправленное дерево.

Вход (docs §A):
  1. финальная спека (output Agent 2), неизменна между fix-итерациями;
  2. текущее дерево исходников — распаковка source.tgz последней ревизии ТЕКУЩЕЙ
     джобы (текстовые файлы как {path, content}; бинарные ассеты — только {path, size});
  3. failure_log (хвост из S3, FIXER_LOG_TAIL_BYTES) + извлечённые error-строки.

Выход (текстовый режим + строгий промт + extract_json, ADR-020 §I, revised; та же схема
agent_output, что Agent 3, ПЛЮС ветка unrecoverable полями JSON — §A): исправленное дерево
(ValidatedTree), ЛИБО сигнал {unrecoverable: true, reason, explanation} → UnrecoverableSignal.
Доменная валидация дерева — поверх извлечённой структуры (§I.2). Невалидный output после ретраев
= fix-неудача (AgentOutputError, учёт в retry_count/no-progress, ADR-005), не падение таски.
Bounded retry на parse/schema-фейл (AGENT_OUTPUT_MAX_RETRIES) — общий слой structured.py.
Модель — AGENT4_MODEL (tiering).
"""

from __future__ import annotations

import io
import json
import tarfile
from dataclasses import dataclass

from app.core.config import Settings
from app.core.logging import get_logger
from app.pipeline.agents.claude_client import AgentCall, ClaudeAgentClient
from app.pipeline.agents.structured import (
    DiagnosticsHook,
    GuardHook,
    UsageHook,
    run_structured_agent,
)
from app.pipeline.prompts import load_prompt
from app.schemas.agent_output import (
    RESERVED_SERVICE_FILENAMES,
    ValidatedTree,
    validate_agent_output,
)

logger = get_logger(__name__)

_SYSTEM_PROMPT = load_prompt("agent4_fixer")
# Sprint 5 (ADR-014): Agent 4 как editor для post-delivery правок — отдельный system-промт
# (вход = спека + current good-ревизия + instruction → новое дерево). Та же выходная схема.
_EDITOR_SYSTEM_PROMPT = load_prompt("agent4_editor")
# Расширения, трактуемые как бинарные ассеты при подаче дерева в Agent 4: их содержимое
# НЕ передаём (только path+size), чтобы не жечь токены (docs §A вход п.2).
_BINARY_EXTS = frozenset(
    {"png", "jpg", "jpeg", "gif", "webp", "ico", "woff", "woff2", "ttf", "otf"}
)


@dataclass(frozen=True)
class UnrecoverableSignal:
    """Явный сигнал Agent 4 «неисправимо» (docs §A): pipeline → FIXING→FAILED(fixer_gave_up)."""

    reason: str
    explanation: str


@dataclass(frozen=True)
class Agent4Result:
    """Результат Fixer: ровно одно из tree / unrecoverable заполнено."""

    call: AgentCall
    tree: ValidatedTree | None
    unrecoverable: UnrecoverableSignal | None


@dataclass(frozen=True)
class _Agent4Output:
    """Внутренний доменно-валидированный выход (без call — добавляется structured-слоем)."""

    tree: ValidatedTree | None
    unrecoverable: UnrecoverableSignal | None


def _ext(path: str) -> str:
    base = path.rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[1].lower() if "." in base else ""


def _read_tree_for_prompt(source_tgz: bytes) -> str:
    """Распаковывает source.tgz в память и форматирует дерево для подачи в Agent 4.

    Текстовые файлы — целиком {path, content}; бинарные ассеты — только {path, size}
    (без содержимого, чтобы не жечь токены, docs §A). Служебный манифест .build.json
    из дерева исключается (он не часть исходников проекта).
    """
    text_files: list[dict[str, str]] = []
    binary_assets: list[dict[str, object]] = []
    with tarfile.open(fileobj=io.BytesIO(source_tgz), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isreg():
                continue
            name = member.name
            if name.rsplit("/", 1)[-1].lower() in RESERVED_SERVICE_FILENAMES:
                continue
            extracted = tar.extractfile(member)
            if extracted is None:
                continue
            data = extracted.read()
            if _ext(name) in _BINARY_EXTS:
                binary_assets.append({"path": name, "size": len(data)})
                continue
            try:
                content = data.decode("utf-8")
            except UnicodeDecodeError:
                # Нетекстовый файл с текстовым расширением — подаём как ассет (path+size).
                binary_assets.append({"path": name, "size": len(data)})
                continue
            text_files.append({"path": name, "content": content})

    payload = {
        "text_files": sorted(text_files, key=lambda f: f["path"]),
        "binary_assets": sorted(binary_assets, key=lambda a: str(a["path"])),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _tail(text: str, max_bytes: int) -> str:
    """Возвращает хвост текста ≤ max_bytes (последние N байт — там диагностическое ядро)."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    # Режем по байтам с конца, восстанавливаем валидный utf-8 (отбрасываем обрезанный лидер).
    return encoded[-max_bytes:].decode("utf-8", errors="ignore")


def _build_user_content(
    settings: Settings,
    *,
    spec_markdown: str,
    source_tree_json: str,
    failure_class: str,
    failure_log: str,
) -> str:
    log_tail = _tail(failure_log, settings.fixer_log_tail_bytes)
    return (
        "## Specification (fixed)\n\n"
        f"{spec_markdown}\n\n"
        "## Current source tree (the revision that just failed)\n\n"
        f"{source_tree_json}\n\n"
        "## Failure\n\n"
        f"failure_class: {failure_class}\n\n"
        "Failure log (tail):\n\n"
        f"{log_tail}\n\n"
        "Submit the corrected file tree, or set unrecoverable=true with reason/explanation."
    )


def _parse_unrecoverable(raw: dict[str, object]) -> UnrecoverableSignal | None:
    """Если output — сигнал {unrecoverable: true, ...}, возвращает его, иначе None."""
    if raw.get("unrecoverable") is not True:
        return None
    reason = raw.get("reason")
    explanation = raw.get("explanation")
    reason_str = reason if isinstance(reason, str) and reason else "fixer_gave_up"
    explanation_str = (
        explanation
        if isinstance(explanation, str) and explanation
        else "The site could not be fixed automatically."
    )
    return UnrecoverableSignal(reason=reason_str, explanation=explanation_str)


def _validate_agent4_output(raw: object, settings: Settings) -> _Agent4Output:
    """Доменная валидация извлечённой структуры Agent 4 поверх extract_json (ADR-020 §I.2).

    Сигнал unrecoverable — легальный выход (дерево None), не ошибка. Иначе валидируем дерево
    тем же контрактом, что Agent 3 (AgentOutputError → schema-фейл, ретраится; на исчерпании
    = fix-неудача класса agent_output_invalid, §I.3 / §A).
    """
    if not isinstance(raw, dict):
        from app.schemas.agent_output import AgentOutputError

        raise AgentOutputError(
            "agent4 output must be a JSON object", signature="agent_output_invalid"
        )

    signal = _parse_unrecoverable(raw)
    if signal is not None:
        logger.info("agent4_unrecoverable", extra={"reason": signal.reason})
        return _Agent4Output(tree=None, unrecoverable=signal)

    tree = validate_agent_output(raw, settings)
    return _Agent4Output(tree=tree, unrecoverable=None)


async def run_agent4(
    settings: Settings,
    *,
    spec_markdown: str,
    source_tgz: bytes,
    failure_class: str,
    failure_log: str,
    before_call: GuardHook,
    after_call: UsageHook,
    on_attempt_failure: DiagnosticsHook,
) -> Agent4Result:
    """Один шаг Fixer (текстовый режим + строгий промт + extract_json + bounded retry +
    доменная валидация, ADR-020 §I).

    Хуки инъектируются task-слоем (budget/wall-clock-гард, llm_usage, диагностика §I.4). Сигнал
    unrecoverable — легальный выход (дерево None). На исчерпании ретраев невалидный output →
    AgentOutputError (fix-неудача, учёт в retry_count/no-progress, §A / §I.3).
    """
    source_tree_json = _read_tree_for_prompt(source_tgz)
    user_content = _build_user_content(
        settings,
        spec_markdown=spec_markdown,
        source_tree_json=source_tree_json,
        failure_class=failure_class,
        failure_log=failure_log,
    )
    return await _run_agent4(
        settings,
        system_prompt=_SYSTEM_PROMPT,
        user_content=user_content,
        before_call=before_call,
        after_call=after_call,
        on_attempt_failure=on_attempt_failure,
    )


def _build_editor_content(*, spec_markdown: str, source_tree_json: str, instruction: str) -> str:
    return (
        "## Specification (baseline)\n\n"
        f"{spec_markdown}\n\n"
        "## Current source tree (the live good revision)\n\n"
        f"{source_tree_json}\n\n"
        "## Edit instruction\n\n"
        f"{instruction}\n\n"
        "Submit the new complete file tree, or set unrecoverable=true with reason/explanation."
    )


async def run_agent4_editor(
    settings: Settings,
    *,
    spec_markdown: str,
    source_tgz: bytes,
    instruction: str,
    before_call: GuardHook,
    after_call: UsageHook,
    on_attempt_failure: DiagnosticsHook,
) -> Agent4Result:
    """Один шаг Agent 4 как editor (Sprint 5, ADR-014): спека + current good-дерево +
    instruction → новое дерево (та же выходная схема/валидация/structured-механизм, что fixer).

    На исчерпании ретраев невалидный output → AgentOutputError (как fix-неудача — учёт в гардах
    edit-цикла). unrecoverable → Agent4Result.unrecoverable (дерево None).
    """
    source_tree_json = _read_tree_for_prompt(source_tgz)
    user_content = _build_editor_content(
        spec_markdown=spec_markdown,
        source_tree_json=source_tree_json,
        instruction=instruction,
    )
    return await _run_agent4(
        settings,
        system_prompt=_EDITOR_SYSTEM_PROMPT,
        user_content=user_content,
        before_call=before_call,
        after_call=after_call,
        on_attempt_failure=on_attempt_failure,
    )


async def _run_agent4(
    settings: Settings,
    *,
    system_prompt: str,
    user_content: str,
    before_call: GuardHook,
    after_call: UsageHook,
    on_attempt_failure: DiagnosticsHook,
) -> Agent4Result:
    """Общий structured-вызов Agent 4 (fixer/editor): текстовый режим + extract_json + retry +
    доменная валидация."""
    client = ClaudeAgentClient(settings)
    result = await run_structured_agent(
        settings,
        client,
        agent="agent4",
        model=settings.agent4_model,
        system_prompt=system_prompt,
        user_content=user_content,
        validate=lambda raw: _validate_agent4_output(raw, settings),
        before_call=before_call,
        after_call=after_call,
        on_attempt_failure=on_attempt_failure,
    )
    return Agent4Result(
        call=result.call,
        tree=result.value.tree,
        unrecoverable=result.value.unrecoverable,
    )
