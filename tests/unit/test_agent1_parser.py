"""Unit: парсинг output Agent 1 в вопросы (docs/modules/pipeline/03-architecture.md)."""

from __future__ import annotations

import json

import pytest

from app.pipeline.agents.agent1 import _parse_questions


def test_parses_valid_questions():
    payload = json.dumps(
        {
            "questions": [
                {"position": 1, "text": "What is the goal?", "kind": "free_text"},
                {"position": 2, "text": "Pick palette", "kind": "choice", "options": ["a", "b"]},
            ]
        }
    )
    qs = _parse_questions(payload)
    assert len(qs) == 2
    assert qs[0].text == "What is the goal?"
    assert qs[1].kind == "choice"
    assert qs[1].options == ["a", "b"]


def test_rejects_invalid_json():
    with pytest.raises(ValueError, match="not valid JSON"):
        _parse_questions("{not json")


def test_rejects_missing_questions():
    with pytest.raises(ValueError, match="non-empty 'questions'"):
        _parse_questions(json.dumps({"questions": []}))


def test_rejects_empty_question_text():
    with pytest.raises(ValueError, match="non-empty string"):
        _parse_questions(json.dumps({"questions": [{"text": "   "}]}))


def test_choice_without_options_rejected():
    with pytest.raises(ValueError, match="non-empty options"):
        _parse_questions(
            json.dumps({"questions": [{"text": "pick", "kind": "choice", "options": []}]})
        )


def test_position_defaults_to_index_when_missing():
    qs = _parse_questions(json.dumps({"questions": [{"text": "a"}, {"text": "b"}]}))
    assert qs[0].position == 1
    assert qs[1].position == 2


def test_unknown_kind_coerced_to_none():
    qs = _parse_questions(json.dumps({"questions": [{"text": "a", "kind": "weird"}]}))
    assert qs[0].kind is None
