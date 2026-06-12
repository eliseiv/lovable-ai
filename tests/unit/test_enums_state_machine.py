"""Unit: enum состояний и устойчивые/терминальные множества (docs/03-data-model.md)."""

from __future__ import annotations

from app.db.enums import PAUSED_STATES, TERMINAL_STATES, JobState


def test_happy_path_states_present():
    for name in (
        "CREATED",
        "INTERVIEWING",
        "AWAITING_CLARIFICATION",
        "SPECCING",
        "BUILDING",
        "DEPLOYING",
        "LIVE",
        "FIXING",
        "FAILED",
    ):
        assert hasattr(JobState, name)


def test_paused_states_have_no_queued_work():
    # Пауза/устойчивые состояния — задач в очереди нет.
    assert JobState.AWAITING_CLARIFICATION in PAUSED_STATES
    assert JobState.LIVE in PAUSED_STATES
    assert JobState.FAILED in PAUSED_STATES


def test_terminal_states_only_failed():
    assert frozenset({JobState.FAILED}) == TERMINAL_STATES


def test_state_str_value_roundtrip():
    assert JobState.SPECCING.value == "SPECCING"
    assert JobState("LIVE") is JobState.LIVE


def test_editing_state_present_and_nonterminal():
    # ADR-030: EDITING — видимый промежуточный статус edit-джобы (CREATED → EDITING → BUILDING).
    assert hasattr(JobState, "EDITING")
    assert JobState.EDITING.value == "EDITING"
    assert JobState("EDITING") is JobState.EDITING
    # EDITING — активное нетерминальное LLM-фазное состояние (как INTERVIEWING/SPECCING):
    # НЕ терминал и НЕ устойчивая пауза.
    assert JobState.EDITING not in TERMINAL_STATES
    assert JobState.EDITING not in PAUSED_STATES
