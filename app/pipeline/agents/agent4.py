"""Agent 4 (Fixer): спека + упавшее дерево + failure_log → исправленное дерево.

Вход (docs §A):
  1. финальная спека (output Agent 2), неизменна между fix-итерациями;
  2. текущее дерево исходников — распаковка source.tgz последней ревизии ТЕКУЩЕЙ
     джобы (текстовые файлы как {path, content}; бинарные ассеты — только {path, size});
  3. failure_log (хвост из S3, FIXER_LOG_TAIL_BYTES) + извлечённые error-строки.

Выход (строго валидируется тем же контрактом, что Agent 3 — app.schemas.agent_output):
  - исправленное дерево (ValidatedTree), ЛИБО
  - сигнал {unrecoverable: true, reason, explanation} → UnrecoverableSignal.

Невалидный патч = fix-неудача (учёт в retry_count/no-progress, ADR-005), не падение
таски: бросается AgentOutputError (как у Agent 3). Модель — AGENT4_MODEL (tiering,
default Opus); prompt caching стабильного system-промта; запись llm_usage у вызывающего.
"""

from __future__ import annotations

import io
import json
import tarfile
from dataclasses import dataclass

from app.core.config import Settings
from app.core.logging import get_logger
from app.observability.timing import timed_agent_call
from app.pipeline.agents.claude_client import AgentCall, ClaudeAgentClient
from app.pipeline.prompts import load_prompt
from app.schemas.agent_output import (
    RESERVED_SERVICE_FILENAMES,
    AgentOutputError,
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
        "Return the corrected file tree JSON, or the unrecoverable signal JSON."
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


def _parse_agent4_output(call: AgentCall, settings: Settings) -> Agent4Result:
    """Парсинг+валидация output Agent 4 (общая для fixer и editor режимов).

    Невалидный output → AgentOutputError (= неудача, учёт в retry_count/no-progress).
    Сигнал unrecoverable — легальный выход (дерево None).
    """
    try:
        raw = json.loads(call.text)
    except json.JSONDecodeError as exc:
        raise AgentOutputError(
            "agent4 output is not valid JSON",
            signature="agent_output_invalid",
            call=call,
        ) from exc
    if not isinstance(raw, dict):
        raise AgentOutputError(
            "agent4 output must be a JSON object",
            signature="agent_output_invalid",
            call=call,
        )

    signal = _parse_unrecoverable(raw)
    if signal is not None:
        logger.info("agent4_unrecoverable", extra={"reason": signal.reason})
        return Agent4Result(call=call, tree=None, unrecoverable=signal)

    try:
        tree = validate_agent_output(raw, settings)
    except AgentOutputError as exc:
        # Прокидываем call наверх (вызов оплачен → запись llm_usage); невалидный патч
        # трактуется как fix-неудача (учёт в retry_count/no-progress, docs §A).
        exc.call = call
        raise
    return Agent4Result(call=call, tree=tree, unrecoverable=None)


async def run_agent4(
    settings: Settings,
    *,
    spec_markdown: str,
    source_tgz: bytes,
    failure_class: str,
    failure_log: str,
) -> Agent4Result:
    """Один вызов Fixer. Бросает AgentOutputError при невалидном патче (= fix-неудача).

    Сигнал unrecoverable — легальный выход (не ошибка валидации): возвращается в
    Agent4Result.unrecoverable, дерево при этом None.
    """
    source_tree_json = _read_tree_for_prompt(source_tgz)
    user_content = _build_user_content(
        settings,
        spec_markdown=spec_markdown,
        source_tree_json=source_tree_json,
        failure_class=failure_class,
        failure_log=failure_log,
    )

    client = ClaudeAgentClient(settings)
    with timed_agent_call("agent4", settings.agent4_model):
        call = await client.run_agent(
            model=settings.agent4_model,
            system_prompt=_SYSTEM_PROMPT,
            user_content=user_content,
        )
    return _parse_agent4_output(call, settings)


def _build_editor_content(*, spec_markdown: str, source_tree_json: str, instruction: str) -> str:
    return (
        "## Specification (baseline)\n\n"
        f"{spec_markdown}\n\n"
        "## Current source tree (the live good revision)\n\n"
        f"{source_tree_json}\n\n"
        "## Edit instruction\n\n"
        f"{instruction}\n\n"
        "Return the new complete file tree JSON, or the unrecoverable signal JSON."
    )


async def run_agent4_editor(
    settings: Settings,
    *,
    spec_markdown: str,
    source_tgz: bytes,
    instruction: str,
) -> Agent4Result:
    """Один вызов Agent 4 как editor (Sprint 5, ADR-014): спека + current good-дерево +
    instruction → новое дерево (та же выходная схема/валидация, что fixer).

    Невалидный output → AgentOutputError (как fix-неудача — учёт в гардах edit-цикла).
    unrecoverable → Agent4Result.unrecoverable (дерево None).
    """
    source_tree_json = _read_tree_for_prompt(source_tgz)
    user_content = _build_editor_content(
        spec_markdown=spec_markdown,
        source_tree_json=source_tree_json,
        instruction=instruction,
    )
    client = ClaudeAgentClient(settings)
    with timed_agent_call("agent4", settings.agent4_model):
        call = await client.run_agent(
            model=settings.agent4_model,
            system_prompt=_EDITOR_SYSTEM_PROMPT,
            user_content=user_content,
        )
    return _parse_agent4_output(call, settings)
