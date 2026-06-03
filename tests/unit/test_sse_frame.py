"""Unit: формат SSE-кадра + маппинг job_events → SSE (Sprint 5, ADR-012, docs/06 §S5).

Чистые функции форматирования (без БД/Redis): _format_frame (id/event/data + retry в первом),
_done_frame, _heartbeat_frame, _is_terminal_event, _parse_last_event_id.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from app.api import sse
from app.api.routers.jobs import _parse_last_event_id
from app.db.models import JobEvent


def _event(eid: int, event_type: str, to_state: str | None) -> JobEvent:
    ev = JobEvent(
        job_id="j_x", event_type=event_type, from_state=None, to_state=to_state, payload={"k": 1}
    )
    ev.id = eid
    ev.created_at = datetime(2026, 6, 2, tzinfo=UTC)
    return ev


def test_format_frame_has_id_event_data():
    frame = sse._format_frame(_event(7, "state_changed", "LIVE")).decode()
    assert "id: 7" in frame
    assert "event: state_changed" in frame
    # data: — валидный JSON с полями контракта.
    data_line = next(line for line in frame.splitlines() if line.startswith("data: "))
    data = json.loads(data_line[len("data: ") :])
    assert data["event_type"] == "state_changed"
    assert data["to_state"] == "LIVE"
    assert data["payload"] == {"k": 1}
    assert data["created_at"] is not None
    assert frame.endswith("\n\n")


def test_format_frame_retry_only_in_first():
    with_retry = sse._format_frame(_event(1, "state_changed", "BUILDING"), retry_ms=3000).decode()
    without = sse._format_frame(_event(2, "state_changed", "DEPLOYING")).decode()
    assert "retry: 3000" in with_retry
    assert "retry:" not in without


def test_done_frame_format():
    frame = sse._done_frame().decode()
    assert "event: done" in frame
    assert frame.endswith("\n\n")


def test_heartbeat_frame_is_comment():
    assert sse._heartbeat_frame() == b": ping\n\n"


def test_is_terminal_event():
    assert sse._is_terminal_event(_event(1, "state_changed", "LIVE")) is True
    assert sse._is_terminal_event(_event(2, "state_changed", "FAILED")) is True
    assert sse._is_terminal_event(_event(3, "state_changed", "BUILDING")) is False
    assert sse._is_terminal_event(_event(4, "agent_started", None)) is False


def test_parse_last_event_id_header_priority():
    # Заголовок приоритетнее query.
    assert _parse_last_event_id("42", 7) == 42
    # Только query.
    assert _parse_last_event_id(None, 7) == 7
    # Невалидный заголовок → None (стрим не падает).
    assert _parse_last_event_id("not-a-number", None) is None
    # Ничего → None (подключение без reconnect-точки).
    assert _parse_last_event_id(None, None) is None
