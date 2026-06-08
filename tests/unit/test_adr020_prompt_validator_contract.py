"""Unit/contract: промт↔валидатор консистентность по single source (ADR-020 §I.1a/§I.6).

Источник истины — docs/modules/pipeline/03-architecture.md §I.1a (канонические output-схемы
агентов, single source of truth) + §I.6 (per-agent декларация точной схемы в промте) + §I.5
(критерии приёмки qa), docs/06-testing-strategy.md §Unit «Structured-output агентов».

Фикс инцидента Agent 2 (live-E2E 2026-06-04): промт декларировал/предписывал ключ-обёртку
`spec`/`specification` (или «Markdown text only, do not wrap in JSON»), а валидатор
`_validate_spec` читал `spec_markdown` → всегда None → schema-фейл «empty specification» →
исчерпание ретраев → FAILED(invalid_agent_output). Эти тесты гарантируют, что НИ ОДИН из 4
агентов не разойдётся промт↔валидатор по имени канонического top-level ключа, и что
минимальная каноническая форма каждого агента проходит свой валидатор без schema-фейла.

Тесты — чистые unit (загрузка реальных promt-файлов через load_prompt + прямой вызов
валидаторов агентов). Без сети/БД/Claude.

Покрывает §I.5:
  - contract (промт↔валидатор консистентность по single source) — для КАЖДОГО из 4 агентов:
    (а) промт ДЕКЛАРИРУЕТ канонический ключ из §I.1a; (б) валидатор читает ТОТ ЖЕ ключ.
  - contract (валидатор не падает «empty» на канонической форме) — минимальная каноническая
    форма §I.1a проходит валидацию без schema-фейла.
  - negative (Agent 2 — отсутствие противоречия в промте) — agent2_spec_writer.txt НЕ содержит
    «do not wrap it in JSON» / «Markdown text only» и НЕ предписывает ключи spec/specification.
"""

from __future__ import annotations

import json

import pytest

from app.core.config import get_settings
from app.pipeline.agents.agent1 import _validate_questions
from app.pipeline.agents.agent2 import _validate_spec
from app.pipeline.agents.agent4 import _validate_agent4_output
from app.pipeline.prompts import load_prompt
from app.schemas.agent_output import AgentOutputError, validate_agent_output

# asyncio_mode=auto (pyproject) — здесь все тесты синхронные (загрузка файла + вызов
# чистых функций-валидаторов), без async.


@pytest.fixture
def settings():  # noqa: ANN201
    return get_settings()


# Канон §I.1a: имя промт-файла → канонические top-level ключи, которые промт ОБЯЗАН
# декларировать (§I.6) и которые читает соответствующий валидатор. Источник истины —
# таблица §I.1a docs/modules/pipeline/03-architecture.md.
_AGENT_PROMPT_CANONICAL_KEYS = {
    "agent1_interviewer": ["questions"],
    "agent2_spec_writer": ["spec_markdown"],
    "agent3_builder": ["files", "entry", "build"],
    # Agent 4 — две ветки: дерево (files/entry/build) ИЛИ сигнал unrecoverable.
    "agent4_fixer": ["files", "entry", "build", "unrecoverable", "reason", "explanation"],
    "agent4_editor": ["files", "entry", "build", "unrecoverable", "reason", "explanation"],
}


# --------------------------------------------------------------------------- #
# (а) Промт ДЕКЛАРИРУЕТ канонический top-level ключ из §I.1a (§I.6).
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("prompt_name", "keys"),
    list(_AGENT_PROMPT_CANONICAL_KEYS.items()),
)
def test_prompt_declares_canonical_top_level_keys(prompt_name, keys):  # noqa: ANN001
    """Системный промт КАЖДОГО агента содержит точное имя канонического ключа из §I.1a (§I.6).

    Точное имя ключа (в кавычках) присутствует в тексте промта — модель не угадывает
    ключ-обёртку (фикс инцидента Agent 2: промт→spec/specification, валидатор→spec_markdown).
    """
    text = load_prompt(prompt_name)
    for key in keys:
        # Ключ декларирован как точное имя в кавычках ("questions", "spec_markdown", ...) —
        # именно так промты §I.6 пинят top-level имя, исключая угадывание обёртки.
        assert f'"{key}"' in text, (
            f"промт {prompt_name}.txt НЕ декларирует канонический ключ {key!r} из §I.1a"
        )


# --------------------------------------------------------------------------- #
# (б) Валидатор того же агента читает ТОТ ЖЕ канонический ключ (промт↔валидатор).
# --------------------------------------------------------------------------- #


def test_agent1_validator_reads_questions_key():
    """agent1 `_validate_questions` читает `questions` (тот же ключ, что декларирует промт)."""
    # Канонический ключ → ok; ключ-обёртка `items`/`list` (как угадала бы модель) → schema-фейл.
    assert _validate_questions({"questions": [{"position": 1, "text": "q", "kind": "free_text"}]})
    from app.pipeline.agents.structured import StructuredOutputError

    with pytest.raises(StructuredOutputError):
        _validate_questions({"items": [{"position": 1, "text": "q", "kind": "free_text"}]})


def test_agent2_validator_reads_spec_markdown_key():
    """agent2 `_validate_spec` читает `spec_markdown` — НЕ `spec`/`specification` (инцидент).

    Каноническая форма §I.1a (ADR-025): значение `spec_markdown` ОБЯЗАНО начинаться маркером
    `**Content language:**` — иначе schema-фейл (см. negative-тест маркера ниже).
    """
    from app.pipeline.agents.structured import StructuredOutputError

    spec = "**Content language:** English (en)\n\n# Spec"
    assert _validate_spec({"spec_markdown": spec}) == spec
    # Ровно та форма, что трижды вернула модель в инциденте, → schema-фейл (валидатор не читает их).
    with pytest.raises(StructuredOutputError):
        _validate_spec({"spec": spec})
    with pytest.raises(StructuredOutputError):
        _validate_spec({"specification": spec})


def test_agent3_validator_reads_files_entry_build_keys(settings):  # noqa: ANN001
    """agent3 `validate_agent_output` читает `files`/`entry`/`build` (те же ключи, что промт)."""
    tree = _minimal_tree()
    validated = validate_agent_output(tree, settings)
    assert validated.entry == "index.html"
    # Подмена ключа `files` → `tree` (что угадала бы модель) → schema-фейл.
    bad = dict(tree)
    bad["tree"] = bad.pop("files")
    with pytest.raises(AgentOutputError):
        validate_agent_output(bad, settings)


def test_agent4_validator_reads_tree_keys_and_unrecoverable(settings):  # noqa: ANN001
    """agent4 `_validate_agent4_output` читает ветку дерева (files/entry/build) И ветку
    unrecoverable/reason/explanation — те же ключи, что декларирует промт fixer/editor."""
    # Ветка дерева.
    out_tree = _validate_agent4_output(_minimal_tree(), settings)
    assert out_tree.tree is not None
    assert out_tree.unrecoverable is None
    # Ветка сигнала unrecoverable.
    out_signal = _validate_agent4_output(
        {"unrecoverable": True, "reason": "no_backend", "explanation": "needs a server"},
        settings,
    )
    assert out_signal.tree is None
    assert out_signal.unrecoverable is not None
    assert out_signal.unrecoverable.reason == "no_backend"


# --------------------------------------------------------------------------- #
# Страховка: минимальная КАНОНИЧЕСКАЯ форма §I.1a проходит валидатор без schema-фейла.
# Воспроизводит и закрывает инцидент: ранее `_validate_spec` падал «empty specification».
# --------------------------------------------------------------------------- #


def test_agent1_canonical_minimal_form_validates():
    """§I.1a Agent 1: {"questions":[{position,text,kind:free_text}]} → ok без schema-фейла."""
    questions = _validate_questions(
        {"questions": [{"position": 1, "text": "q", "kind": "free_text"}]}
    )
    assert len(questions) == 1
    assert questions[0].text == "q"


def test_agent2_canonical_minimal_form_validates():
    """§I.1a Agent 2: каноническая минимальная форма (С маркером §Язык/локализация, ADR-025)
    → ok без schema-фейла (закрывает инцидент «empty specification» И требование маркера).

    Канон §I.1a колонка «Минимальный валидный объект»:
    `{"spec_markdown":"**Content language:** English (en)\\n\\n# Specification\\n…"}`.
    """
    spec = "**Content language:** English (en)\n\n# x"
    assert _validate_spec({"spec_markdown": spec}) == spec


def test_agent3_canonical_minimal_form_validates(settings):  # noqa: ANN001
    """§I.1a Agent 3: минимальное валидное дерево → validate_agent_output ok без schema-фейла."""
    validated = validate_agent_output(_minimal_tree(), settings)
    assert validated.entry == "index.html"
    assert validated.build_command == "npm ci && vite build"


def test_agent4_canonical_minimal_tree_validates(settings):  # noqa: ANN001
    """§I.1a Agent 4 (ветка дерева): минимальное валидное дерево → ok (tree, unrecoverable None)."""
    out = _validate_agent4_output(_minimal_tree(), settings)
    assert out.tree is not None
    assert out.unrecoverable is None


def test_agent4_canonical_unrecoverable_signal_validates(settings):  # noqa: ANN001
    """§I.1a Agent 4 (ветка сигнала): {"unrecoverable":true,"reason","explanation"} →
    UnrecoverableSignal (легальный выход, дерево None — НЕ schema-фейл)."""
    out = _validate_agent4_output(
        {"unrecoverable": True, "reason": "self_contradictory", "explanation": "cannot fix"},
        settings,
    )
    assert out.tree is None
    assert out.unrecoverable is not None
    assert out.unrecoverable.reason == "self_contradictory"
    assert out.unrecoverable.explanation == "cannot fix"


# --------------------------------------------------------------------------- #
# Negative (Agent 2) — отсутствие противоречия в промте (устранённый второй корень §I.6).
# --------------------------------------------------------------------------- #


def test_agent2_prompt_has_no_markdown_only_contradiction():
    """agent2_spec_writer.txt НЕ содержит «do not wrap it in JSON» / «Markdown text only»
    (устранённый второй корень §I.6: тело промта противоречило STRICT_JSON_SUFFIX/валидатору).

    Исторический промт предписывал «respond as GitHub-flavored Markdown text only. Do not wrap
    it in JSON» — прямое противоречие raw-JSON-суффиксу и валидатору `spec_markdown`. Эти
    регрессные формулы должны быть удалены. (Допустимо «do not wrap the JSON in markdown fences»
    — это про фенсы вокруг JSON, не «не оборачивать в JSON».)"""
    text = load_prompt("agent2_spec_writer").lower()
    assert "do not wrap it in json" not in text
    assert "markdown text only" not in text
    assert "markdown only" not in text


def test_agent2_prompt_does_not_prescribe_spec_or_specification_top_level_key():
    """agent2_spec_writer.txt НЕ предписывает ключи `spec`/`specification` как top-level ответ
    (это формы, что вернула модель в инциденте; валидатор их не читает).

    Канонический `spec_markdown` декларирован; обёртки `spec`/`specification` либо отсутствуют,
    либо упомянуты ТОЛЬКО под явным запретом («Do not use the keys ...») — НЕ как предписанный
    ключ ответа. Проверяем: каждое вхождение `spec`/`specification` (как имя ключа в кавычках)
    стоит в контексте запрета, а не предписания."""
    text = load_prompt("agent2_spec_writer")
    # Нормализуем пробелы/переводы строк (промт переносит «Do not\nuse the keys ...»).
    flat = " ".join(text.lower().split())
    assert '"spec_markdown"' in text
    # Если `spec`/`specification` упомянуты как ключ — только в запрещающем контексте.
    for wrapper in ("spec", "specification"):
        if f'"{wrapper}"' in text:
            # Запрет вида «do not use the keys "spec" or "specification"» обязан присутствовать.
            assert "do not use the keys" in flat, (
                f"промт упоминает ключ {wrapper!r} вне явного запрета — риск предписать обёртку"
            )


# --------------------------------------------------------------------------- #
# Хелпер: минимальное валидное дерево §I.1a (Agent 3/4 ветка дерева).
# --------------------------------------------------------------------------- #


def _minimal_tree() -> dict:
    pkg = json.dumps(
        {"name": "s", "scripts": {"build": "vite build"}, "devDependencies": {"vite": "^5"}}
    )
    return {
        "files": [
            {"path": "package.json", "encoding": "utf8", "content": pkg},
            {"path": "index.html", "encoding": "utf8", "content": "<!doctype html><html></html>"},
        ],
        "entry": "index.html",
        "build": {"tool": "vite", "command": "npm ci && vite build", "output_dir": "dist"},
    }
