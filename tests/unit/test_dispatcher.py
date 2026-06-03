"""Unit: диспетчер task-на-состояние (ADR-001).

Каждое продуктивное состояние ставит свой Celery-task в нужную очередь;
устойчивые/терминальные состояния (AWAITING_CLARIFICATION/LIVE/FAILED/INTERVIEWING)
— no-op.

FIXING — НЕ no-op: с Sprint 2 (docs/modules/pipeline/03-architecture.md §B п.2 —
"В FIXING диспетчер ставит task_fix (queue=llm)") восстановительный цикл диспетчеризует
task_fix. Прежняя версия теста ошибочно классифицировала FIXING как paused/no-op
(стейл-контракт до S2) и «проходила» лишь потому, что task_fix не был в наборе фейков
(реальный task_fix.apply_async дёргался, а ассерт проверял только 4 фейка). Контракт
актуализирован: FIXING → task_fix (queue=llm), см. test_dispatch_fixing_enqueues_task_fix.
"""

from __future__ import annotations

import pytest

from app.db.enums import JobState
from app.pipeline import dispatcher


class _FakeTask:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []

    def apply_async(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        self.calls.append((args, kwargs))


@pytest.fixture
def fake_tasks(monkeypatch):  # noqa: ANN001, ANN201
    import app.workers.tasks as tasks_mod

    fakes = {
        "task_interview": _FakeTask(),
        "task_spec": _FakeTask(),
        "task_build_request": _FakeTask(),
        "task_deploy": _FakeTask(),
        "task_fix": _FakeTask(),
    }
    for name, fake in fakes.items():
        monkeypatch.setattr(tasks_mod, name, fake)
    return fakes


@pytest.mark.parametrize(
    ("state", "task_name", "queue"),
    [
        (JobState.CREATED, "task_interview", "llm"),
        (JobState.SPECCING, "task_spec", "llm"),
        (JobState.BUILDING, "task_build_request", "build"),
        (JobState.DEPLOYING, "task_deploy", "build"),
        # FIXING → task_fix (queue=llm): восстановительный цикл S2 (docs pipeline §B п.2).
        (JobState.FIXING, "task_fix", "llm"),
    ],
)
def test_dispatch_enqueues_correct_task_and_queue(fake_tasks, state, task_name, queue):
    dispatcher.dispatch_for_state("j_x", state)
    fake = fake_tasks[task_name]
    assert len(fake.calls) == 1
    args, kwargs = fake.calls[0]
    assert kwargs["args"] == ["j_x"]
    assert kwargs["queue"] == queue
    # Остальные таски не дёрнуты.
    for other_name, other in fake_tasks.items():
        if other_name != task_name:
            assert other.calls == []


@pytest.mark.parametrize(
    "state",
    [
        JobState.AWAITING_CLARIFICATION,
        JobState.LIVE,
        JobState.FAILED,
        JobState.INTERVIEWING,
    ],
)
def test_dispatch_noop_for_paused_states(fake_tasks, state):
    dispatcher.dispatch_for_state("j_x", state)
    assert all(fake.calls == [] for fake in fake_tasks.values())


def test_dispatch_fixing_enqueues_task_fix(fake_tasks):
    """FIXING диспетчеризует task_fix в queue=llm (S2 fix-loop, docs pipeline §B п.2).

    Регресс-тест на стейл-контракт: FIXING НЕ no-op. Раньше FIXING был в paused-наборе
    и тест ложно проходил (task_fix не фейкался). Теперь task_fix в наборе фейков —
    проверяем, что дёрнут именно он (queue=llm) и ровно один раз, остальные — нет.
    """
    dispatcher.dispatch_for_state("j_fix", JobState.FIXING)
    fix = fake_tasks["task_fix"]
    assert len(fix.calls) == 1
    _, kwargs = fix.calls[0]
    assert kwargs["args"] == ["j_fix"]
    assert kwargs["queue"] == "llm"
    for name, fake in fake_tasks.items():
        if name != "task_fix":
            assert fake.calls == []
