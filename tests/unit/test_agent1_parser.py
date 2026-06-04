"""Unit: доменная валидация output Agent 1 в вопросы (docs/modules/pipeline/03-architecture.md).

ADR-020 §I.1: структура приходит из форсированного tool-use (tool_input — уже распарсенный
SDK-объект), поэтому доменный валидатор `_validate_questions` принимает dict (не JSON-строку);
снятие фенсов/парсинг JSON — ответственность общего слоя structured.py (тестируется отдельно).
Нарушение контракта questions → StructuredOutputError(schema_error) — re-семплируемый фейл.
"""

from __future__ import annotations

import pytest

from app.pipeline.agents.agent1 import _validate_questions
from app.pipeline.agents.structured import StructuredOutputError


def test_validates_valid_questions():
    data = {
        "questions": [
            {"position": 1, "text": "What is the goal?", "kind": "free_text"},
            {"position": 2, "text": "Pick palette", "kind": "choice", "options": ["a", "b"]},
        ]
    }
    qs = _validate_questions(data)
    assert len(qs) == 2
    assert qs[0].text == "What is the goal?"
    assert qs[1].kind == "choice"
    assert qs[1].options == ["a", "b"]


def test_rejects_non_dict():
    with pytest.raises(StructuredOutputError, match="non-empty 'questions'"):
        _validate_questions("not a dict")


def test_rejects_missing_questions():
    with pytest.raises(StructuredOutputError, match="non-empty 'questions'"):
        _validate_questions({"questions": []})


def test_rejects_empty_question_text():
    with pytest.raises(StructuredOutputError, match="non-empty string"):
        _validate_questions({"questions": [{"text": "   "}]})


def test_choice_without_options_rejected():
    with pytest.raises(StructuredOutputError, match="non-empty options"):
        _validate_questions({"questions": [{"text": "pick", "kind": "choice", "options": []}]})


def test_position_defaults_to_index_when_missing():
    qs = _validate_questions({"questions": [{"text": "a"}, {"text": "b"}]})
    assert qs[0].position == 1
    assert qs[1].position == 2


def test_unknown_kind_coerced_to_none():
    qs = _validate_questions({"questions": [{"text": "a", "kind": "weird"}]})
    assert qs[0].kind is None


def test_schema_error_fail_class():
    """Нарушение доменного контракта классифицируется как schema_error (re-семплируемый)."""
    with pytest.raises(StructuredOutputError) as ei:
        _validate_questions({"questions": []})
    assert ei.value.fail_class == "schema_error"
